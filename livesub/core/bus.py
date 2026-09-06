"""In-process publish/subscribe bus.

A module never names its consumer; it publishes to a *topic* and the graph config
decides who listens. That indirection is what makes stages optional (delete a node,
repoint the topic) and fan-out free (two nodes subscribe to the same topic).

The critical property, and the reason this is not just an ``asyncio.Queue``:

    **every subscriber gets its own queue.**

The display and the translator both subscribe to ``text.corrected``. With one shared
queue they would compete for events and the display would be stuck behind whatever the
translator is doing. With a queue each, corrected English reaches the screen the instant
it exists, while the translator takes as long as it needs.

Backpressure is chosen per subscription *mode*, and the difference matters:

* **``"drop"`` -- drop oldest.** Audio topics default to this. A slow LLM must never
  make the microphone lag or grow memory without bound. Dropping a 20 ms frame of
  already-stale audio is the correct loss. Drops are counted and reported rather than
  hidden.
* **``"blocking"`` -- block the publisher.** Text topics default to this. Sentences are
  precious and rare; delaying one beats losing it. A blocked text queue propagates
  backpressure upstream, which is what we want.
* **``"catchup"`` -- ring buffer.** The publisher never blocks and the subscriber reads
  at its own pace, receiving everything still in the ring. Used by nodes that must not
  lose items but also must not stall the publisher.

The bus also owns two pieces of bookkeeping modules must not do themselves:

* **revision stamping.** Every ``TextFrame`` publish gets a per-``(segment_id, topic)``
  revision assigned here, so all subscribers of a topic see identical, monotonically
  increasing revisions.
* **finalisation tracking.** Which ``(topic, segment_id)`` pairs have received a final
  ``TextFrame`` -- used by ``skip_if_finalized`` catch-up subscriptions so a slow node
  does not redo work a live node already published.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator, Literal

from .types import AudioFrame, TextFrame, Utterance

Policy = Literal["drop_oldest", "block"]
SubscriptionMode = Literal["blocking", "drop", "catchup"]

#: Sentinel put on every subscriber queue when a topic's last publisher closes.
_CLOSED = object()

DEFAULT_LIVE_MAXSIZE = 256  # ~5 s of 20 ms frames
DEFAULT_BLOCKING_MAXSIZE = 64


def default_maxsize(payload_type: type | None) -> int:
    """Queue depth by payload type. A topic name decides nothing: the same topic could
    carry any type, and it is the type that has (or lacks) a realtime constraint."""
    return DEFAULT_LIVE_MAXSIZE if payload_type is AudioFrame else DEFAULT_BLOCKING_MAXSIZE


@dataclass
class SubscriptionStats:
    received: int = 0
    dropped: int = 0
    max_depth: int = 0


class Subscription:
    """One consumer's private queue view of a topic. Async-iterable."""

    def __init__(
        self, topic: str, subscriber: str, maxsize: int, mode: SubscriptionMode
    ):
        self.topic = topic
        self.subscriber = subscriber
        self.mode: SubscriptionMode = mode
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.stats = SubscriptionStats()
        self._closed = False

    @property
    def policy(self) -> Policy:
        """Backward-compatible alias for the pre-mode naming."""
        return "drop_oldest" if self.mode == "drop" else "block"

    def _offer(self, item: Any) -> None:
        """Non-blocking delivery used by drop topics."""
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()  # discard the stalest frame
                self.stats.dropped += 1
            except asyncio.QueueEmpty:  # pragma: no cover - racy but harmless
                pass
            try:
                self.queue.put_nowait(item)
            except asyncio.QueueFull:  # pragma: no cover
                self.stats.dropped += 1
                return
        self.stats.max_depth = max(self.stats.max_depth, self.queue.qsize())

    async def _deliver(self, item: Any) -> None:
        """Blocking delivery used by text topics."""
        await self.queue.put(item)
        self.stats.max_depth = max(self.stats.max_depth, self.queue.qsize())

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Any]:
        while True:
            item = await self.queue.get()
            if item is _CLOSED:
                self._closed = True
                return
            self.stats.received += 1
            yield item

    async def get(self) -> Any | None:
        """Single item, or ``None`` once the topic is closed and drained."""
        item = await self.queue.get()
        if item is _CLOSED:
            self._closed = True
            return None
        self.stats.received += 1
        return item


class RingBuffer:
    """Shared circular buffer for ``"catchup"`` subscribers.

    The publisher appends and never blocks; each reader keeps its own position and
    consumes whatever is still in the ring. Items overwritten before a slow reader
    catches up are gone -- that is the deal this mode offers.
    """

    def __init__(self, maxsize: int = 512):
        self._buf: deque[Any] = deque(maxlen=maxsize)
        self._total: int = 0  # total items ever appended
        self._events: list[asyncio.Event] = []

    @property
    def total(self) -> int:
        return self._total

    def add_reader(self) -> asyncio.Event:
        """Register a reader; its event is set on every append."""
        event = asyncio.Event()
        self._events.append(event)
        return event

    def append(self, item: Any) -> None:
        self._buf.append(item)
        self._total += 1
        for event in self._events:
            event.set()

    def read_from(self, position: int) -> tuple[list[Any], int]:
        """Return items from ``position`` to now, and the new position.

        If ``position`` is too far behind (items have been overwritten), reading starts
        from the oldest surviving item: the caller may have missed some, which the ring
        size bounds.
        """
        oldest = self._total - len(self._buf)
        start = max(position, oldest)
        items = list(self._buf)[start - oldest :]
        return items, self._total


class CatchupSubscription:
    """A subscriber that polls a :class:`RingBuffer` instead of owning a queue.

    Starts from "now" (``subscription time``): only items published *after* the node
    starts are delivered. If ``skip_finalized_topic`` is set, utterances whose segment
    has already been finalised on that topic are skipped -- this is how a catch-up node
    avoids re-transcribing work a live node already did.
    """

    def __init__(
        self,
        topic: str,
        subscriber: str,
        ring: RingBuffer,
        bus: "Bus | None" = None,
        skip_finalized_topic: str | None = None,
    ):
        self.topic = topic
        self.subscriber = subscriber
        self.mode: SubscriptionMode = "catchup"
        self.skip_finalized_topic = skip_finalized_topic
        self._ring = ring
        self._bus = bus
        self._position = ring.total
        self._event = ring.add_reader()
        self.stats = SubscriptionStats()
        self._closed = False
        self._iterator: AsyncIterator[Any] | None = None

    @property
    def policy(self) -> str:
        return "catchup"

    def __aiter__(self) -> AsyncIterator[Any]:
        if self._iterator is None:
            self._iterator = self._iterate()
        return self._iterator

    async def _iterate(self) -> AsyncIterator[Any]:
        while True:
            await self._event.wait()
            self._event.clear()
            # Drain everything published so far BEFORE honouring close: close() fires
            # after the last publisher is done, so this final read cannot be raced.
            items, self._position = self._ring.read_from(self._position)
            for item in items:
                if self._skip(item):
                    continue
                self.stats.received += 1
                yield item
            if self._closed:
                return

    def _skip(self, item: Any) -> bool:
        if not (isinstance(item, Utterance) and self.skip_finalized_topic):
            return False
        if self._bus is None:  # pragma: no cover - bus is always passed by Bus
            return False
        return item.id in self._bus._finalized.get(self.skip_finalized_topic, set())

    def _notify(self) -> None:
        self._event.set()

    def close(self) -> None:
        """End-of-stream: wake the reader so it can exit cleanly."""
        self._closed = True
        self._event.set()

    async def get(self) -> Any | None:
        it: AsyncIterator[Any] = self.__aiter__()
        try:
            return await it.__anext__()
        except StopAsyncIteration:
            return None


@dataclass
class TopicStats:
    published: int = 0
    publishers: int = 0
    subscribers: int = 0
    mode: str = "blocking"

    @property
    def policy(self) -> str:
        """Backward-compatible alias for the pre-mode naming."""
        return {
            "drop": "drop_oldest",
            "catchup": "catchup",
            "blocking": "block",
        }.get(self.mode, self.mode)


class Bus:
    """Topic registry plus fan-out delivery.

    Publishing to a topic nobody listens to is legal and cheap -- it keeps a config that
    logs to a sink you later removed from working rather than crashing.
    """

    def __init__(self) -> None:
        self._subs: dict[str, list[Subscription | CatchupSubscription]] = {}
        self._stats: dict[str, TopicStats] = {}
        self._publishers: dict[str, int] = {}
        self._topic_types: dict[str, type] = {}
        self._revisions: dict[tuple[str, str], int] = {}
        self._finalized: dict[str, set[str]] = {}
        self._rings: dict[str, RingBuffer] = {}

    # -- wiring ---------------------------------------------------------------

    def default_mode(self, topic: str) -> SubscriptionMode:
        """Backpressure mode for a topic with no explicit subscription mode.

        Decided purely by the topic's registered payload type: live audio must never be
        blocked, text must never be dropped. An unregistered topic defaults to
        ``"blocking"`` -- the safe choice, since guessing ``"drop"`` could silently drop
        data. The graph registers every topic's type during wiring, before any
        subscription is made, so this only ever matters for hand-built buses.
        """
        if self._topic_types.get(topic) is AudioFrame:
            return "drop"
        return "blocking"  # Utterance, TextFrame, unregistered -> blocking

    def topic_type(self, topic: str) -> type | None:
        """The registered payload type for a topic, if known."""
        return self._topic_types.get(topic)

    def register_topic_type(self, topic: str, payload_type: type) -> None:
        """Declare what type a topic carries. Called by the graph during wiring.

        Sets the default subscription mode and enables revision/finalisation
        bookkeeping. Re-registering a topic as a different type is a wiring error.
        """
        if topic in self._topic_types and self._topic_types[topic] is not payload_type:
            raise ValueError(
                f"topic {topic!r} already registered as "
                f"{self._topic_types[topic].__name__}; "
                f"cannot re-register as {payload_type.__name__}"
            )
        self._topic_types[topic] = payload_type

    def subscribe(
        self,
        topic: str,
        subscriber: str = "anon",
        maxsize: int | None = None,
        mode: SubscriptionMode | None = None,
        skip_if_finalized: str | None = None,
    ) -> Subscription | CatchupSubscription:
        mode = mode or self.default_mode(topic)
        if mode == "catchup":
            ring = self._rings.setdefault(topic, RingBuffer())
            sub: Subscription | CatchupSubscription = CatchupSubscription(
                topic,
                subscriber,
                ring,
                bus=self,
                skip_finalized_topic=skip_if_finalized,
            )
        else:
            sub = Subscription(
                topic=topic,
                subscriber=subscriber,
                maxsize=(
                    maxsize
                    if maxsize is not None
                    else default_maxsize(self._topic_types.get(topic))
                ),
                mode=mode,
            )
        self._subs.setdefault(topic, []).append(sub)
        st = self._stats.setdefault(topic, TopicStats(mode=mode))
        st.mode = mode
        st.subscribers += 1
        return sub

    def register_publisher(self, topic: str) -> None:
        """Declare intent to publish, so ``close`` only fires when *all* publishers are
        done. Two translators may legitimately share one output topic."""
        self._publishers[topic] = self._publishers.get(topic, 0) + 1
        st = self._stats.setdefault(topic, TopicStats(mode=self.default_mode(topic)))
        st.publishers += 1

    # -- traffic --------------------------------------------------------------

    async def publish(self, topic: str, payload: Any) -> None:
        # TextFrame bookkeeping happens even with no subscribers: the finalized-set
        # must be complete for late catch-up subscribers, and revision counters keep
        # advancing so a resumed subscriber's numbering stays consistent.
        if isinstance(payload, TextFrame):
            key = (payload.lineage.segment_id, topic)
            next_rev = self._revisions.get(key, -1) + 1
            self._revisions[key] = next_rev
            # Do not mutate the original; every subscriber of this call must see the
            # same revision-stamped frame.
            payload = replace(
                payload, lineage=replace(payload.lineage, revision=next_rev)
            )
            if payload.is_final:
                self._finalized.setdefault(topic, set()).add(
                    payload.lineage.segment_id
                )

        st = self._stats.setdefault(topic, TopicStats(mode=self.default_mode(topic)))
        st.published += 1

        ring = self._rings.get(topic)
        if ring is not None:
            ring.append(payload)

        subs = self._subs.get(topic)
        if not subs:
            return
        for sub in subs:
            if isinstance(sub, CatchupSubscription):
                continue  # delivery happens via the ring buffer
            if sub.mode == "drop":
                sub._offer(payload)
            else:
                await sub._deliver(payload)

    async def close(self, topic: str) -> None:
        """Signal end-of-stream once every registered publisher has closed."""
        remaining = self._publishers.get(topic, 1) - 1
        self._publishers[topic] = max(remaining, 0)
        if remaining > 0:
            return
        for sub in self._subs.get(topic, []):
            if isinstance(sub, CatchupSubscription):
                sub.close()
            else:
                await sub.queue.put(_CLOSED)

    async def close_all(self) -> None:
        for topic in list(self._subs):
            for sub in self._subs[topic]:
                if isinstance(sub, CatchupSubscription):
                    sub.close()
                else:
                    await sub.queue.put(_CLOSED)

    # -- introspection --------------------------------------------------------

    @property
    def topics(self) -> list[str]:
        return sorted(set(self._subs) | set(self._stats) | set(self._topic_types))

    def subscribers_of(self, topic: str) -> list[str]:
        return [s.subscriber for s in self._subs.get(topic, [])]

    def report(self) -> dict[str, dict[str, Any]]:
        """Per-topic traffic and per-subscriber drop counts, for the bench output."""
        out: dict[str, dict[str, Any]] = {}
        for topic in self.topics:
            st = self._stats.get(topic, TopicStats())
            out[topic] = {
                "published": st.published,
                "mode": st.mode,
                "policy": st.policy,
                "subscribers": {
                    s.subscriber: {
                        "received": s.stats.received,
                        "dropped": s.stats.dropped,
                        "max_depth": s.stats.max_depth,
                    }
                    for s in self._subs.get(topic, [])
                },
            }
        return out

    def total_dropped(self) -> int:
        return sum(
            s.stats.dropped for subs in self._subs.values() for s in subs
        )
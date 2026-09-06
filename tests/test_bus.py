"""Bus behaviour. Every architectural claim in the design rests on these.

If fan-out isolation breaks, wiring the display and the translator to the same
``text.corrected`` topic silently becomes a serial chain again, and the corrected English
line starts waiting on the translator -- the exact failure the design exists to avoid.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from livesub.core.bus import Bus, RingBuffer
from livesub.core.types import AudioFrame, Lineage, TextFrame, Utterance

pytestmark = pytest.mark.asyncio


def _frame(seq: int) -> AudioFrame:
    return AudioFrame(np.zeros(320, dtype=np.float32), 16_000, seq)


def _text(i: int, segment_id: str | None = None, is_final: bool = True) -> TextFrame:
    return TextFrame(
        text=f"line {i}",
        lineage=Lineage(segment_id=segment_id or f"s{i}"),
        is_final=is_final,
    )


def _utterance(id: str, t_end: float = 1.0) -> Utterance:
    return Utterance(id=id, pcm=np.zeros(1600, dtype=np.float32), t_start=0.0,
                     t_end=t_end)


def _bus_with_audio_topic(topic: str = "audio.raw") -> Bus:
    bus = Bus()
    bus.register_topic_type(topic, AudioFrame)
    return bus


async def test_every_subscriber_receives_every_event():
    bus = Bus()
    bus.register_publisher("text.corrected")
    a = bus.subscribe("text.corrected", "translator")
    b = bus.subscribe("text.corrected", "display")

    for i in range(3):
        await bus.publish("text.corrected", _text(i))
    await bus.close("text.corrected")

    got_a = [e.text async for e in a]
    got_b = [e.text async for e in b]
    assert got_a == got_b == ["line 0", "line 1", "line 2"]


async def test_slow_subscriber_does_not_delay_a_fast_one():
    """The load-bearing test.

    A translator that takes 100 ms per line must not hold up the display that is reading
    the same topic. Both subscribe to ``text.corrected``; the display must finish all
    five lines long before the translator has finished one.
    """
    bus = Bus()
    bus.register_publisher("text.corrected")
    slow = bus.subscribe("text.corrected", "translator")
    fast = bus.subscribe("text.corrected", "display")

    display_done = asyncio.Event()
    display_order: list[str] = []
    translator_order: list[str] = []

    async def run_display():
        async for e in fast:
            display_order.append(e.text)
        display_done.set()

    async def run_translator():
        async for e in slow:
            await asyncio.sleep(0.1)
            translator_order.append(e.text)

    d = asyncio.create_task(run_display())
    t = asyncio.create_task(run_translator())

    for i in range(5):
        await bus.publish("text.corrected", _text(i))
    await bus.close("text.corrected")

    # The display must complete while the translator is still working.
    await asyncio.wait_for(display_done.wait(), timeout=0.3)
    assert len(display_order) == 5
    assert len(translator_order) < 5, "display waited for the slow translator"

    await asyncio.wait_for(t, timeout=2.0)
    assert len(translator_order) == 5  # nothing was lost, only delayed
    await d


async def test_audio_topics_drop_oldest_under_pressure():
    """A slow consumer on an audio topic must lose stale frames, not grow memory.

    A microphone that keeps producing while an overloaded stage falls behind is the
    normal case, not an error case; the correct loss is the *oldest* frame. The mode
    follows from the topic's registered payload type, never from its name.
    """
    bus = _bus_with_audio_topic()
    bus.register_publisher("audio.raw")
    sub = bus.subscribe("audio.raw", "vad", maxsize=4)
    assert sub.mode == "drop"

    for i in range(10):
        await bus.publish("audio.raw", _frame(i))  # never blocks

    assert sub.queue.qsize() == 4
    assert sub.stats.dropped == 6
    kept = [sub.queue.get_nowait().seq for _ in range(4)]
    assert kept == [6, 7, 8, 9], "kept the stalest frames instead of the freshest"


async def test_text_topics_block_rather_than_lose_a_sentence():
    bus = Bus()
    bus.register_publisher("text.raw")
    sub = bus.subscribe("text.raw", "corrector", maxsize=2)
    assert sub.mode == "blocking"

    await bus.publish("text.raw", _text(0))
    await bus.publish("text.raw", _text(1))

    blocked = asyncio.create_task(bus.publish("text.raw", _text(2)))
    await asyncio.sleep(0.05)
    assert not blocked.done(), "text publish should apply backpressure, not drop"
    assert sub.stats.dropped == 0

    await sub.get()  # make room
    await asyncio.wait_for(blocked, timeout=1.0)


async def test_mode_defaults_are_decided_by_registered_type_not_name():
    """Two topics with identical names-carrying-different-types behave differently."""
    bus = Bus()
    bus.register_topic_type("pipe.one", AudioFrame)
    bus.register_topic_type("pipe.two", TextFrame)
    drop = bus.subscribe("pipe.one", "a")
    blocking = bus.subscribe("pipe.two", "b")
    assert drop.mode == "drop"
    assert blocking.mode == "blocking"


async def test_publishing_to_a_topic_with_no_subscribers_is_harmless():
    bus = Bus()
    bus.register_publisher("text.out")
    await bus.publish("text.out", _text(0))
    assert bus.report()["text.out"]["published"] == 1


async def test_topic_closes_only_after_every_publisher_finishes():
    """Two translators may share one output topic; the display must not stop after the
    first of them finishes."""
    bus = Bus()
    bus.register_publisher("text.out")
    bus.register_publisher("text.out")
    sub = bus.subscribe("text.out", "display")

    await bus.publish("text.out", _text(0))
    await bus.close("text.out")  # first publisher done

    assert await sub.get() is not None
    await bus.publish("text.out", _text(1))
    assert (await sub.get()).text == "line 1"

    await bus.close("text.out")  # second publisher done
    assert await sub.get() is None


async def test_bus_assigns_revisions_per_segment_and_topic():
    """Revision is bus-assigned bookkeeping: modules never set it, and the same frame
    lineage published to two topics gets independent revision sequences."""
    bus = Bus()
    sub = bus.subscribe("text.corrected", "probe")

    await bus.publish("text.corrected", _text(0, segment_id="u1"))
    await bus.publish("text.corrected", _text(1, segment_id="u1"))
    await bus.publish("text.corrected", _text(2, segment_id="u2"))
    await bus.close("text.corrected")

    got = [e async for e in sub]
    assert [e.revision for e in got] == [0, 1, 0]
    assert all(e.lineage.t_audio_end is None for e in got), (
        "the bus must not touch anything but the revision"
    )


async def test_bus_does_not_mutate_the_published_frame():
    bus = Bus()
    frame = _text(0, segment_id="u1")
    await bus.publish("text.corrected", frame)
    assert frame.revision == 0, "the original frame the module holds stays untouched"


async def test_finalized_segments_are_tracked_per_topic():
    bus = Bus()
    await bus.publish("text.corrected", _text(0, segment_id="u1", is_final=True))
    await bus.publish("text.corrected", _text(1, segment_id="u2", is_final=False))
    assert bus._finalized["text.corrected"] == {"u1"}


async def test_catchup_subscriber_gets_everything_in_order_and_skips_finalized():
    """A catch-up node neither stalls the publisher nor loses utterances -- except the
    ones a live node already turned into final text."""
    bus = Bus()
    bus.register_topic_type("utterance.pipe", Utterance)
    live_sub = bus.subscribe("utterance.pipe", "live_asr")
    catchup = bus.subscribe(
        "utterance.pipe", "catchup_asr", mode="catchup", skip_if_finalized="text.raw"
    )
    assert catchup.mode == "catchup"

    async def consume() -> list[str]:
        seen = []
        async for item in catchup:
            seen.append(item.id)
        return seen

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let the consumer start waiting

    await bus.publish("utterance.pipe", _utterance("u1"))
    await bus.publish("text.raw", _text(0, segment_id="u1", is_final=True))
    await bus.publish("utterance.pipe", _utterance("u2"))
    await bus.publish("utterance.pipe", _utterance("u3"))
    await bus.close("utterance.pipe")

    seen = await asyncio.wait_for(consumer, timeout=1.0)
    assert seen == ["u2", "u3"], "u1 was already transcribed by the live node"
    # the drop subscriber still received all three
    got_live = [e.id async for e in live_sub]
    assert got_live == ["u1", "u2", "u3"]


async def test_catchup_ring_never_blocks_the_publisher():
    bus = Bus()
    ring_topic = "utterance.pipe"
    bus.register_topic_type(ring_topic, Utterance)
    bus.subscribe(ring_topic, "slow_catchup", mode="catchup")
    ring: RingBuffer = bus._rings[ring_topic]

    for i in range(ring._buf.maxlen * 2):  # far more than the ring holds
        await bus.publish(ring_topic, _utterance(f"u{i}"))
    assert len(ring._buf) == ring._buf.maxlen
    assert ring.total == ring._buf.maxlen * 2


async def test_report_counts_traffic_per_subscriber():
    bus = _bus_with_audio_topic()
    bus.register_publisher("audio.raw")
    a = bus.subscribe("audio.raw", "vad", maxsize=2)
    bus.subscribe("audio.raw", "recorder", maxsize=64)

    for i in range(5):
        await bus.publish("audio.raw", _frame(i))

    report = bus.report()["audio.raw"]
    assert report["published"] == 5
    assert report["mode"] == "drop"
    assert report["policy"] == "drop_oldest"  # backward-compatible alias
    assert report["subscribers"]["vad"]["dropped"] == 3
    assert report["subscribers"]["recorder"]["dropped"] == 0
    assert bus.total_dropped() == 3
    assert a.stats.max_depth == 2
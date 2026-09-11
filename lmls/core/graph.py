"""Builds, validates and runs the node graph.

This is the only module in the package that instantiates every module kind, and it does
so through the registry rather than directly. Everything it knows about a node is the
:class:`~lmls.core.interfaces.Module` port declarations and the topics the config
wired it to.

Validation happens at build time, before a microphone is opened or a model is loaded, so
a typo in a topic name is a one-line startup error rather than a pipeline that runs and
silently emits nothing. Types are checked by *following port declarations*, not by
topic-name prefixes: a topic carries whatever type its producing port declares, and any
reader must declare the same type on its input port.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .bus import Bus, SubscriptionMode
from .config import GraphConfig, NodeConfig
from .interfaces import Module
from .metrics import Metrics
from .registry import build, resolve
from .startup import StartupCallback
from .types import AudioFrame, TextFrame, Utterance

log = logging.getLogger("lmls.graph")


class GraphError(ValueError):
    pass


@dataclass
class BuiltNode:
    config: NodeConfig
    stage: Module
    subscriptions: list[Any] = dataclasses.field(default_factory=list)


def validate(cfg: GraphConfig) -> list[str]:
    """Check wiring. Returns non-fatal warnings; raises :class:`GraphError` on faults."""
    nodes = cfg.active
    if not nodes:
        raise GraphError("graph has no enabled nodes")

    # Duplicate name check (unchanged)
    names = [n.name for n in nodes]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise GraphError(f"duplicate node names: {sorted(dupes)}")

    classes = {n.name: resolve(n.impl) for n in nodes}

    # Port-shape sanity: config wiring must agree with the class's declarations.
    for n in nodes:
        cls = classes[n.name]
        if not cls.inputs and n.inputs:
            raise GraphError(
                f"node {n.name!r} ({cls.__name__}) declares no input ports"
            )
        if cls.inputs and not n.inputs:
            raise GraphError(f"node {n.name!r} ({cls.__name__}) needs an input topic")
        if not cls.outputs and n.outputs:
            raise GraphError(
                f"node {n.name!r} ({cls.__name__}) declares no output ports"
            )
        if cls.outputs and not n.inputs and not n.outputs:
            # Unlike a transform, a source may not skip its ports: _run_source has
            # nowhere to publish, so the whole stage would be dead weight.
            raise GraphError(
                f"node {n.name!r} ({cls.__name__}) declares output ports "
                f"{sorted(cls.outputs)} but wires none"
            )

    # A graph needs at least one node that produces without consuming.
    if not any(not cls.inputs for cls in classes.values()):
        raise GraphError("graph has no source node (no module with empty inputs)")

    # Build topic -> payload type map by following output port declarations
    topic_types: dict[str, type] = {}
    produced: dict[str, list[str]] = {}
    for n in nodes:
        cls = classes[n.name]
        for port_name, topic in n.outputs.items():
            if port_name not in cls.outputs:
                raise GraphError(
                    f"node {n.name!r}: output port {port_name!r} not declared "
                    f"by {cls.__name__}"
                )
            port_type = cls.outputs[port_name]
            if topic in topic_types and topic_types[topic] is not port_type:
                raise GraphError(
                    f"topic {topic!r} carries conflicting types: "
                    f"{topic_types[topic].__name__} and {port_type.__name__}"
                )
            topic_types[topic] = port_type
            produced.setdefault(topic, []).append(n.name)

    # Validate subscriber input port types against the topic type
    for n in nodes:
        cls = classes[n.name]
        for port_name, topic in n.inputs.items():
            if topic not in topic_types:
                raise GraphError(
                    f"topic {topic!r} is read by node {n.name!r} but nothing publishes it"
                )
            declared = cls.inputs.get(port_name)
            if declared is None:
                # Auto-named subscription ports (list-form `in`) are not declared by
                # the class; accept them when the topic carries a type the module reads.
                if topic_types[topic] in set(cls.inputs.values()):
                    continue
                raise GraphError(
                    f"node {n.name!r}: input port {port_name!r} not declared by "
                    f"{cls.__name__}"
                )
            if declared is not topic_types[topic]:
                raise GraphError(
                    f"node {n.name!r} port {port_name!r} expects {declared.__name__} "
                    f"but topic {topic!r} carries {topic_types[topic].__name__}"
                )

    # Warn about produced but unconsumed topics
    consumed = {t for n in nodes for t in n.inputs.values()}
    produced_set = set(produced)
    warnings = [
        f"topic {t!r} is published but nobody subscribes to it"
        for t in produced_set - consumed
    ]

    _check_acyclic(nodes, produced)
    return warnings


def _check_acyclic(nodes: Sequence[NodeConfig], produced: dict[str, list[str]]) -> None:
    edges: dict[str, set[str]] = {n.name: set() for n in nodes}
    for n in nodes:
        for topic in n.inputs.values():
            for upstream in produced.get(topic, []):
                edges[upstream].add(n.name)

    state: dict[str, int] = {}  # 0 unvisited, 1 on stack, 2 done

    def visit(name: str, path: list[str]) -> None:
        if state.get(name) == 1:
            cycle = " -> ".join(path[path.index(name) :] + [name])
            raise GraphError(f"cycle in graph: {cycle}")
        if state.get(name) == 2:
            return
        state[name] = 1
        for nxt in edges[name]:
            visit(nxt, path + [name])
        state[name] = 2

    for n in nodes:
        visit(n.name, [])


def mermaid(cfg: GraphConfig) -> str:
    """Render the wiring as a mermaid flowchart for the technical report.

    Shape follows the port declaration, not a module kind: no inputs is a source
    (stadium), no outputs is a sink (trapezoid), both is a transform (rectangle).
    """
    lines = ["flowchart LR"]
    for n in cfg.active:
        cls = resolve(n.impl)
        if not cls.outputs:
            lo, hi = "[/", "/]"  # sink
        elif not cls.inputs:
            lo, hi = "([", "])"  # source
        else:
            lo, hi = "[", "]"  # transform
        lines.append(f'    {n.name}{lo}"{n.name}<br/><i>{n.impl}</i>"{hi}')
    for topic in sorted(
        {t for n in cfg.active for t in n.out_topics}
        | {t for n in cfg.active for t in n.in_topics}
    ):
        lines.append(f'    {_tid(topic)}(("{topic}"))')
    for n in cfg.active:
        for topic in n.out_topics:
            lines.append(f"    {n.name} --> {_tid(topic)}")
        for topic in n.in_topics:
            lines.append(f"    {_tid(topic)} --> {n.name}")
    return "\n".join(lines)


def _tid(topic: str) -> str:
    return "t_" + topic.replace(".", "_")


class Graph:
    """A built, runnable graph."""

    def __init__(self, cfg: GraphConfig, bus: Bus | None = None,
                 on_startup: StartupCallback | None = None,
                 on_event: Callable[[dict[str, Any]], None] | None = None,
                 namespace_segments: bool = False):
        self.config = cfg
        self.bus = bus or Bus()
        self._on_startup = on_startup
        self._on_event = on_event
        self._namespace_segments = namespace_segments
        self.warnings = validate(cfg)
        for w in self.warnings:
            log.warning("%s", w)
        self.metrics = Metrics()
        # Register every topic's payload type before any node is built, so default
        # backpressure modes are decided by type and never by config file order.
        for n in cfg.active:
            cls = resolve(n.impl)
            for port_name, topic in n.outputs.items():
                self.bus.register_topic_type(topic, cls.outputs[port_name])
        self.nodes: list[BuiltNode] = [self._build_node(n) for n in cfg.active]
        # Drivers such as ``lmls demo`` may warm a stage by calling its processing
        # path before Graph.start(). Attach the reporter at construction time so model
        # downloads and load failures from that warm-up are not silently lost.
        if self._on_startup is not None:
            for node in self.nodes:
                node.stage._startup_reporter = self._on_startup
        self._tasks: list[asyncio.Task] = []
        self.started_at = 0.0
        # run() and external drivers (the demo pre-starts stages to warm models) may
        # both call start(); a stage holding a resource must be started exactly once.
        self._started: set[int] = set()
        self._start_order: list[BuiltNode] = []
        self._start_lock = asyncio.Lock()
        self._close_task: asyncio.Task | None = None
        self._starting: asyncio.Task | None = None
        self._aborted = False
        self._states = {n.config.name: {"state": "pending", "message": ""}
                        for n in self.nodes}
        self._in_flight = {n.config.name: 0 for n in self.nodes}
        self._no_output_warned: set[str] = set()

    def _emit(self, event: dict[str, Any]) -> None:
        """Synchronous, loop-thread notification; no event history is retained."""
        if self._on_event is not None:
            try:
                self._on_event(event)
            except (Exception, asyncio.CancelledError):
                log.exception("graph observer failed")

    def _state(self, node: BuiltNode, state: str, message: str = "") -> None:
        # Retain one bounded message per configured node, never exception tracebacks.
        status = {"state": state, "message": message[:2048]}
        self._states[node.config.name] = status
        self._emit({"kind": "node_state", "node": node.config.name, **status})

    def snapshot(self) -> dict[str, Any]:
        """Return detached JSON data. Call on the graph's event loop while running.

        Source describe() implementations must be cheap synchronous diagnostics.
        Exceptions and non-JSON results are omitted; payloads are never retained.
        """
        nodes = {}
        for node in self.nodes:
            name = node.config.name
            count = self.metrics.stage_counts.get(name, 0)
            total = self.metrics.stage_totals.get(name, 0.0)
            samples = self.metrics.stage_ms.get(name)
            info = {
                **self._states[name], "in_flight": self._in_flight[name],
                "processing": {
                    "count": count, "total_ms": total,
                    "mean_ms": total / count if count else 0.0,
                    "last_ms": samples[-1] if samples else 0.0,
                },
            }
            if not node.stage.inputs:
                try:
                    diagnostics = node.stage.describe()
                    if isinstance(diagnostics, dict):
                        info["diagnostics"] = json.loads(
                            json.dumps(diagnostics, allow_nan=False)
                        )
                except (Exception, asyncio.CancelledError):
                    pass
            nodes[name] = info
        return {"nodes": nodes, "metrics": self.metrics.summary(),
                "bus": self.bus.report(), "started_at": self.started_at}

    def _build_node(self, nc: NodeConfig) -> BuiltNode:
        stage = build(nc.impl, name=nc.name, **nc.options)
        node = BuiltNode(config=nc, stage=stage)

        mode: SubscriptionMode | None = (
            None if nc.mode == "default" else nc.mode  # type: ignore[assignment]
        )
        for port_name, topic in nc.inputs.items():
            if mode is None and self.bus.topic_type(topic) is AudioFrame:
                # Offline runs replay files as fast as they are consumed; drop-oldest
                # would silently discard most of the recording. The benchmark sets
                # audio_backpressure="block" to make audio readers wait instead.
                if self.config.settings.get("audio_backpressure") == "block":
                    mode = "blocking"
            sub = self.bus.subscribe(
                topic,
                subscriber=nc.name,
                mode=mode,
                skip_if_finalized=nc.skip_if_finalized,
            )
            node.subscriptions.append(sub)

        for topic in nc.out_topics:
            self.bus.register_publisher(topic)

        return node

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Start every stage exactly once.

        Drivers that need models warm before their own work begins (the demo warms the
        pipeline, then starts playback) call this instead of poking ``node.stage``
        directly. run() consults the same bookkeeping, so a stage started here is not
        started a second time -- the websocket sink binds its port in start(), and a
        second bind is EADDRINUSE even with no other process present.
        """
        async with self._start_lock:
            if self._close_task is not None:
                raise RuntimeError("graph is closed; build a new graph for another run")
            try:
                # Resource-producing sources must not start until every consumer is ready.
                for node in sorted(self.nodes, key=lambda n: not bool(n.stage.inputs)):
                    if id(node) in self._started:
                        continue
                    self._started.add(id(node))
                    self._start_order.append(node)  # includes partially started stages
                    self._state(node, "starting")
                    try:
                        self._starting = asyncio.create_task(node.stage.start())
                        await self._starting
                        if self._close_task is not None:
                            raise asyncio.CancelledError
                    except asyncio.CancelledError:
                        self._state(node, "completed", "startup cancelled")
                        raise
                    except Exception as exc:
                        self._state(node, "failed", str(exc))
                        raise
                    finally:
                        self._starting = None
                    self._state(node, "ready")
            except BaseException:
                self._aborted = True
                await self.aclose()
                raise

    async def run(self, timeout: float | None = None) -> None:
        self.started_at = time.time()
        try:
            await self.start()
            self._tasks = [
                asyncio.create_task(self._runner(node), name=node.config.name)
                for node in self.nodes
            ]
            try:
                async with asyncio.timeout(timeout) as deadline:
                    await asyncio.gather(*self._tasks)
            except asyncio.TimeoutError:
                if not deadline.expired():
                    raise  # a stage's own TimeoutError is a failure, not our deadline
                self._aborted = True
                log.info("graph stopped after %.1fs timeout", timeout)
        except BaseException:
            self._aborted = True
            raise
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Idempotent cleanup, protected even against repeated caller cancellation."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._cleanup(), name="graph:cleanup")
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _cleanup(self) -> None:
        if self._starting is not None and not self._starting.done():
            self._aborted = True
            if not self._starting.cancelling():
                self._starting.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._starting
        for task in self._tasks:
            if not task.done():
                self._aborted = True
                if not task.cancelling():
                    task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._aborted:
            # No consumers remain to make room for a blocking end-of-stream marker.
            await self.bus.close_all(discard_pending=True)
        for node in reversed(self._start_order):
            try:
                await node.stage.stop()
            except (Exception, asyncio.CancelledError) as exc:
                self._state(node, "failed", f"stop failed: {exc}")
                log.exception("node %r stop failed", node.config.name)
            if self._states[node.config.name]["state"] not in {"completed", "failed"}:
                self._state(node, "completed", "stopped")
        self._started.clear()
        self._start_order.clear()
        self._tasks.clear()

    # -- unified runners ------------------------------------------------------

    async def _runner(self, node: BuiltNode) -> None:
        self._state(node, "running")
        try:
            cls = type(node.stage)
            if not cls.inputs:
                # Source: no subscriptions, has a run() method
                await self._run_source(node)
            elif not cls.outputs:
                # Sink: has subscriptions, process() returns None
                await self._run_sink(node)
            else:
                # Transform: has both inputs and outputs
                await self._run_transform(node)
            for topic in node.config.out_topics:
                await self.bus.close(topic)
        except asyncio.CancelledError:
            self._state(node, "completed", "cancelled")
            raise
        except Exception as exc:
            self._state(node, "failed", str(exc))
            log.exception("node %r failed", node.config.name)
            raise
        else:
            self._state(node, "completed")

    async def _run_source(self, node: BuiltNode) -> None:
        stage = node.stage
        # Sources have exactly one output port
        topic = node.config.out_topics[0]
        async with contextlib.aclosing(stage.run()) as stream:
            async for payload in stream:
                await self._publish(node, topic, self._namespace_origin(node, payload))

    def _namespace_origin(self, node: BuiltNode, payload: Any) -> Any:
        """Scope locally originating segment IDs without retaining an ID map."""
        if not self._namespace_segments:
            return payload
        if isinstance(payload, Utterance):
            return dataclasses.replace(payload, id=json.dumps(
                [node.config.name, payload.id], separators=(",", ":")
            ))
        if isinstance(payload, TextFrame):
            return dataclasses.replace(payload, lineage=dataclasses.replace(
                payload.lineage, segment_id=json.dumps(
                    [node.config.name, payload.segment_id], separators=(",", ":")
                )
            ))
        return payload

    async def _run_transform(self, node: BuiltNode) -> None:
        stage = node.stage
        is_async = inspect.iscoroutinefunction(stage.process)

        # If multiple subscriptions (fan-in), run one pump per subscription concurrently
        pumps = [
            asyncio.create_task(self._pump(node, sub, is_async))
            for sub in node.subscriptions
        ]
        await self._join_pumps(pumps)

        # Input stream exhausted: publish whatever the module still buffers.
        for leftover in stage.drain():
            await self._publish_result(node, leftover, None, 0.0)

    async def _pump(self, node: BuiltNode, sub, is_async: bool) -> None:
        async for payload in sub:
            result, elapsed = await self._process(node, payload, is_async)
            await self._publish_result(node, result, payload, elapsed)

    async def _join_pumps(self, pumps: list[asyncio.Task]) -> None:
        try:
            await asyncio.gather(*pumps)
        finally:
            # gather does not cancel siblings when one raises.
            for pump in pumps:
                if not pump.done() and not pump.cancelling():
                    pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)

    async def _process(
        self, node: BuiltNode, payload: Any, is_async: bool
    ) -> tuple[Any, float]:
        elapsed = 0.0
        invoked = False

        def process_sync():
            nonlocal elapsed, invoked
            invoked = True
            t0 = time.perf_counter()
            try:
                return node.stage.process(payload)
            finally:
                elapsed = time.perf_counter() - t0

        self._in_flight[node.config.name] += 1
        try:
            if is_async:
                invoked = True
                t0 = time.perf_counter()
                try:
                    result = await node.stage.process(payload)
                finally:
                    elapsed = time.perf_counter() - t0
            else:
                future = asyncio.get_running_loop().run_in_executor(None, process_sync)
                try:
                    result = await asyncio.shield(future)
                except asyncio.CancelledError:
                    # Python cannot interrupt a running worker: join it before stop()
                    # releases resources that process() may still be using.
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await future
                    raise
            return result, elapsed
        finally:
            try:
                if invoked:
                    self.metrics.record_stage(node.config.name, elapsed * 1000)
                    self._emit({"kind": "processing", "node": node.config.name,
                                "elapsed_ms": elapsed * 1000})
            finally:
                self._in_flight[node.config.name] -= 1

    async def _publish(self, node: BuiltNode, topic: str, payload: Any) -> None:
        self._in_flight[node.config.name] += 1
        try:
            payload = await self.bus.publish(topic, payload)
            if isinstance(payload, TextFrame):
                self.metrics.record_event(payload)
            self._emit({"kind": "output", "node": node.config.name,
                        "topic": topic, "payload": payload})
        finally:
            self._in_flight[node.config.name] -= 1

    async def _publish_result(
        self, node: BuiltNode, result: Any, source_payload: Any, elapsed_s: float
    ) -> None:
        """Dispatch the module's return value to the correct output topic(s).

        Also records stage latency into the Lineage if the result contains a TextFrame.
        The bus stamps the revision when the frame is published.
        """
        if result is None:
            return

        items: list[tuple[str | None, Any]] = []  # (port_name_or_None, payload)

        if isinstance(result, dict):
            items = [(port, payload) for port, payload in result.items()]
        elif isinstance(result, list):
            items = [(None, item) for item in result]
        else:
            items = [(None, result)]

        for port_name, payload in items:
            if isinstance(payload, Utterance) and (
                not isinstance(source_payload, Utterance) or payload.id != source_payload.id
            ):
                payload = self._namespace_origin(node, payload)
            # Inject stage latency into Lineage for TextFrames
            if isinstance(payload, TextFrame):
                payload = dataclasses.replace(
                    payload,
                    lineage=payload.lineage.record_latency(
                        node.config.name, elapsed_s
                    ),
                )

            # Resolve output topic
            if port_name is not None:
                topic = node.config.topic_for_port(port_name)
            else:
                # A multi-port module may explicitly declare its bare-result port.
                # Sorting topic strings would otherwise reroute a translation when
                # an editor connects its optional correction output.
                default_port = node.stage.default_output
                if default_port is not None:
                    if default_port not in node.stage.outputs:
                        raise GraphError(f"{node.config.name}: default output {default_port!r} is not declared")
                    topic = node.config.topic_for_port(default_port)
                elif len(node.config.outputs) == 1:
                    topic = next(iter(node.config.outputs.values()))
                elif not node.config.outputs:
                    # Every output port was skipped or never wired: like a named
                    # result for an unwired port, the frame is simply dropped.
                    if node.config.name not in self._no_output_warned:
                        self._no_output_warned.add(node.config.name)
                        log.warning(
                            "node %r: no output topic is wired; its results are discarded",
                            node.config.name,
                        )
                    continue
                else:
                    raise GraphError(f"{node.config.name}: multiple outputs require a named result or default_output")

            if topic is None:
                log.debug(
                    "node %r: no topic for port %r; dropping",
                    node.config.name,
                    port_name,
                )
                continue

            await self._publish(node, topic, payload)

    async def _run_sink(self, node: BuiltNode) -> None:
        stage = node.stage
        is_async = inspect.iscoroutinefunction(stage.process)
        lock = asyncio.Lock()  # serialise writes; sinks render shared state

        async def pump(sub) -> None:
            async for payload in sub:
                async with lock:
                    await self._process(node, payload, is_async)

        await self._join_pumps([
            asyncio.create_task(pump(sub)) for sub in node.subscriptions
        ])


async def run_config(cfg: GraphConfig, timeout: float | None = None) -> Graph:
    graph = Graph(cfg)
    await graph.run(timeout=timeout)
    return graph

"""Builds, validates and runs the node graph.

This is the only module in the package that instantiates every module kind, and it does
so through the registry rather than directly. Everything it knows about a node is the
:class:`~livesub.core.interfaces.Module` port declarations and the topics the config
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
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from .bus import Bus, SubscriptionMode
from .config import GraphConfig, NodeConfig
from .interfaces import Module
from .metrics import Metrics
from .registry import build, resolve
from .types import AudioFrame, TextFrame

log = logging.getLogger("livesub.graph")


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
        if cls.outputs and not n.outputs:
            raise GraphError(f"node {n.name!r} ({cls.__name__}) needs an output topic")

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

    def __init__(self, cfg: GraphConfig, bus: Bus | None = None):
        self.config = cfg
        self.bus = bus or Bus()
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
        self._tasks: list[asyncio.Task] = []
        self.started_at = 0.0
        # run() and external drivers (the demo pre-starts stages to warm models) may
        # both call start(); a stage holding a resource must be started exactly once.
        self._started: set[int] = set()

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
        for node in self.nodes:
            if id(node) not in self._started:
                self._started.add(id(node))
                await node.stage.start()

    async def run(self, timeout: float | None = None) -> None:
        self.started_at = time.time()
        await self.start()
        self._tasks = [
            asyncio.create_task(self._runner(node), name=node.config.name)
            for node in self.nodes
        ]
        try:
            if timeout:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=False), timeout
                )
            else:
                await asyncio.gather(*self._tasks)
        except asyncio.TimeoutError:
            log.info("graph stopped after %.1fs timeout", timeout)
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        for task in self._tasks:
            if not task.done():
                task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for node in self.nodes:
            if id(node) not in self._started:
                continue  # never started; stopping it would close nothing it opened
            with contextlib.suppress(Exception):
                await node.stage.stop()  # flush() responsibility moved here
        self._started.clear()

    # -- unified runners ------------------------------------------------------

    async def _runner(self, node: BuiltNode) -> None:
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
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("node %r failed", node.config.name)
            raise
        finally:
            for topic in node.config.out_topics:
                await self.bus.close(topic)

    async def _run_source(self, node: BuiltNode) -> None:
        stage = node.stage
        # Sources have exactly one output port
        topic = node.config.out_topics[0]
        async for payload in stage.run():
            await self.bus.publish(topic, payload)

    async def _run_transform(self, node: BuiltNode) -> None:
        stage = node.stage
        is_async = inspect.iscoroutinefunction(stage.process)

        # If multiple subscriptions (fan-in), run one pump per subscription concurrently
        pumps = [
            asyncio.create_task(self._pump(node, sub, stage, is_async))
            for sub in node.subscriptions
        ]
        await asyncio.gather(*pumps)

        # Input stream exhausted: publish whatever the module still buffers.
        for leftover in stage.drain():
            await self._publish_result(node, leftover, None, 0.0)

    async def _pump(self, node: BuiltNode, sub, stage: Module, is_async: bool) -> None:
        loop = asyncio.get_running_loop()
        async for payload in sub:
            t0 = time.perf_counter()
            if is_async:
                result = await stage.process(payload)
            else:
                result = await loop.run_in_executor(None, stage.process, payload)
            elapsed = time.perf_counter() - t0
            self.metrics.record_stage(node.config.name, elapsed * 1000)
            await self._publish_result(node, result, payload, elapsed)

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
                # Bare payload: the module publishes everything it makes to its single
                # output topic. (A multi-port module must return a dict keyed by port.)
                topic = node.config.out_topics[0]

            if topic is None:
                log.debug(
                    "node %r: no topic for port %r; dropping",
                    node.config.name,
                    port_name,
                )
                continue

            if isinstance(payload, TextFrame):
                self.metrics.record_event(payload)
            await self.bus.publish(topic, payload)

    async def _run_sink(self, node: BuiltNode) -> None:
        stage = node.stage
        is_async = inspect.iscoroutinefunction(stage.process)
        loop = asyncio.get_running_loop()
        lock = asyncio.Lock()  # serialise writes; sinks render shared state

        async def pump(sub) -> None:
            async for payload in sub:
                async with lock:
                    if is_async:
                        await stage.process(payload)
                    else:
                        await loop.run_in_executor(None, stage.process, payload)

        await asyncio.gather(*(pump(sub) for sub in node.subscriptions))


async def run_config(cfg: GraphConfig, timeout: float | None = None) -> Graph:
    graph = Graph(cfg)
    await graph.run(timeout=timeout)
    return graph
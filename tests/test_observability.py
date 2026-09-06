"""Graph telemetry and resource ownership, without models, devices, or web code."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
import numpy as np

from livesub.core.bus import Bus
from livesub.core.config import GraphConfig, NodeConfig
from livesub.core.graph import Graph
from livesub.core.interfaces import Module
from livesub.core.metrics import Metrics
from livesub.core.registry import REGISTRY
from livesub.core.startup import StartupPhase
from livesub.core.types import AudioFrame, Lineage, TextFrame, Utterance


class Source(Module):
    outputs = {"out": TextFrame}

    def __init__(self):
        super().__init__()
        self.starts = self.stops = 0
        self.frames = [TextFrame("hello", lineage=Lineage("segment", revision=99))]
        self.closed = False

    async def start(self):
        self.starts += 1
        self._report_startup(StartupPhase.READY)

    async def stop(self):
        self.stops += 1

    async def run(self):
        try:
            for frame in self.frames:
                yield frame
        finally:
            self.closed = True


class Transform(Source):
    inputs = {"in": TextFrame}

    def process(self, frame):
        return [replace(frame, text=frame.text + "!"), replace(frame, text="again")]


class Sink(Source):
    inputs = {"in": TextFrame}
    outputs = {}

    def __init__(self):
        super().__init__()
        self.received = []

    async def process(self, frame):
        self.received.append(frame)


class AudioSource(Source):
    outputs = {"out": AudioFrame}


class UtteranceSource(Source):
    outputs = {"out": Utterance}


class Segmenter(Source):
    inputs = {"in": AudioFrame}
    outputs = {"out": Utterance}

    def process(self, frame):
        partial = Utterance("u0001", frame.pcm, 1.0, 2.0, is_final=False)
        return [partial, replace(partial, is_final=True)]

    def drain(self):
        return [Utterance("u0001", np.zeros(1, dtype=np.float32), 1.0, 2.0)]


class UtterancePass(Source):
    inputs = {"in": Utterance}
    outputs = {"out": Utterance}

    def process(self, frame):
        return frame


class UtteranceSink(Sink):
    inputs = {"in": Utterance}


@pytest.fixture
def make_graph(monkeypatch):
    for cls in (Source, Transform, Sink, AudioSource, UtteranceSource, Segmenter,
                UtterancePass, UtteranceSink):
        monkeypatch.setitem(REGISTRY, cls.__name__, f"{__name__}:{cls.__name__}")

    def make(*, fanin=False, transform=True, **kwargs):
        nodes = [NodeConfig("source", "Source", outputs={"out": "raw"})]
        inputs = {"in": "raw"}
        if fanin:
            nodes.append(NodeConfig("other", "Source", outputs={"out": "other"}))
            inputs["other"] = "other"
        if transform:
            nodes.append(NodeConfig("transform", "Transform", inputs=inputs,
                                    outputs={"out": "processed"}))
            inputs = {"in": "processed"}
        nodes.append(NodeConfig("sink", "Sink", inputs=inputs))
        return Graph(GraphConfig(nodes), **kwargs)

    return make


@pytest.mark.asyncio
async def test_lifecycle_stamped_output_loop_thread_and_processing_counts(make_graph):
    events, startup = [], []
    loop = asyncio.get_running_loop()
    thread = threading.get_ident()

    def observe(event):
        assert asyncio.get_running_loop() is loop
        assert threading.get_ident() == thread
        events.append(event)

    graph = make_graph(on_event=observe, on_startup=startup.append)
    source, transform, sink = [n.stage for n in graph.nodes]
    source.frames *= 2
    before = graph.snapshot()
    assert before["nodes"]["source"]["state"] == "pending"
    await graph.start()
    await graph.run()
    await graph.aclose()

    assert all(stage.starts == stage.stops == 1 for stage in (source, transform, sink))
    assert {event.module_name for event in startup} == {"source", "transform", "sink"}
    ready = [e["node"] for e in events if e.get("state") == "ready"]
    assert ready == ["transform", "sink", "source"]
    for node in graph.nodes:
        assert [e["state"] for e in events if e["kind"] == "node_state"
                and e["node"] == node.config.name] == [
                    "starting", "ready", "running", "completed"]
    raw = [e["payload"] for e in events if e.get("topic") == "raw"]
    assert [frame.revision for frame in raw] == [0, 1]
    assert source.frames[0].revision == 99
    emitted = [e["payload"] for e in events if e.get("topic") == "processed"]
    assert [frame.revision for frame in emitted] == [0, 1, 2, 3]
    assert all(a is b for a, b in zip(emitted, sink.received))
    snapshot = graph.snapshot()
    assert json.loads(json.dumps(snapshot, allow_nan=False)) == snapshot
    assert snapshot["nodes"]["transform"]["processing"]["count"] == 2
    assert snapshot["nodes"]["sink"]["processing"]["count"] == 4
    assert snapshot["nodes"]["source"]["processing"]["count"] == 0
    assert snapshot["metrics"]["stages"]["transform"]["n"] == 2
    assert snapshot["metrics"]["counts"] == {"en": 6}
    assert len([e for e in events if e["kind"] == "processing"]) == 6
    assert all(e["elapsed_ms"] >= 0 for e in events if e["kind"] == "processing")
    snapshot["nodes"]["source"]["state"] = "corrupted"
    assert graph.snapshot()["nodes"]["source"]["state"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_observer_errors_cannot_crash_or_cancel_graph(make_graph, error):
    seen = set()

    def broken(event):
        seen.add(event["kind"])
        raise error("observer error")

    graph = make_graph(on_event=broken)
    await graph.run()
    assert seen == {"node_state", "output", "processing"}
    assert len(graph.nodes[-1].stage.received) == 2
    assert all(n.stage.stops == 1 for n in graph.nodes)


@pytest.mark.asyncio
@pytest.mark.parametrize("direct_start", [True, False])
@pytest.mark.parametrize("error", [RuntimeError, TimeoutError])
async def test_startup_failure_stops_partial_stage_and_prior_consumers(
    make_graph, direct_start, error
):
    graph = make_graph()
    source, transform, sink = [n.stage for n in graph.nodes]

    async def fail():
        sink.starts += 1
        raise error("bind failed")

    sink.start = fail
    with pytest.raises(error, match="bind failed"):
        await (graph.start() if direct_start else graph.run())
    await graph.aclose()
    assert source.starts == source.stops == 0
    assert transform.starts == transform.stops == sink.starts == sink.stops == 1
    assert not graph._started
    assert graph.snapshot()["nodes"]["sink"]["state"] == "failed"
    assert graph.snapshot()["nodes"]["sink"]["message"] == "bind failed"


@pytest.mark.asyncio
async def test_startup_cancel_and_repeated_cancel_wait_for_cleanup(make_graph):
    graph = make_graph()
    entered, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stage = graph.nodes[1].stage

    async def start():
        entered.set()
        await asyncio.Event().wait()

    async def stop():
        stopping.set()
        await release.wait()
        stage.stops += 1

    stage.start, stage.stop = start, stop
    task = asyncio.create_task(graph.start())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.wait_for(stopping.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert stage.stops == 1
    assert not graph._started


@pytest.mark.asyncio
@pytest.mark.parametrize("transform", [True, False])
async def test_fanin_failure_joins_sibling_pumps_before_stop(make_graph, transform):
    graph = make_graph(fanin=True, transform=transform)
    graph.nodes[0].stage.frames = [TextFrame("wait")]
    graph.nodes[1].stage.frames = [TextFrame("fail")]
    stage = graph.nodes[2].stage
    waiting, cancelled = asyncio.Event(), asyncio.Event()

    async def process(frame):
        if frame.text == "fail":
            raise RuntimeError("process failed")
        waiting.set()
        if not transform:
            # Sink locks serialise processing; let the second pump acquire the lock.
            return
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def stop():
        if transform:
            assert cancelled.is_set()
        stage.stops += 1

    stage.process, stage.stop = process, stop
    before = asyncio.all_tasks()
    with pytest.raises(RuntimeError, match="process failed"):
        await asyncio.wait_for(graph.run(), 1)
    assert waiting.is_set()
    assert not (asyncio.all_tasks() - before)
    assert all(n.stage.stops == 1 for n in graph.nodes)
    assert graph.snapshot()["nodes"][stage.name]["processing"]["count"] == 2


@pytest.mark.asyncio
async def test_cancel_full_queues_closes_source_and_leaves_no_pumps(make_graph):
    graph = make_graph(fanin=True, transform=False)
    graph.nodes[0].stage.frames *= 1000
    graph.nodes[1].stage.frames *= 1000
    entered = asyncio.Event()

    async def block(frame):
        entered.set()
        await asyncio.Event().wait()

    graph.nodes[-1].stage.process = block
    before = asyncio.all_tasks()
    task = asyncio.create_task(graph.run())
    await asyncio.wait_for(entered.wait(), 1)
    assert any(n.subscriptions[0].queue.full() for n in graph.nodes if n.subscriptions)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert all(n.stage.stops == 1 for n in graph.nodes)
    assert graph.nodes[0].stage.closed and graph.nodes[1].stage.closed
    assert not (asyncio.all_tasks() - before)
    assert all(sub["depth"] == 0 for topic in graph.snapshot()["bus"].values()
               for sub in topic["subscribers"].values())


@pytest.mark.asyncio
async def test_cancel_while_bus_close_waits_for_full_queue(make_graph):
    published = asyncio.Event()

    def observe(event):
        if event["kind"] == "output":
            published.set()

    graph = make_graph(transform=False, on_event=observe)
    probe = graph.bus.subscribe("raw", "unread", maxsize=1)
    task = asyncio.create_task(graph.run())
    await asyncio.wait_for(published.wait(), 1)
    # The sole frame filled the probe; normal source EOF is blocked inserting its marker.
    assert probe.queue.full()
    assert graph.nodes[0].stage.closed
    assert graph.snapshot()["nodes"]["source"]["state"] == "running"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert all(n.stage.stops == 1 for n in graph.nodes)
    assert probe.depth == 0
    assert await probe.get() is None


@pytest.mark.asyncio
async def test_close_during_startup_cancels_start_before_releasing_resources(make_graph):
    graph = make_graph()
    entered, finished = asyncio.Event(), asyncio.Event()
    stage = graph.nodes[1].stage

    async def start():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    async def stop():
        assert finished.is_set()
        stage.stops += 1

    stage.start, stage.stop = start, stop
    task = asyncio.create_task(graph.run())
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(graph.aclose(), 1)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert stage.stops == 1
    assert graph.nodes[0].stage.starts == 0


@pytest.mark.asyncio
async def test_sync_processing_cancel_joins_worker_before_stage_stop(make_graph):
    graph = make_graph()
    entered, finished = asyncio.Event(), threading.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    stage = graph.nodes[1].stage

    def process(frame):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        finished.set()
        return frame

    async def stop():
        assert finished.is_set()
        stage.stops += 1

    stage.process, stage.stop = process, stop
    task = asyncio.create_task(graph.run())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert stage.stops == 0
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert stage.stops == 1
    assert graph.metrics.stage_counts["transform"] == 1


@pytest.mark.asyncio
async def test_executor_queue_wait_is_not_processing_time(make_graph, monkeypatch):
    graph = make_graph()
    loop = asyncio.get_running_loop()
    release = threading.Event()
    entered, submitted = asyncio.Event(), asyncio.Event()
    clock = [0.0]
    monkeypatch.setattr("livesub.core.graph.time.perf_counter", lambda: clock[0])
    real_executor = loop.run_in_executor

    with ThreadPoolExecutor(max_workers=1) as executor:
        def occupy():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)

        blocker = real_executor(executor, occupy)
        await asyncio.wait_for(entered.wait(), 1)

        def submit(_executor, function, *args):
            submitted.set()
            return real_executor(executor, function, *args)

        monkeypatch.setattr(loop, "run_in_executor", submit)
        task = asyncio.create_task(graph.run())
        try:
            await asyncio.wait_for(submitted.wait(), 1)
            clock[0] = 10.0  # ten seconds in the executor queue, zero in process()
        finally:
            release.set()
        await blocker
        await asyncio.wait_for(task, 1)
    assert graph.snapshot()["nodes"]["transform"]["processing"]["total_ms"] == 0


@pytest.mark.asyncio
async def test_drain_and_none_results_do_not_inflate_processing_count(make_graph):
    graph = make_graph()
    stage = graph.nodes[1].stage
    stage.process = lambda frame: None
    stage.drain = lambda: [TextFrame("flushed") for _ in range(3)]
    await graph.run()
    assert graph.metrics.stage_counts["transform"] == 1
    assert graph.metrics.stage_counts["sink"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("own_timeout", [True, False])
async def test_runtime_timeout_only_swallows_graph_deadline(make_graph, own_timeout):
    graph = make_graph()

    async def process(frame):
        if own_timeout:
            raise TimeoutError("service timed out")
        await asyncio.Event().wait()

    graph.nodes[1].stage.process = process
    if own_timeout:
        with pytest.raises(TimeoutError, match="service timed out"):
            await graph.run(timeout=0.1)
        assert graph.snapshot()["nodes"]["transform"]["state"] == "failed"
    else:
        await graph.run(timeout=0.01)
        assert graph.snapshot()["nodes"]["transform"]["state"] == "completed"
    assert all(n.stage.stops == 1 for n in graph.nodes)


@pytest.mark.asyncio
async def test_stop_failure_does_not_skip_other_resources(make_graph):
    graph = make_graph()

    async def bad_stop():
        raise RuntimeError("close failed")

    graph.nodes[0].stage.stop = bad_stop
    await graph.run()
    assert all(n.stage.stops == 1 for n in graph.nodes[1:])
    assert graph.snapshot()["nodes"]["source"]["state"] == "failed"


def test_metrics_bound_samples_keep_lifetime_counts_mean_max_and_ignore_lineage(monkeypatch):
    metrics = Metrics(max_samples=3)
    monkeypatch.setattr("livesub.core.types.time.time", lambda: 100.0)
    for i in range(10):
        metrics.record_stage("work", i)
        metrics.record_event(TextFrame("hi", lineage=Lineage(
            "segment", t_audio_end=90 + i, stage_latency_ms={"work": i, "fake": 999}
        )))
    assert list(metrics.stage_ms["work"]) == [7, 8, 9]
    assert len(metrics.end_to_end_ms["en"]) == 3
    summary = metrics.summary()
    assert set(summary) == {"counts", "stages", "end_to_end"}
    assert summary["stages"] == {"work": {"n": 10, "p50": 8, "p95": 8.9, "mean": 4.5}}
    assert summary["end_to_end"]["en"] == {"n": 10, "p50": 2000, "p95": 2900, "max": 10000}
    assert summary["counts"] == {"en": 10}
    assert "work" in metrics.format_table()


def test_snapshot_omits_unsafe_diagnostics_and_detaches_safe_values(make_graph):
    graph = make_graph()
    stage = graph.nodes[0].stage
    data = {"position": [10], "paused": True}
    stage.describe = lambda: data
    result = graph.snapshot()
    data["position"].append(20)
    assert result["nodes"]["source"]["diagnostics"]["position"] == [10]
    for bad in ({"handle": object()}, {"nan": float("nan")}):
        stage.describe = lambda: bad
        assert "diagnostics" not in graph.snapshot()["nodes"]["source"]

    def raises():
        raise RuntimeError("device gone")

    stage.describe = raises
    json.dumps(graph.snapshot(), allow_nan=False)


@pytest.mark.asyncio
async def test_bus_reports_live_queue_depth_and_catchup_overwrites():
    bus = Bus()
    drop = bus.subscribe("raw", "drop", mode="drop", maxsize=2)
    catchup = bus.subscribe("raw", "catchup", mode="catchup")
    for i in range(520):
        assert await bus.publish("raw", i) == i
    report = bus.report()["raw"]["subscribers"]
    assert report["drop"]["depth"] == 2
    assert report["drop"]["dropped"] == 518
    assert report["catchup"]["depth"] == report["catchup"]["max_depth"] == 512
    assert report["catchup"]["dropped"] == 8
    assert await catchup.get() == 8
    assert bus.report()["raw"]["subscribers"]["catchup"]["depth"] == 511
    assert await drop.get() == 518
    assert bus.report()["raw"]["subscribers"]["drop"]["depth"] == 1
    await catchup._iterator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [True, False])
@pytest.mark.parametrize("outcome", ["empty", "none", "fail", "cancel"])
async def test_in_flight_processing_fanin_and_terminal_cleanup(make_graph, is_async, outcome):
    graph = make_graph(fanin=True)
    entered = asyncio.Queue()
    async_release, sync_release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def result():
        if outcome == "fail":
            raise RuntimeError("processing failed")
        return [] if outcome == "empty" else None

    async def async_process(frame):
        entered.put_nowait(None)
        await async_release.wait()
        return result()

    def sync_process(frame):
        loop.call_soon_threadsafe(entered.put_nowait, None)
        assert sync_release.wait(2)
        return result()

    graph.nodes[2].stage.process = async_process if is_async else sync_process
    assert all(n["in_flight"] == 0 for n in graph.snapshot()["nodes"].values())
    task = asyncio.create_task(graph.run())
    try:
        await asyncio.wait_for(entered.get(), 1)
        await asyncio.wait_for(entered.get(), 1)
        snapshot = graph.snapshot()
        assert snapshot["nodes"]["transform"]["in_flight"] == 2
        assert snapshot["nodes"]["sink"]["in_flight"] == 0
        assert snapshot["nodes"]["source"]["in_flight"] == 0
        assert snapshot["nodes"]["other"]["in_flight"] == 0
        if outcome == "cancel":
            task.cancel()
    finally:
        async_release.set()
        sync_release.set()
    if outcome in {"fail", "cancel"}:
        with pytest.raises(RuntimeError if outcome == "fail" else asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    else:
        await asyncio.wait_for(task, 1)
        assert graph.nodes[-1].stage.received == []
    assert all(n["in_flight"] == 0 for n in graph.snapshot()["nodes"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_in_flight_publish_backpressure_and_cleanup(make_graph, cancel):
    published = asyncio.Event()
    callback_counts = []

    def observe(event):
        if event["kind"] in {"processing", "output"}:
            callback_counts.append(graph.snapshot()["nodes"][event["node"]]["in_flight"])
        if event.get("topic") == "processed":
            published.set()

    graph = make_graph(on_event=observe)
    probe = graph.bus.subscribe("processed", "unread", maxsize=1)
    task = asyncio.create_task(graph.run())
    await asyncio.wait_for(published.wait(), 1)
    assert graph.snapshot()["nodes"]["transform"]["in_flight"] == 1
    assert probe.depth == 1  # the second output is blocked behind the first
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    else:
        async def consume():
            return [item async for item in probe]

        reader = asyncio.create_task(consume())
        await asyncio.wait_for(task, 1)
        assert len(await reader) == 2
    assert all(count > 0 for count in callback_counts)
    assert all(n["in_flight"] == 0 for n in graph.snapshot()["nodes"].values())


@pytest.mark.asyncio
async def test_in_flight_publish_failure_resets_counter(make_graph, monkeypatch):
    graph = make_graph()

    async def fail(topic, payload):
        assert graph.snapshot()["nodes"]["source"]["in_flight"] == 1
        raise RuntimeError("delivery failed")

    monkeypatch.setattr(graph.bus, "publish", fail)
    with pytest.raises(RuntimeError, match="delivery failed"):
        await graph.run()
    assert all(n["in_flight"] == 0 for n in graph.snapshot()["nodes"].values())


@pytest.mark.asyncio
async def test_in_flight_excludes_source_wait_and_input_wait(make_graph):
    graph = make_graph()
    decoding = asyncio.Event()

    async def stream():
        decoding.set()
        await asyncio.Event().wait()
        yield TextFrame("unreachable")

    graph.nodes[0].stage.run = stream
    task = asyncio.create_task(graph.run())
    await asyncio.wait_for(decoding.wait(), 1)
    assert all(n["in_flight"] == 0 for n in graph.snapshot()["nodes"].values())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace", [True, False])
async def test_segment_namespaces_distinguish_branches_preserve_partials_drain_and_passthrough(
    make_graph, namespace
):
    nodes = []
    for side in ("a", "b"):
        nodes.extend([
            NodeConfig(f"source_{side}", "AudioSource", outputs={"out": f"audio_{side}"}),
            NodeConfig(f"segment_{side}", "Segmenter", inputs={"in": f"audio_{side}"},
                       outputs={"out": f"utterance_{side}"}),
        ])
    nodes.extend([
        NodeConfig("pass", "UtterancePass", inputs={"in": "utterance_a", "other": "utterance_b"},
                   outputs={"out": "passed"}),
        NodeConfig("sink", "UtteranceSink", inputs={"in": "passed"}),
    ])
    graph = Graph(GraphConfig(nodes), namespace_segments=namespace)
    for node in graph.nodes:
        if not node.stage.inputs:
            node.stage.frames = [AudioFrame(np.zeros(1, dtype=np.float32))]
    await graph.run()
    frames = graph.nodes[-1].stage.received
    assert len(frames) == 6
    if namespace:
        assert {tuple(json.loads(f.id)) for f in frames} == {
            ("segment_a", "u0001"), ("segment_b", "u0001")}
        for side in ("a", "b"):
            branch = [f for f in frames if json.loads(f.id)[0] == f"segment_{side}"]
            assert [f.is_final for f in branch] == [False, True, True]
            assert len({f.id for f in branch}) == 1
    else:
        assert {f.id for f in frames} == {"u0001"}


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace", [True, False])
@pytest.mark.parametrize("payload_type", ["utterance", "text"])
async def test_direct_source_namespaces_are_stable_and_preserve_other_fields(
    make_graph, namespace, payload_type
):
    source_impl = "UtteranceSource" if payload_type == "utterance" else "Source"
    sink_impl = "UtteranceSink" if payload_type == "utterance" else "Sink"
    graph = Graph(GraphConfig([
        NodeConfig("source_a", source_impl, outputs={"out": "raw"}),
        NodeConfig("source_b", source_impl, outputs={"out": "raw"}),
        NodeConfig("sink", sink_impl, inputs={"in": "raw"}),
    ]), namespace_segments=namespace)
    if payload_type == "utterance":
        partial = Utterance("u0001", np.zeros(1, dtype=np.float32), 1.0, 2.0, False)
    else:
        partial = TextFrame("hello", lang="vi", is_final=False,
                            lineage=Lineage("u0001", t_audio_end=2.0,
                                            stage_latency_ms={"upstream": 4.0}),
                            meta={"detail": "preserved"})
    for node in graph.nodes[:2]:
        node.stage.frames = [partial, replace(partial, is_final=True)]
    await graph.run()
    frames = graph.nodes[-1].stage.received
    ids = [f.id if payload_type == "utterance" else f.segment_id for f in frames]
    assert len(frames) == 4
    assert ids[0] == ids[1] and ids[2] == ids[3]
    assert (ids[0] != ids[2]) is namespace
    if namespace:
        assert [json.loads(i) for i in ids] == [
            ["source_a", "u0001"], ["source_a", "u0001"],
            ["source_b", "u0001"], ["source_b", "u0001"]]
    else:
        assert set(ids) == {"u0001"}
    for frame in frames:
        if payload_type == "utterance":
            assert frame.pcm is partial.pcm
            assert (frame.t_start, frame.t_end) == (1.0, 2.0)
        else:
            assert frame.lineage.t_audio_end == 2.0
            assert frame.lineage.stage_latency_ms == {"upstream": 4.0}
            assert frame.meta == partial.meta and frame.lang == "vi" and frame.text == "hello"
    assert (partial.id if payload_type == "utterance" else partial.segment_id) == "u0001"


@pytest.mark.asyncio
async def test_changed_utterance_id_is_a_new_origin(make_graph):
    graph = Graph(GraphConfig([
        NodeConfig("source", "UtteranceSource", outputs={"out": "raw"}),
        NodeConfig("split", "UtterancePass", inputs={"in": "raw"}, outputs={"out": "split"}),
        NodeConfig("sink", "UtteranceSink", inputs={"in": "split"}),
    ]), namespace_segments=True)
    graph.nodes[0].stage.frames = [Utterance("u0001", np.zeros(1), 1.0, 2.0)]
    graph.nodes[1].stage.process = lambda frame: replace(frame, id="child")
    await graph.run()
    assert json.loads(graph.nodes[-1].stage.received[0].id) == ["split", "child"]

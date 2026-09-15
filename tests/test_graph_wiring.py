"""Graph construction, validation, and the two properties the design exists to provide:
optional stages, and fan-out to several consumers at once.

These run entirely on mock/passthrough adapters -- no model, no microphone, no network.
Validation is port-based: what a topic carries is whatever type the producing module's
output port declares, and the topic's name decides nothing.
"""

from __future__ import annotations

import asyncio

import pytest

from lmls.core.config import ConfigError, GraphConfig, chain_config, load_config
from lmls.core.graph import Graph, GraphError, mermaid, validate
from lmls.core.interfaces import Module
from lmls.core.types import AudioFrame, Lineage, TextFrame, Utterance

pytestmark = pytest.mark.asyncio


def cfg_from(nodes: list[dict]) -> GraphConfig:
    from lmls.core.config import _parse_node

    # Offline replay pushes frames as fast as they are consumed, so drop-oldest would
    # discard most of the recording before the VAD (executor-wrapped per frame) can
    # read it -- corrupting every assertion downstream. The benchmark sets
    # audio_backpressure=block for exactly this reason; the tests replay the same way.
    return GraphConfig(
        nodes=[_parse_node(n, i) for i, n in enumerate(nodes)],
        settings={"audio_backpressure": "block"},
    )


WAV = "assets/lecture.wav"


class StreamingTransformProbe(Module):
    """Consumes the complete input before yielding, as remote stream finalisation can."""

    inputs = {"audio": AudioFrame}
    outputs = {"text": TextFrame}

    async def process_stream(self, frames):
        last = None
        count = 0
        async for _, last in frames:
            count += 1
        assert last is not None
        await asyncio.sleep(0)
        yield TextFrame(
            f"received {count} frames",
            lineage=Lineage(segment_id="stream-1", t_audio_end=last.t_capture),
        )


class MultiOutStreamProbe(Module):
    """Yields (port, payload) tuples, the multi-output streaming convention."""

    inputs = {"audio": AudioFrame}
    outputs = {"text": TextFrame, "side": TextFrame}

    def __init__(self, bad=None):
        super().__init__()
        self.bad = bad  # None | "dict" | "bare" | "undeclared"

    async def process_stream(self, frames):
        count = 0
        async for _, _frame in frames:
            count += 1
        if self.bad == "dict":
            yield {"text": TextFrame("dicted", lineage=Lineage.new("s1"))}
            return
        if self.bad == "bare":
            yield TextFrame("bare", lineage=Lineage.new("s1"))
            return
        if self.bad == "undeclared":
            yield "implicit", TextFrame("nope", lineage=Lineage.new("s1"))
            return
        yield "text", TextFrame(f"main {count}", lineage=Lineage(segment_id="s1"))
        yield ("side", TextFrame("side", lineage=Lineage(segment_id="s1")))


def full_pipeline(**overrides) -> list[dict]:
    return [
        {"name": "src", "impl": "wav", "out": "audio.raw",
         "path": WAV, "realtime": False},
        {"name": "nr", "impl": "passthrough_denoiser",
         "in": "audio.raw", "out": "audio.clean"},
        {"name": "vad", "impl": "energy",
         "in": "audio.clean", "out": "utterance.speech"},
        {"name": "asr", "impl": "mock_transcriber",
         "in": "utterance.speech", "out": "text.raw", "delay_ms": 0},
        {"name": "fix", "impl": "rules",
         "in": "text.raw", "out": "text.corrected"},
        {"name": "vi", "impl": "mock_translator",
         "in": "text.corrected", "out": {"text_out": "text.out"},
         "target": "vi", "delay_ms": 0},
        {"name": "sink", "impl": "jsonl",
         "in": {
             "raw": "text.raw",
             "corrected": "text.corrected",
             "translated": "text.out",
         }},
    ]


# -- validation --------------------------------------------------------------


def test_valid_pipeline_passes_validation():
    assert validate(cfg_from(full_pipeline())) == []


def test_subscribing_to_a_topic_nobody_publishes_is_an_error():
    nodes = full_pipeline()
    nodes[3]["in"] = "utterance.typo"
    with pytest.raises(GraphError, match="nothing publishes it"):
        validate(cfg_from(nodes))


def test_publishing_where_nobody_listens_is_only_a_warning():
    """A sink you removed should not stop the pipeline from running."""
    nodes = full_pipeline()[:-1]
    warnings = validate(cfg_from(nodes))
    assert any("text.out" in w for w in warnings)


def test_a_text_stage_cannot_read_an_audio_topic():
    """Type checking, not prefix checking: the corrector's port expects a TextFrame."""
    nodes = full_pipeline()
    nodes[4]["in"] = "audio.clean"
    with pytest.raises(GraphError, match="expects TextFrame"):
        validate(cfg_from(nodes))


def test_a_port_name_not_declared_by_the_module_is_an_error():
    nodes = full_pipeline()
    nodes[4]["in"] = {"wrong_port": "text.raw"}
    with pytest.raises(ConfigError, match="not declared by RuleCorrector"):
        cfg_from(nodes)


def test_an_input_list_cannot_create_implicit_ports():
    nodes = full_pipeline()
    nodes[4]["in"] = ["text.raw", "text.corrected"]
    with pytest.raises(ConfigError, match="each port accepts exactly one topic"):
        cfg_from(nodes)


def test_validation_rejects_an_implicit_input_port_when_parsing_is_bypassed():
    cfg = cfg_from(full_pipeline())
    cfg.node("fix").inputs = {"implicit": "text.raw"}
    with pytest.raises(GraphError, match="not declared by RuleCorrector"):
        validate(cfg)


def test_a_topic_cannot_have_two_publishers():
    nodes = full_pipeline()
    # A second output port cannot merge into the ASR publisher's topic, regardless of
    # whether its payload type happens to match.
    nodes.insert(
        5,
        {"name": "bogus", "impl": "energy",
         "in": "audio.raw", "out": {"utterance": "text.raw"}},
    )
    with pytest.raises(GraphError, match="more than one publisher"):
        validate(cfg_from(nodes))


def test_two_output_ports_cannot_publish_the_same_topic():
    nodes = full_pipeline()
    nodes[5]["out"] = {
        "text_out": "text.out",
        "corrected": "text.out",
    }
    with pytest.raises(GraphError, match="more than one publisher"):
        validate(cfg_from(nodes))


async def test_multi_output_module_cannot_return_a_bare_payload():
    graph = Graph(cfg_from(full_pipeline()))
    translator = next(n for n in graph.nodes if n.config.name == "vi")
    frame = TextFrame("hello", lineage=Lineage.new("segment"))
    with pytest.raises(GraphError, match="must return a dict keyed by declared"):
        await graph._publish_result(translator, frame, frame, 0.0)


async def test_module_cannot_return_an_undeclared_output_port():
    graph = Graph(cfg_from(full_pipeline()))
    translator = next(n for n in graph.nodes if n.config.name == "vi")
    frame = TextFrame("hello", lineage=Lineage.new("segment"))
    with pytest.raises(GraphError, match="undeclared output port 'implicit'"):
        await graph._publish_result(translator, {"implicit": frame}, frame, 0.0)


class CycleProbe(Module):
    """Two-input passthrough so a graph can close a loop with one publisher per topic."""

    inputs = {"in": TextFrame, "feedback": TextFrame}
    outputs = {"out": TextFrame}

    async def process(self, port, frame):
        return frame


async def test_cycles_are_rejected(monkeypatch):
    """Every topic has exactly one publisher, so the loop closes through a node that
    also consumes its own output: the cycle check is what catches that."""
    from lmls.core.registry import REGISTRY

    monkeypatch.setitem(REGISTRY, "cycle_probe", f"{__name__}:CycleProbe")
    nodes = [
        {"name": "src", "impl": "wav", "out": "audio.raw", "path": WAV},
        {"name": "vad", "impl": "energy", "in": "audio.raw", "out": "utterance.speech"},
        {"name": "asr", "impl": "mock_transcriber",
         "in": "utterance.speech", "out": "text.raw"},
        {"name": "fix", "impl": "rules", "in": "text.raw", "out": "text.corrected"},
        {"name": "vi", "impl": "mock_translator",
         "in": "text.corrected", "out": {"text_out": "text.vi"},
         "target": "vi", "delay_ms": 0},
        {"name": "loop", "impl": "cycle_probe",
         "in": {"in": "text.vi", "feedback": "text.loop"},  # <- feeds itself
         "out": "text.loop"},
        {"name": "sink", "impl": "jsonl",
         "in": {"corrected": "text.corrected"}},
    ]
    with pytest.raises(GraphError, match="cycle"):
        validate(cfg_from(nodes))


def test_duplicate_node_names_are_rejected():
    nodes = full_pipeline()
    nodes[1]["name"] = "asr"
    with pytest.raises(GraphError, match="duplicate node names"):
        validate(cfg_from(nodes))


def test_a_graph_needs_a_source_node():
    with pytest.raises(GraphError, match="no source node"):
        validate(cfg_from(full_pipeline()[1:]))


def test_a_transform_with_no_input_wiring_is_an_error():
    nodes = full_pipeline()
    del nodes[4]["in"]
    with pytest.raises(GraphError, match="needs an input topic"):
        validate(cfg_from(nodes))


def test_a_source_cannot_have_inputs():
    nodes = full_pipeline()
    nodes[0]["in"] = "audio.clean"
    with pytest.raises(ConfigError, match="declares none"):
        cfg_from(nodes)


def test_an_out_list_can_skip_a_port_with_underscore():
    """A "_" entry in list-form `out` leaves that declared port unwired (spec §10.2).

    MockTranslator declares {"text_out", "corrected"} in that order, so
    ["_", "text.corrected"] must wire only the corrected port; the translator
    simply publishes nothing on text_out.
    """
    nodes = [n for n in full_pipeline() if n["name"] != "fix"]
    # vi now publishes text.corrected, so it must stop reading it (self-cycle):
    nodes[4]["in"] = "text.raw"
    nodes[4]["out"] = ["_", "text.corrected"]
    nodes[5]["in"] = {"raw": "text.raw", "corrected": "text.corrected"}
    cfg = cfg_from(nodes)
    assert cfg.node("vi").outputs == {"corrected": "text.corrected"}
    assert "text.out" not in cfg.node("vi").out_topics
    assert validate(cfg) == []


def test_an_out_table_cannot_use_underscore_as_a_topic():
    nodes = full_pipeline()
    nodes[5]["out"] = {"corrected": "_"}
    with pytest.raises(ConfigError, match="'_' is not a topic name"):
        cfg_from(nodes)


def test_an_out_string_cannot_be_an_underscore():
    nodes = full_pipeline()
    nodes[4]["out"] = "_"  # RuleCorrector: single output port
    with pytest.raises(ConfigError, match="'_' is not a topic name"):
        cfg_from(nodes)


async def test_an_all_underscore_out_list_publishes_nothing():
    """A transform with every output port skipped is valid and simply publishes nothing."""
    nodes = full_pipeline()
    nodes[5]["out"] = ["_", "_"]
    nodes[6]["in"] = {"raw": "text.raw", "corrected": "text.corrected"}
    assert validate(cfg_from(nodes)) == []
    graph = Graph(cfg_from(nodes))
    await graph.run(timeout=20)
    assert "text.out" not in graph.bus.report()
    assert graph.metrics.counts.get("vi", 0) == 0  # translations were dropped


async def test_a_bare_result_with_no_wired_output_publishes_nothing():
    """A transcription stage whose output port is never wired (or skipped with "_")
    must not fail the run: like a named result for an unwired port, its results are
    discarded, and a one-time warning explains where the subtitles went."""
    nodes = [
        {"name": "src", "impl": "wav", "out": "audio.raw",
         "path": WAV, "realtime": False},
        {"name": "vad", "impl": "energy", "in": "audio.raw", "out": "utterance.speech"},
        {"name": "asr", "impl": "mock_transcriber",
         "in": "utterance.speech", "delay_ms": 0},  # no "out" wired at all
    ]
    cfg = cfg_from(nodes)
    assert validate(cfg) == []
    graph = Graph(cfg)
    await graph.run(timeout=20)
    assert graph.metrics.stage_counts.get("asr", 0) > 0  # it ran, and dropped
    assert graph.metrics.counts.get("en", 0) == 0  # nothing was published
    assert "text.raw" not in graph.bus.report()


def test_a_source_with_no_wired_output_is_an_error():
    """A source cannot skip its ports: it would decode audio for nobody and fail
    later with an opaque IndexError instead of this one-line startup error."""
    nodes = full_pipeline()
    del nodes[0]["out"]
    with pytest.raises(GraphError, match="wires none"):
        validate(cfg_from(nodes))


async def test_a_skipped_out_port_publishes_nothing():
    """repair_mode builds frames for both ports every call; the skipped port's
    frames are dropped before the bus sees them."""
    nodes = [n for n in full_pipeline() if n["name"] != "fix"]
    nodes[4]["out"] = ["_", "text.corrected"]
    nodes[4]["in"] = "text.raw"  # vi publishes text.corrected now; reading it = cycle
    nodes[4]["repair_mode"] = True
    nodes[5]["in"] = {"raw": "text.raw", "corrected": "text.corrected"}
    graph = Graph(cfg_from(nodes))
    await graph.run(timeout=20)
    assert graph.metrics.counts.get("en", 0) > 0  # corrected English landed
    assert graph.metrics.counts.get("vi", 0) == 0  # translations were dropped
    assert "text.out" not in graph.bus.report()


# -- optional stages ---------------------------------------------------------


async def test_pipeline_runs_without_the_denoise_stage():
    """Delete the node, repoint the topic. Nothing else changes."""
    nodes = [n for n in full_pipeline() if n["name"] != "nr"]
    nodes[1]["in"] = "audio.raw"
    graph = Graph(cfg_from(nodes))
    await graph.run(timeout=20)
    assert graph.metrics.counts.get("vi", 0) > 0


async def test_pipeline_runs_without_the_correction_stage():
    """No corrector anywhere -- the translator is wired straight to raw ASR text and
    repairs while translating (repair_mode), publishing corrected English *and* a
    translation from one process() call."""
    nodes = [n for n in full_pipeline() if n["name"] != "fix"]
    translator = next(n for n in nodes if n["name"] == "vi")
    translator["in"] = "text.raw"
    translator["out"] = {"corrected": "text.corrected", "text_out": "text.out"}
    translator["repair_mode"] = True

    graph = Graph(cfg_from(nodes))
    await graph.run(timeout=20)

    assert not any(n.config.impl.endswith("_corrector") for n in graph.nodes)
    # Corrected English still reaches the display, produced by the translator.
    assert graph.metrics.counts.get("en", 0) > 0
    assert graph.metrics.counts.get("vi", 0) > 0


async def test_minimal_pipeline_is_just_source_vad_asr_translate():
    nodes = [
        {"name": "src", "impl": "wav", "out": "audio.raw",
         "path": WAV, "realtime": False},
        {"name": "vad", "impl": "energy", "in": "audio.raw", "out": "utterance.speech"},
        {"name": "asr", "impl": "mock_transcriber",
         "in": "utterance.speech", "out": "text.raw", "delay_ms": 0},
        {"name": "vi", "impl": "mock_translator",
         "in": "text.raw", "out": {"text_out": "text.out"},
         "target": "vi", "delay_ms": 0},
        {"name": "sink", "impl": "jsonl", "in": {"translated": "text.out"}},
    ]
    graph = Graph(cfg_from(nodes))
    await graph.run(timeout=20)
    assert graph.metrics.counts.get("vi", 0) > 0


async def test_streaming_transform_can_emit_after_its_input_stream_ends(monkeypatch):
    """Full-duplex providers are allowed to deliver final text after the last audio
    frame; the graph must keep the output side alive long enough to publish it."""
    from lmls.core.registry import REGISTRY

    monkeypatch.setitem(
        REGISTRY,
        "test_streaming_transform",
        f"{__name__}:StreamingTransformProbe",
    )
    nodes = [
        {"name": "src", "impl": "wav", "out": "audio.raw",
         "path": WAV, "realtime": False},
        {"name": "asr", "impl": "test_streaming_transform",
         "in": "audio.raw", "out": "text.raw"},
        {"name": "sink", "impl": "collect", "in": {"raw": "text.raw"}},
    ]
    graph = Graph(cfg_from(nodes))
    await graph.run(timeout=20)

    collector = next(n.stage for n in graph.nodes if n.config.impl == "collect")
    assert len(collector.events) == 1
    assert collector.events[0].text.startswith("received ")
    assert collector.events[0].is_final
    assert graph.metrics.stage_counts["asr"] == 1


async def test_multi_output_stream_routes_tuples_to_their_ports(monkeypatch):
    """A streaming transform with several outputs must yield (port, payload) tuples;
    each tuple is routed to the topic bound for that port."""
    from lmls.core.registry import REGISTRY

    monkeypatch.setitem(REGISTRY, "multi_stream", f"{__name__}:MultiOutStreamProbe")
    nodes = [
        {"name": "src", "impl": "wav", "out": "audio.raw",
         "path": WAV, "realtime": False},
        {"name": "asr", "impl": "multi_stream", "in": "audio.raw",
         "out": {"text": "text.raw", "side": "text.side"}},
        {"name": "sink", "impl": "collect",
         "in": {"raw": "text.raw", "translated_secondary": "text.side"}},
    ]
    graph = Graph(cfg_from(nodes))
    await graph.run(timeout=20)

    collector = next(n.stage for n in graph.nodes if n.config.impl == "collect")
    main = [e for e in collector.events if e.text.startswith("main ")]
    side = [e for e in collector.events if e.text == "side"]
    assert len(main) == 1 and len(side) == 1


@pytest.mark.parametrize("bad", ["dict", "bare", "undeclared"])
async def test_multi_output_stream_rejects_non_tuple_yields(monkeypatch, bad):
    """dict yields, bare payloads and undeclared port names are convention errors,
    not silent misrouting."""
    from lmls.core.registry import REGISTRY

    monkeypatch.setitem(REGISTRY, "multi_stream", f"{__name__}:MultiOutStreamProbe")
    nodes = [
        {"name": "src", "impl": "wav", "out": "audio.raw",
         "path": WAV, "realtime": False},
        {"name": "asr", "impl": "multi_stream", "in": "audio.raw",
         "out": {"text": "text.raw", "side": "text.side"}},
        {"name": "sink", "impl": "collect", "in": {"raw": "text.raw"}},
    ]
    graph = Graph(cfg_from(nodes))
    next(n.stage for n in graph.nodes if n.config.name == "asr").bad = bad
    with pytest.raises(GraphError, match="process_stream"):
        await graph.run(timeout=20)


# -- fan-out -----------------------------------------------------------------


async def test_one_topic_feeds_several_consumers():
    """text.corrected is read by the translator AND two sinks, each independently."""
    nodes = full_pipeline()
    nodes.append({"name": "sink2", "impl": "jsonl",
                  "in": {"corrected": "text.corrected"}})
    graph = Graph(cfg_from(nodes))
    assert sorted(graph.bus.subscribers_of("text.corrected")) == [
        "sink", "sink2", "vi"
    ]
    await graph.run(timeout=20)
    report = graph.bus.report()["text.corrected"]
    counts = {name: s["received"] for name, s in report["subscribers"].items()}
    assert counts["vi"] == counts["sink"] == counts["sink2"] > 0


async def test_two_translators_share_one_correction_pass():
    """Adding a language is adding a node, not changing code."""
    nodes = full_pipeline()
    nodes.append({"name": "zh", "impl": "mock_translator",
                  "in": "text.corrected", "out": {"text_out": "text.zh"},
                  "target": "zh", "delay_ms": 0})
    nodes.append({"name": "probe", "impl": "collect",
                  "in": {
                      "raw": "text.raw",
                      "corrected": "text.corrected",
                      "translated": "text.out",
                      "translated_secondary": "text.zh",
                  }})

    graph = Graph(cfg_from(nodes))
    await graph.run(timeout=20)

    collector = next(n.stage for n in graph.nodes if n.config.impl == "collect")
    # One correction feeds two translations: the correction was not done twice. The
    # English finals (raw revised into corrected) are the shared upstream.
    finals = collector.finals()
    by_lang: dict[str, int] = {}
    for e in finals:
        by_lang[e.lang] = by_lang.get(e.lang, 0) + 1
    assert by_lang.get("vi", 0) == by_lang.get("zh", 0) > 0
    assert by_lang.get("en", 0) == by_lang.get("vi", 0), (
        "each segment must reach both languages exactly once"
    )


async def test_a_slow_consumer_does_not_delay_a_fast_one_in_a_real_graph():
    """The bus guarantee, exercised through the actual graph rather than in isolation."""
    nodes = full_pipeline()
    graph = Graph(cfg_from(nodes))

    translator = next(n for n in graph.nodes if n.config.name == "vi")
    sink = next(n for n in graph.nodes if n.config.name == "sink")

    order: list[str] = []
    real_translate = translator.stage.process
    real_process = sink.stage.process

    async def slow_process(port, frame):
        await asyncio.sleep(0.25)
        order.append(f"translate:{frame.segment_id}")
        return await real_process(port, frame)

    async def fast_emit(port, frame):
        if frame.lang == "en":
            order.append(f"display:{frame.segment_id}")
        return await real_process(port, frame)

    translator.stage.process = slow_process  # type: ignore[method-assign]
    sink.stage.process = fast_emit  # type: ignore[method-assign]

    await graph.run(timeout=30)

    first_display = next(i for i, x in enumerate(order) if x.startswith("display:"))
    first_translate = next(i for i, x in enumerate(order) if x.startswith("translate:"))
    assert first_display < first_translate, (
        "the display waited for the slow translator; fan-out isolation is broken"
    )


# -- routing by port ---------------------------------------------------------


async def test_repair_mode_translator_publishes_to_both_ports():
    nodes = [n for n in full_pipeline() if n["name"] != "fix"]
    translator = next(n for n in nodes if n["name"] == "vi")
    translator["in"] = "text.raw"
    translator["out"] = {"corrected": "text.corrected", "text_out": "text.out"}
    translator["repair_mode"] = True

    graph = Graph(cfg_from(nodes))
    en = graph.bus.subscribe("text.corrected", "probe_en")
    vi = graph.bus.subscribe("text.out", "probe_vi")
    await graph.run(timeout=20)

    def _frames(sub) -> list[TextFrame]:
        out = []
        while sub.queue.qsize():
            item = sub.queue.get_nowait()
            if isinstance(item, TextFrame):  # skip the end-of-stream sentinel
                out.append(item)
        return out

    en_events = _frames(en)
    vi_events = _frames(vi)
    assert en_events and all(e.lang == "en" for e in en_events)
    assert vi_events and all(e.lang == "vi" for e in vi_events)


async def test_every_subscriber_sees_the_same_revision_for_a_frame():
    """The bus stamps the revision before fan-out, so no subscriber can see a version
    another subscriber missed."""
    nodes = full_pipeline()
    graph = Graph(cfg_from(nodes))
    a = graph.bus.subscribe("text.corrected", "probe_a")
    b = graph.bus.subscribe("text.corrected", "probe_b")
    await graph.run(timeout=20)

    got_a = [e async for e in a]
    got_b = [e async for e in b]
    assert got_a and [(e.segment_id, e.revision) for e in got_a] == [
        (e.segment_id, e.revision) for e in got_b
    ]
    # Per segment, revisions are a gapless sequence starting at 0: bus-assigned.
    by_seg: dict[str, list[int]] = {}
    for e in got_a:
        by_seg.setdefault(e.segment_id, []).append(e.revision)
    assert all(revs == list(range(len(revs))) for revs in by_seg.values())


# -- config forms ------------------------------------------------------------


def test_chain_shorthand_skips_omitted_stages():
    cfg = chain_config(
        ["mic", "segment", "asr", "translate"],
        overrides={"asr": "mock_transcriber", "translate": "mock_translator"},
    )
    assert [n.impl for n in cfg.nodes] == [
        "mic", "energy", "mock_transcriber", "mock_translator", "stdout_pretty"
    ]
    # With no corrector, the translator reads the ASR topic directly.
    assert cfg.node("translate").inputs == {"text_in": "text.raw"}
    assert cfg.nodes[-1].inputs == {
        "raw": "text.raw",
        "translated": "text.out",
    }
    validate(cfg)


def test_chain_shorthand_wires_the_full_pipeline():
    cfg = chain_config(
        ["mic", "segment", "asr", "correct", "translate"],
        overrides={"asr": "mock_transcriber",
                   "correct": "rules",
                   "translate": "mock_translator"},
    )
    assert cfg.node("asr").inputs == {"audio": "utterance.speech"}
    assert cfg.node("translate").inputs == {"text_in": "text.corrected"}
    assert cfg.nodes[-1].inputs == {
        "raw": "text.raw",
        "corrected": "text.corrected",
        "translated": "text.out",
    }
    validate(cfg)


def test_unknown_chain_stage_is_rejected():
    with pytest.raises(ConfigError, match="unknown chain stage"):
        chain_config(["mic", "magic"])


@pytest.mark.parametrize(
    "preset",
    ["default", "mlx", "mock", "with_denoise", "no_correct", "fused", "bilingual", "video",
     "deepgram"],
)
def test_every_shipped_preset_is_a_valid_wiring(preset):
    """Presets are documentation. A preset that does not validate is a broken claim."""
    cfg = load_config(f"config/{preset}.toml")
    validate(cfg)
    assert mermaid(cfg).startswith("flowchart")


async def test_run_does_not_start_stages_a_driver_already_started():
    """``lmls demo`` starts every stage itself -- to warm the models before playback --
    and then calls ``graph.run()``. Stages hold resources (the websocket sink binds a
    port; a second bind is EADDRINUSE even with no other process present), so each stage
    must be started exactly once no matter how many drivers ask."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    graph = Graph(
        cfg_from([
            {"name": "src", "impl": "wav", "out": "audio.raw",
             "path": WAV, "realtime": False},
            {"name": "vad", "impl": "energy", "in": "audio.raw",
             "out": "utterance.speech"},
            {"name": "asr", "impl": "mock_transcriber",
             "in": "utterance.speech", "out": "text.raw"},
            {"name": "ws", "impl": "websocket_server", "in": {"raw": "text.raw"},
             "port": port},
        ])
    )

    # The shipped sink defends itself, so pin the contract on a stage that does not:
    # a double start must be visible even if some impl's start() happens to be idempotent.
    # The counter is installed BEFORE the pre-start, so it counts the driver's start and
    # any start() that graph.run() adds on top.
    sink = next(n.stage for n in graph.nodes if n.config.impl == "websocket_server")
    real_start = sink.start
    starts = 0

    async def counted_start() -> None:
        nonlocal starts
        starts += 1
        assert starts == 1, f"stage started {starts} times; run() restarted it"
        await real_start()

    sink.start = counted_start

    await graph.start()  # the driver contract: what lmls demo does
    await graph.run(timeout=5.0)
    assert starts == 1


async def test_segmenter_drain_flushes_the_final_utterance():
    """When the audio ends mid-sentence, the graph must ask the segmenter for whatever
    is still buffered before closing the utterance topic."""
    nodes = [
        {"name": "src", "impl": "wav", "out": "audio.raw",
         "path": WAV, "realtime": False},
        {"name": "vad", "impl": "energy", "in": "audio.raw", "out": "utterance.speech"},
        {"name": "asr", "impl": "mock_transcriber",
         "in": "utterance.speech", "out": "text.raw", "delay_ms": 0},
        {"name": "sink", "impl": "collect", "in": {"raw": "text.raw"}},
    ]
    graph = Graph(cfg_from(nodes))
    await graph.run(timeout=20)
    collector = next(n.stage for n in graph.nodes if n.config.impl == "collect")
    # The fixture's last sentence has no trailing silence; only drain() can flush it.
    assert len(collector.events) >= 8

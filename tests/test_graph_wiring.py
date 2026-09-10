"""Graph construction, validation, and the two properties the design exists to provide:
optional stages, and fan-out to several consumers at once.

These run entirely on mock/passthrough adapters -- no model, no microphone, no network.
Validation is port-based: what a topic carries is whatever type the producing module's
output port declares, and the topic's name decides nothing.
"""

from __future__ import annotations

import asyncio

import pytest

from livesub.core.config import ConfigError, GraphConfig, chain_config, load_config
from livesub.core.graph import Graph, GraphError, mermaid, validate
from livesub.core.types import AudioFrame, TextFrame, Utterance

pytestmark = pytest.mark.asyncio


def cfg_from(nodes: list[dict]) -> GraphConfig:
    from livesub.core.config import _parse_node

    # Offline replay pushes frames as fast as they are consumed, so drop-oldest would
    # discard most of the recording before the VAD (executor-wrapped per frame) can
    # read it -- corrupting every assertion downstream. The benchmark sets
    # audio_backpressure=block for exactly this reason; the tests replay the same way.
    return GraphConfig(
        nodes=[_parse_node(n, i) for i, n in enumerate(nodes)],
        settings={"audio_backpressure": "block"},
    )


WAV = "assets/lecture.wav"


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
         "in": ["text.raw", "text.corrected", "text.out"]},
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


def test_a_topic_cannot_carry_two_types():
    nodes = full_pipeline()
    # The "wrong" translator publishes Utterance onto a topic the translator reads as
    # text -- a segmenter output wired into a text topic.
    nodes.insert(
        5,
        {"name": "bogus", "impl": "energy",
         "in": "audio.raw", "out": {"utterance": "text.raw"}},
    )
    with pytest.raises(GraphError, match="conflicting types"):
        validate(cfg_from(nodes))


def test_cycles_are_rejected():
    """Every topic has a publisher and a subscriber, so only the cycle check catches it:
    the translator feeds its output back into the topic the corrector reads."""
    nodes = [
        {"name": "src", "impl": "wav", "out": "audio.raw", "path": WAV},
        {"name": "vad", "impl": "energy", "in": "audio.raw", "out": "utterance.speech"},
        {"name": "asr", "impl": "mock_transcriber",
         "in": "utterance.speech", "out": "text.raw"},
        {"name": "fix", "impl": "rules", "in": "text.raw", "out": "text.corrected"},
        {"name": "vi", "impl": "mock_translator",
         "in": "text.corrected", "out": {"text_out": "text.raw"},  # <- back into fix
         "target": "vi", "delay_ms": 0},
        {"name": "sink", "impl": "jsonl", "in": ["text.corrected"]},
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
    nodes = full_pipeline()
    nodes[5]["out"] = ["_", "text.corrected"]
    # vi now publishes text.corrected, so it must stop reading it (self-cycle):
    nodes[5]["in"] = "text.raw"
    nodes[6]["in"] = ["text.raw", "text.corrected"]  # text.out is no longer produced
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
    nodes[6]["in"] = ["text.raw", "text.corrected"]  # text.out is no longer produced
    assert validate(cfg_from(nodes)) == []
    graph = Graph(cfg_from(nodes))
    await graph.run(timeout=20)
    assert "text.out" not in graph.bus.report()
    assert graph.metrics.counts.get("vi", 0) == 0  # translations were dropped


async def test_a_skipped_out_port_publishes_nothing():
    """repair_mode builds frames for both ports every call; the skipped port's
    frames are dropped before the bus sees them."""
    nodes = full_pipeline()
    nodes[5]["out"] = ["_", "text.corrected"]
    nodes[5]["in"] = "text.raw"  # vi publishes text.corrected now; reading it = cycle
    nodes[5]["repair_mode"] = True
    nodes[6]["in"] = ["text.raw", "text.corrected"]
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
        {"name": "sink", "impl": "jsonl", "in": ["text.out"]},
    ]
    graph = Graph(cfg_from(nodes))
    await graph.run(timeout=20)
    assert graph.metrics.counts.get("vi", 0) > 0


# -- fan-out -----------------------------------------------------------------


async def test_one_topic_feeds_several_consumers():
    """text.corrected is read by the translator AND two sinks, each independently."""
    nodes = full_pipeline()
    nodes.append({"name": "sink2", "impl": "jsonl", "in": ["text.corrected"]})
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
                  "in": ["text.raw", "text.corrected", "text.out", "text.zh"]})

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

    async def slow_process(frame):
        await asyncio.sleep(0.25)
        order.append(f"translate:{frame.segment_id}")
        return await real_process(frame)

    async def fast_emit(frame):
        if frame.lang == "en":
            order.append(f"display:{frame.segment_id}")
        return await real_process(frame)

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
    validate(cfg)


def test_unknown_chain_stage_is_rejected():
    with pytest.raises(ConfigError, match="unknown chain stage"):
        chain_config(["mic", "magic"])


@pytest.mark.parametrize(
    "preset",
    ["default", "mlx", "mock", "with_denoise", "no_correct", "fused", "bilingual", "video"],
)
def test_every_shipped_preset_is_a_valid_wiring(preset):
    """Presets are documentation. A preset that does not validate is a broken claim."""
    cfg = load_config(f"config/{preset}.toml")
    validate(cfg)
    assert mermaid(cfg).startswith("flowchart")


async def test_run_does_not_start_stages_a_driver_already_started():
    """``livesub demo`` starts every stage itself -- to warm the models before playback --
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
            {"name": "ws", "impl": "websocket_server", "in": ["text.raw"],
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

    await graph.start()  # the driver contract: what livesub demo does
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
        {"name": "sink", "impl": "collect", "in": ["text.raw"]},
    ]
    graph = Graph(cfg_from(nodes))
    await graph.run(timeout=20)
    collector = next(n.stage for n in graph.nodes if n.config.impl == "collect")
    # The fixture's last sentence has no trailing silence; only drain() can flush it.
    assert len(collector.events) >= 8

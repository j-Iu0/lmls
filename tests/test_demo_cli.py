from __future__ import annotations

from typer.testing import CliRunner

import livesub.cli.demo as demo_module
from livesub.cli.__main__ import app
from livesub.cli.demo import (
    BackendChoice,
    BufferChoice,
    SourceChoice,
    _demo_config,
    _languages,
    _selected_model,
)
from livesub.core.graph import Graph


def build_demo(**overrides):
    options = {
        "source": SourceChoice.ffmpeg,
        "source_input": "lecture.mp4",
        "ffmpeg_device": False,
        "backend": BackendChoice.whisper_ollama,
        "languages": ["vi"],
        "buffer": BufferChoice.live,
    }
    options.update(overrides)
    return _demo_config(**options)


def test_demo_help_exposes_choices_but_not_graph_config():
    result = CliRunner().invoke(app, ["demo", "--help"])

    assert result.exit_code == 0
    assert "--source" in result.output
    assert "cuda" in result.output
    assert "--buffer-mode" in result.output
    assert "--model" in result.output
    assert "--config" not in result.output


def test_top_level_version_option():
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == "livesub 0.1.0"

    demo_result = CliRunner().invoke(app, ["demo", "--version"])
    assert demo_result.exit_code == 0
    assert demo_result.output.strip() == "livesub 0.1.0"


def test_demo_graph_is_built_in_and_supports_multiple_languages():
    cfg = build_demo(languages=["vi", "zh"])
    Graph(cfg)  # full graph validation and construction

    assert cfg.source_path is None
    assert cfg.settings["languages"] == ["vi", "zh"]
    translators = [n for n in cfg.nodes if n.name.startswith("translate_")]
    assert [n.options["target"] for n in translators] == ["vi", "zh"]
    assert all(n.inputs == {"text_in": "text.corrected"} for n in translators)


def test_demo_backend_variants():
    mlx = build_demo(backend=BackendChoice.mlx)
    portable = build_demo(backend=BackendChoice.whisper_ollama)
    cuda = build_demo(backend=BackendChoice.cuda)

    assert mlx.node("asr").impl == "mlx_whisper"
    assert mlx.node("fix").impl == "mlx_llm_corrector"
    assert portable.node("asr").options["device"] == "auto"
    assert portable.node("fix").impl == "ollama_corrector"
    assert cuda.node("asr").options == {
        "model": "small.en", "device": "cuda", "compute_type": "float16"
    }
    assert cuda.node("fix").impl == "ollama_corrector"


def test_model_selection_is_applied_to_every_language_stage():
    cfg = build_demo(llm_model="my-model")

    language_nodes = [
        node for node in cfg.nodes
        if node.name == "fix" or node.name.startswith("translate_")
    ]
    assert {node.options["model"] for node in language_nodes} == {"my-model"}


def test_unsupported_model_selection_is_ignored_with_a_warning(monkeypatch, capsys):
    monkeypatch.setattr(demo_module, "_MODEL_SELECTABLE_BACKENDS", frozenset())

    assert _selected_model(BackendChoice.cuda, "unsupported-model") is None
    warning = capsys.readouterr().err
    assert "does not support model selection" in warning
    assert "ignoring --model 'unsupported-model'" in warning


def test_demo_buffer_mode_never_uses_catchup():
    live = build_demo(buffer=BufferChoice.live)
    block = build_demo(buffer=BufferChoice.block)

    assert {n.mode for n in live.nodes if n.inputs} == {"live"}
    assert {n.mode for n in block.nodes if n.inputs} == {"blocking"}
    assert all(n.mode != "catchup" for n in live.nodes + block.nodes)


def test_language_flags_are_repeatable_comma_aware_and_deduplicated():
    assert _languages(["vi, zh", "vi", "FR"]) == ["vi", "zh", "fr"]

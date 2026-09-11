from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

import lmls.cli.demo as demo_module
from lmls.cli.__main__ import app
from lmls.cli.demo import (
    BackendChoice,
    BufferChoice,
    SourceChoice,
    _demo_config,
    _language,
    _play_ffmpeg_input,
    _selected_model,
    _startup_detail,
)
from lmls.core.graph import Graph
from lmls.core.startup import StartupEvent, StartupPhase, StartupProgress


def build_demo(**overrides):
    options = {
        "source": SourceChoice.ffmpeg,
        "source_input": "lecture.mp4",
        "ffmpeg_device": False,
        "backend": BackendChoice.whisper_ollama,
        "target": "vi",
        "buffer": BufferChoice.drop,
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
    assert result.output.strip() == "lmls 0.1.0"

    demo_result = CliRunner().invoke(app, ["demo", "--version"])
    assert demo_result.exit_code == 0
    assert demo_result.output.strip() == "lmls 0.1.0"


def test_demo_graph_is_built_in_with_a_single_target():
    cfg = build_demo()
    Graph(cfg)  # full graph validation and construction

    assert cfg.source_path is None
    assert cfg.settings["target"] == "vi"
    fused = cfg.node("fix_translate")
    assert fused.impl == "fused_ollama"
    assert fused.inputs == {"text_in": "text.raw"}
    assert fused.outputs == {
        "corrected": "text.corrected", "translated": "text.translation"
    }
    assert fused.options["target"] == "vi"
    assert not [n for n in cfg.nodes if n.name.startswith("translate_")]


def test_demo_backend_variants():
    mlx = build_demo(backend=BackendChoice.mlx)
    portable = build_demo(backend=BackendChoice.whisper_ollama)
    cuda = build_demo(backend=BackendChoice.cuda)

    assert mlx.node("asr").impl == "mlx_whisper"
    assert mlx.node("fix_translate").impl == "fused_llm"
    assert mlx.node("fix_translate").options["model"] == "mlx-community/Qwen3.5-4B-4bit"
    assert mlx.node("fix_translate").options["target"] == "vi"
    assert portable.node("asr").options["device"] == "auto"
    assert portable.node("fix_translate").impl == "fused_ollama"
    assert cuda.node("asr").options == {
        "model": "small.en", "device": "cuda", "compute_type": "float16"
    }
    assert cuda.node("fix_translate").impl == "fused_ollama"


def test_model_selection_is_applied_to_the_fused_stage():
    cfg = build_demo(llm_model="my-model")

    assert cfg.node("fix_translate").options["model"] == "my-model"


def test_unsupported_model_selection_is_ignored_with_a_warning(monkeypatch, capsys):
    monkeypatch.setattr(demo_module, "_MODEL_SELECTABLE_BACKENDS", frozenset())

    assert _selected_model(BackendChoice.cuda, "unsupported-model") is None
    warning = capsys.readouterr().err
    assert "does not support model selection" in warning
    assert "ignoring --model 'unsupported-model'" in warning


def test_demo_buffer_mode_never_uses_catchup():
    drop = build_demo(buffer=BufferChoice.drop)
    block = build_demo(buffer=BufferChoice.block)

    assert {n.mode for n in drop.nodes if n.inputs} == {"drop"}
    assert {n.mode for n in block.nodes if n.inputs} == {"blocking"}
    assert all(n.mode != "catchup" for n in drop.nodes + block.nodes)


def test_language_flag_is_normalised_and_required():
    assert _language(" VI ") == "vi"
    with pytest.raises(typer.BadParameter):
        _language("  ")


def test_startup_event_detail_includes_message_and_progress():
    event = StartupEvent(
        "fix",
        StartupPhase.IN_PROGRESS,
        "downloading model",
        StartupProgress(current=25, total=100, unit="MB"),
    )

    assert _startup_detail(event) == "downloading model | 25/100 MB (25%)"


def test_ffmpeg_playback_uses_the_same_input_and_slice(monkeypatch):
    launched: list[tuple[list[str], dict]] = []

    class Process:
        pass

    monkeypatch.setattr(demo_module.shutil, "which", lambda name: "/bin/ffplay")
    monkeypatch.setattr(
        demo_module.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append((command, kwargs)) or Process(),
    )

    player = _play_ffmpeg_input(
        "lecture.mp3", device=False, start=12.5, seconds=30.0
    )

    assert isinstance(player, Process)
    assert launched[0][0] == [
        "/bin/ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
        "-ss", "12.5", "-i", "lecture.mp3", "-t", "30.0",
    ]


def test_ffmpeg_playback_is_optional_when_ffplay_is_missing(monkeypatch):
    monkeypatch.setattr(demo_module.shutil, "which", lambda name: None)

    assert _play_ffmpeg_input(
        "https://example.com/live.m3u8", device=False, start=0, seconds=0
    ) is None

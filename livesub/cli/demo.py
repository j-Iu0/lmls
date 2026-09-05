"""The deliberately opinionated ``livesub demo`` command.

Unlike ``livesub run``, the demo never reads a graph config.  It builds one known-good
variant of the default pipeline and exposes only the choices that make sense while a
demo is running: capture source, compute backend, output languages and queue policy.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import shutil
import sys
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import numpy as np
import typer

from ..core.config import GraphConfig, NodeConfig
from ..core.graph import Graph
from ..core.startup import StartupEvent, StartupPhase
from ..core.types import SAMPLE_RATE, AudioFrame, TextFrame, Utterance


class SourceChoice(str, Enum):
    mic = "mic"
    ffmpeg = "ffmpeg"


class BackendChoice(str, Enum):
    mlx = "mlx"
    whisper_ollama = "whisper-ollama"
    cuda = "cuda"


class BufferChoice(str, Enum):
    live = "live"
    block = "block"


class LogLevel(str, Enum):
    debug = "debug"
    info = "info"
    warning = "warning"
    error = "error"


_MLX_ASR_MODEL = "mlx-community/whisper-small.en-mlx"
_MLX_LLM_MODEL = "mlx-community/Qwen3.5-4B-4bit"
_WHISPER_MODEL = "small.en"
_OLLAMA_MODEL = "qwen3.5:4b"
_OLLAMA_HOST = "http://localhost:11434"

# Keep this separate from the implementation mapping. A future backend may expose a
# fixed appliance/model; accepting --model and silently forwarding it into **options
# would make the command appear to honor a choice that it cannot actually make.
_MODEL_SELECTABLE_BACKENDS = frozenset(BackendChoice)


def _detect_backend() -> BackendChoice:
    """Choose for the host, without importing a model runtime or loading weights."""
    if sys.platform == "darwin" and platform.machine().lower() in {"arm64", "aarch64"}:
        return BackendChoice.mlx
    if shutil.which("nvidia-smi") is not None:
        return BackendChoice.cuda
    return BackendChoice.whisper_ollama


def _languages(values: list[str]) -> list[str]:
    """Accept repeated flags as well as the convenient ``vi,zh`` spelling."""
    result: list[str] = []
    for value in values:
        for language in value.split(","):
            language = language.strip().lower()
            if language and language not in result:
                result.append(language)
    if not result:
        raise typer.BadParameter("select at least one output language")
    return result


def _version_callback(value: bool) -> None:
    if value:
        from .. import __version__

        typer.echo(f"livesub {__version__}")
        raise typer.Exit()


def _selected_model(backend: BackendChoice, requested: str | None) -> str | None:
    """Return a supported model override, warning when it must be ignored."""
    if requested and backend not in _MODEL_SELECTABLE_BACKENDS:
        typer.secho(
            f"warning: backend {backend.value!r} does not support model selection; "
            f"ignoring --model {requested!r}",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return None
    return requested


def _demo_config(
    *,
    source: SourceChoice,
    source_input: str | None,
    ffmpeg_device: bool,
    backend: BackendChoice,
    languages: list[str],
    buffer: BufferChoice,
    asr_model: str | None = None,
    llm_model: str | None = None,
    ollama_host: str = _OLLAMA_HOST,
    start: float = 0.0,
    jsonl: Path | None = None,
    websocket_port: int | None = None,
) -> GraphConfig:
    """Build the fixed demo graph.  No filesystem config is consulted."""
    mode = "live" if buffer is BufferChoice.live else "blocking"

    if source is SourceChoice.ffmpeg:
        if not source_input:
            raise typer.BadParameter(
                "--source ffmpeg requires --input FILE_OR_URL (or --in)"
            )
        source_options: dict[str, Any] = {
            "url": source_input,
            "device": ffmpeg_device,
            "realtime": True,
        }
        if start:
            source_options["extra_args"] = ["-ss", str(start)]
    else:
        if ffmpeg_device:
            raise typer.BadParameter("--ffmpeg-device requires --source ffmpeg")
        if start:
            raise typer.BadParameter("--start requires --source ffmpeg")
        source_options = {"device": source_input or "default"}

    if backend is BackendChoice.mlx:
        asr_impl, correct_impl, translate_impl = (
            "mlx_whisper", "mlx_llm_corrector", "mlx_llm_translator"
        )
        asr_options = {"model": asr_model or _MLX_ASR_MODEL}
        llm_options = {"model": llm_model or _MLX_LLM_MODEL}
    else:
        asr_impl, correct_impl, translate_impl = (
            "faster_whisper", "ollama_corrector", "ollama_translator"
        )
        asr_options = {
            "model": asr_model or _WHISPER_MODEL,
            "device": "cuda" if backend is BackendChoice.cuda else "auto",
            "compute_type": "float16" if backend is BackendChoice.cuda else "int8",
        }
        llm_options = {
            "model": llm_model or _OLLAMA_MODEL,
            "host": ollama_host,
        }

    nodes = [
        NodeConfig(
            name="source", impl=source.value,
            outputs={"audio": "audio.raw"}, options=source_options,
        ),
        NodeConfig(
            name="vad", impl="energy", inputs={"audio": "audio.raw"},
            outputs={"utterance": "utterance.speech"}, mode=mode,
        ),
        NodeConfig(
            name="asr", impl=asr_impl, inputs={"audio": "utterance.speech"},
            outputs={"text": "text.raw"}, options=asr_options, mode=mode,
        ),
        NodeConfig(
            name="fix", impl=correct_impl, inputs={"text_in": "text.raw"},
            outputs={"text_out": "text.corrected"}, options=dict(llm_options),
            mode=mode,
        ),
    ]

    text_topics = ["text.raw", "text.corrected"]
    for index, language in enumerate(languages):
        topic = f"text.translation.{index}"
        nodes.append(
            NodeConfig(
                name=f"translate_{index}_{language.replace('-', '_')}",
                impl=translate_impl,
                inputs={"text_in": "text.corrected"},
                outputs={"text_out": topic},
                options={**llm_options, "target": language},
                mode=mode,
            )
        )
        text_topics.append(topic)

    sink_inputs = {
        "text" if i == 0 else f"text_{i}": topic
        for i, topic in enumerate(text_topics)
    }
    nodes.append(
        NodeConfig(
            name="screen", impl="stdout_pretty", inputs=sink_inputs,
            options={"show_latency": True}, mode=mode,
        )
    )
    if jsonl is not None:
        nodes.append(
            NodeConfig(
                name="log", impl="jsonl", inputs=dict(sink_inputs),
                options={"path": str(jsonl)}, mode=mode,
            )
        )
    if websocket_port is not None:
        nodes.append(
            NodeConfig(
                name="ws", impl="websocket_server", inputs=dict(sink_inputs),
                options={"port": websocket_port}, mode=mode,
            )
        )

    return GraphConfig(
        nodes=nodes,
        settings={
            "backend": backend.value,
            "languages": languages,
            "buffer_mode": buffer.value,
        },
    )


async def _warm(graph: Graph) -> float:
    """Load models and force one inference before capture starts."""
    t0 = time.perf_counter()
    silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
    loop = asyncio.get_running_loop()

    for node in graph.nodes:
        stage = node.stage
        cls = type(stage)
        in_types = set(cls.inputs.values())
        if Utterance in in_types:
            # Go through the live path directly. The one-shot path segments first,
            # so a silent probe would never reach Whisper and would not warm it.
            now = time.time()
            await stage.process(
                Utterance("_warmup", silence, now - 1.0, now, is_final=True)
            )
        elif TextFrame in in_types and cls.outputs:
            from dataclasses import replace
            from ..core.types import Lineage

            probe = TextFrame(
                text="This is a warm up sentence.",
                lineage=replace(
                    Lineage.new(segment_id="_warmup"), t_audio_end=time.time()
                ),
            )
            await stage.process(probe)
        elif AudioFrame in in_types and AudioFrame in set(cls.outputs.values()):
            frame = AudioFrame(np.zeros(320, dtype=np.float32), SAMPLE_RATE, 0)
            await loop.run_in_executor(None, stage.process, frame)
    return time.perf_counter() - t0


def _startup_detail(event: StartupEvent) -> str:
    """Format optional startup progress without assuming every field is known."""
    parts = [event.message] if event.message else []
    progress = event.progress
    if progress is not None:
        unit = f" {progress.unit}" if progress.unit else ""
        if progress.current is not None and progress.total is not None:
            measured = f"{progress.current:g}/{progress.total:g}{unit}"
        elif progress.current is not None:
            measured = f"{progress.current:g}{unit}"
        else:
            measured = ""
        if progress.fraction is not None:
            percent = f"{progress.fraction:.0%}"
            measured = f"{measured} ({percent})" if measured else percent
        if measured:
            parts.append(measured)
    return " | ".join(parts)


def register(app: typer.Typer) -> None:
    @app.command()
    def demo(
        source: SourceChoice = typer.Option(
            "mic", help="Capture adapter: mic or ffmpeg."
        ),
        source_input: Optional[str] = typer.Option(
            None, "--input", "--in",
            help="Mic device name, or an ffmpeg file/URL/device.",
        ),
        ffmpeg_device: bool = typer.Option(
            False, help="Treat ffmpeg input as a capture device."
        ),
        backend: Optional[BackendChoice] = typer.Option(
            None, help="Compute backend; omitted means detect from this host."
        ),
        language: list[str] = typer.Option(
            ["vi"], "--language", "--target", "-l",
            help="Output language; repeat the flag or use a comma-separated list.",
        ),
        buffer_mode: BufferChoice = typer.Option(
            "live", help="Queue policy: live or block."
        ),
        seconds: float = typer.Option(
            0.0, help="Stop after N seconds; 0 runs until input ends or Ctrl-C."
        ),
        start: float = typer.Option(
            0.0, help="For ffmpeg inputs, seek this many seconds before reading."
        ),
        asr_model: Optional[str] = typer.Option(None, help="Override the ASR model."),
        llm_model: Optional[str] = typer.Option(
            None,
            "--model",
            "--llm-model",
            help="Select the backend language model.",
        ),
        ollama_host: str = typer.Option(
            _OLLAMA_HOST, help="Ollama URL for whisper-ollama and cuda."
        ),
        log_level: LogLevel = typer.Option(
            "warning", help="Python log level."
        ),
        jsonl: Optional[Path] = typer.Option(
            None, help="Also write subtitle events to this JSONL file."
        ),
        websocket_port: Optional[int] = typer.Option(
            None, help="Also publish subtitle events over WebSocket."
        ),
        warm: bool = typer.Option(True, help="Warm models before capture starts."),
        stats: bool = typer.Option(True, help="Print latency statistics on exit."),
        version: bool = typer.Option(
            False,
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ) -> None:
        """Run the fixed live-subtitle demonstration (no config files)."""
        logging.basicConfig(
            level=getattr(logging, log_level.value.upper()),
            format="%(levelname)s %(name)s: %(message)s",
            stream=sys.stderr,
        )
        selected_backend = backend or _detect_backend()
        selected_llm_model = _selected_model(selected_backend, llm_model)
        targets = _languages(language)
        if seconds < 0:
            raise typer.BadParameter("--seconds must be zero or greater")
        if start < 0:
            raise typer.BadParameter("--start must be zero or greater")
        if websocket_port is not None and not 1 <= websocket_port <= 65535:
            raise typer.BadParameter("--websocket-port must be between 1 and 65535")

        cfg = _demo_config(
            source=source,
            source_input=source_input,
            ffmpeg_device=ffmpeg_device,
            backend=selected_backend,
            languages=targets,
            buffer=buffer_mode,
            asr_model=asr_model,
            llm_model=selected_llm_model,
            ollama_host=ollama_host,
            start=start,
            jsonl=jsonl,
            websocket_port=websocket_port,
        )

        print_lock = threading.Lock()

        def on_startup(event: StartupEvent) -> None:
            color, prefix = {
                StartupPhase.IN_PROGRESS: (typer.colors.BLUE, "  ..."),
                StartupPhase.READY: (typer.colors.GREEN, "  ok "),
                StartupPhase.FAILED: (typer.colors.RED, "  ERR"),
            }[event.phase]
            formatted = _startup_detail(event)
            detail = f" {formatted}" if formatted else ""
            with print_lock:
                typer.secho(
                    f"{prefix} [{event.module_name}]{detail}", fg=color, err=True
                )

        graph = Graph(cfg, on_startup=on_startup)

        async def go() -> None:
            if warm:
                typer.secho("warming models...", fg=typer.colors.BLUE, err=True)
                elapsed = await _warm(graph)
                typer.secho(
                    f"models warm in {elapsed:.1f}s", fg=typer.colors.GREEN, err=True
                )
            typer.secho(
                f"demo: {source.value} | {selected_backend.value} | "
                f"{', '.join(lang.upper() for lang in targets)} | "
                f"buffer={buffer_mode.value} | Ctrl-C to stop\n",
                fg=typer.colors.BLUE,
                err=True,
            )
            await graph.run(timeout=seconds or None)

        try:
            asyncio.run(go())
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            typer.secho(f"demo failed: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None

        if stats:
            typer.secho(graph.metrics.format_table(), err=True)
            dropped = graph.bus.total_dropped()
            if dropped:
                typer.secho(
                    f"\n  {dropped} queued items dropped in live mode.",
                    fg=typer.colors.YELLOW,
                    err=True,
                )

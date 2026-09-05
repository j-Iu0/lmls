"""``livesub`` -- run the whole pipeline, inspect a graph, or benchmark a preset.

    livesub run --config config/default.toml
    livesub run --config config/mock.toml            # no models needed
    livesub run --chain mic,segment,asr,translate --target vi
    livesub graph --config config/default.toml --mermaid
    livesub bench --config config/mock.toml --in assets/lecture.wav
    livesub devices
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

import typer

# Model-hub progress bars scribble over the subtitle display.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from ..core.config import GraphConfig, chain_config, load_config  # noqa: E402
from ..core.graph import Graph, mermaid, validate  # noqa: E402
from ..core.registry import available  # noqa: E402

app = typer.Typer(
    add_completion=False,
    help="Live bilingual subtitle pipeline. Modules are wired by config, not by code.",
)


def _version_callback(value: bool) -> None:
    if value:
        from .. import __version__

        typer.echo(f"livesub {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Live bilingual subtitle pipeline."""


def _setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else (logging.ERROR if quiet else logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


#: Impl names grouped the way the CLI flags (--input/--denoise/...) think about them.
#: Purely CLI ergonomics: the graph routes by port declarations, not by these sets.
_INPUT_IMPLS = {"mic", "ffmpeg", "wav", "stdin"}
_DENOISE_IMPLS = {
    "passthrough_denoiser", "highpass_gate", "spectral", "noisereduce",
    "deepfilternet",
}
_TRANSCRIBE_IMPLS = {"mock_transcriber", "mlx_whisper", "faster_whisper"}
_CORRECT_IMPLS = {
    "passthrough_corrector", "rules", "mlx_llm_corrector", "ollama_corrector",
    "cloud_llm_corrector",
}
_TRANSLATE_IMPLS = {
    "mock_translator", "mlx_llm_translator", "ollama_translator",
    "cloud_llm_translator",
}


def _resolve(
    config: Optional[Path],
    chain: Optional[str],
    overrides: dict[str, str],
    target: Optional[str],
    source: Optional[str],
    device: bool,
) -> GraphConfig:
    if config and chain:
        raise typer.BadParameter("use --config or --chain, not both")
    if chain:
        cfg = chain_config([s for s in chain.split(",") if s.strip()], overrides)
    elif config:
        cfg = load_config(config)
    else:
        raise typer.BadParameter("need --config FILE or --chain STAGES")

    # CLI overrides applied on top of the file, so a preset can be reused as-is.
    for node in cfg.nodes:
        old_impl = node.impl
        role = (
            "input" if node.impl in _INPUT_IMPLS
            else "denoise" if node.impl in _DENOISE_IMPLS
            else "transcribe" if node.impl in _TRANSCRIBE_IMPLS
            else "correct" if node.impl in _CORRECT_IMPLS
            else "translate" if node.impl in _TRANSLATE_IMPLS
            else None
        )
        if role and role in overrides:
            node.impl = overrides[role]
            if node.impl != old_impl and role == "input":
                # 'device' means different things per impl: a name for 'mic', a boolean
                # flag for 'ffmpeg'. The old impl's value must not leak into the new one.
                node.options.pop("device", None)
        if target and (node.impl in _TRANSLATE_IMPLS or node.impl.startswith("fused")):
            node.options["target"] = target
        if source and node.impl in _INPUT_IMPLS:
            node.options["url" if node.impl == "ffmpeg" else "device"] = source
            node.options["path"] = source if node.impl == "wav" else node.options.get(
                "path"
            )
            if device and node.impl == "ffmpeg":
                node.options["device"] = True
    if target:
        cfg.settings["target"] = target
    return cfg


@app.command()
def run(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Graph TOML."),
    chain: Optional[str] = typer.Option(
        None, help="Linear shorthand, e.g. 'mic,segment,asr,correct,translate'."
    ),
    target: Optional[str] = typer.Option(None, help="Target language: vi, zh, ..."),
    source: Optional[str] = typer.Option(
        None, help="Override the input source (device name, file, or URL)."
    ),
    device: bool = typer.Option(False, help="Treat --source as a capture device."),
    input_impl: Optional[str] = typer.Option(None, "--input", help="Override input impl."),
    denoise: Optional[str] = typer.Option(None, help="Override denoise impl."),
    transcribe: Optional[str] = typer.Option(None, help="Override transcribe impl."),
    correct: Optional[str] = typer.Option(None, help="Override correct impl."),
    translate: Optional[str] = typer.Option(None, help="Override translate impl."),
    seconds: float = typer.Option(0.0, help="Stop after N seconds; 0 runs until Ctrl-C."),
    verbose: bool = typer.Option(False, "-v", help="Debug logging."),
    quiet: bool = typer.Option(False, "-q", help="Errors only."),
    stats: bool = typer.Option(True, help="Print the latency table on exit."),
) -> None:
    """Run the pipeline."""
    _setup_logging(verbose, quiet)
    overrides = {
        k: v
        for k, v in {
            "input": input_impl, "denoise": denoise, "transcribe": transcribe,
            "correct": correct, "translate": translate,
        }.items()
        if v
    }
    cfg = _resolve(config, chain, overrides, target, source, device)
    graph = Graph(cfg)

    if not quiet:
        typer.secho(_banner(cfg, graph), err=True, fg=typer.colors.BLUE)

    async def go() -> None:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with __import__("contextlib").suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        runner = asyncio.create_task(graph.run(timeout=seconds or None))
        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait(
            {runner, stopper}, return_when=asyncio.FIRST_COMPLETED
        )
        if stopper in done and not runner.done():
            typer.secho("\nstopping...", err=True, fg=typer.colors.YELLOW)
            await graph.aclose()
            with __import__("contextlib").suppress(Exception):
                await runner
        stopper.cancel()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:  # pragma: no cover
        pass

    if stats and not quiet:
        typer.secho(graph.metrics.format_table(), err=True)
        dropped = graph.bus.total_dropped()
        if dropped:
            typer.secho(
                f"\n  {dropped} audio frames dropped under backpressure "
                f"({dropped * 20 / 1000:.1f}s) -- a stage could not keep up.",
                err=True,
                fg=typer.colors.YELLOW,
            )


def _banner(cfg: GraphConfig, graph: Graph) -> str:
    lines = ["", "pipeline:"]
    for node in cfg.active:
        ins = ", ".join(f"{k}<-{v}" for k, v in node.inputs.items()) or "-"
        outs = ", ".join(f"{k}->{v}" for k, v in node.outputs.items()) or "-"
        lines.append(f"  {node.name:<14} {node.impl:<22} [{ins}] => [{outs}]")
    fanout = {
        topic: graph.bus.subscribers_of(topic)
        for topic in graph.bus.topics
        if len(graph.bus.subscribers_of(topic)) > 1
    }
    for topic, subs in fanout.items():
        lines.append(f"  fan-out: {topic} -> {', '.join(subs)} (independent queues)")
    for warning in graph.warnings:
        lines.append(f"  note: {warning}")
    return "\n".join(lines) + "\n"


@app.command()
def graph(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    chain: Optional[str] = typer.Option(None),
    mermaid_out: bool = typer.Option(False, "--mermaid", help="Emit a mermaid diagram."),
) -> None:
    """Validate a wiring and show it. Never opens a device or loads a model."""
    cfg = _resolve(config, chain, {}, None, None, False)
    warnings = validate(cfg)
    if mermaid_out:
        typer.echo(mermaid(cfg))
    else:
        typer.echo(_describe(cfg))
    for warning in warnings:
        typer.secho(f"note: {warning}", err=True, fg=typer.colors.YELLOW)
    typer.secho("wiring is valid", err=True, fg=typer.colors.GREEN)


def _describe(cfg: GraphConfig) -> str:
    produced: dict[str, list[str]] = {}
    consumed: dict[str, list[str]] = {}
    for node in cfg.active:
        for t in node.out_topics:
            produced.setdefault(t, []).append(node.name)
        for t in node.in_topics:
            consumed.setdefault(t, []).append(node.name)
    lines = ["nodes:"]
    for node in cfg.active:
        opts = {k: v for k, v in node.options.items() if not k.startswith("_")}
        lines.append(f"  {node.name:<14} {node.impl}")
        if node.inputs:
            lines.append(f"                 in  {', '.join(f'{k}={v}' for k, v in node.inputs.items())}")
        if node.outputs:
            lines.append(
                "                 out "
                + ", ".join(f"{k}={v}" for k, v in node.outputs.items())
            )
        if opts:
            lines.append(f"                 opt {json.dumps(opts, default=str)}")
    lines.append("\ntopics:")
    for topic in sorted(set(produced) | set(consumed)):
        readers = consumed.get(topic, [])
        marker = "  <- FAN-OUT" if len(readers) > 1 else ""
        lines.append(
            f"  {topic:<18} from {', '.join(produced.get(topic, ['?'])):<18} "
            f"to {', '.join(readers) or '(nobody)'}{marker}"
        )
    return "\n".join(lines)


@app.command()
def devices() -> None:
    """List audio input devices."""
    from ..input.ffmpeg_source import FfmpegSource
    from ..input.mic import MicSource

    typer.echo("PortAudio (input impl 'mic', option device=):")
    for d in MicSource.list_devices():
        mark = "  *default" if d["default"] else ""
        typer.echo(f"  [{d['index']}] {d['name']} ({d['samplerate']} Hz){mark}")
    typer.echo("\navfoundation (input impl 'ffmpeg' with device=true):")
    for d in FfmpegSource.list_devices():
        typer.echo(f"  [{d['index']}] {d['name']}")
    typer.secho(
        "\nA virtual device such as 'Background Music' or 'BlackHole' carries system\n"
        "audio -- use it to caption a video call or a playing video:\n"
        "  livesub run -c config/video.toml --source 'Background Music' --device",
        fg=typer.colors.BLUE,
    )


@app.command("list")
def list_impls() -> None:
    """List swappable implementations. The registry is flat: roles live in the
    modules' port declarations, not in the registry."""
    for name in available():
        typer.echo(f"  {name}")


from .bench import register as _register_bench  # noqa: E402
from .demo import register as _register_demo  # noqa: E402

_register_bench(app)
_register_demo(app)


def app_main() -> None:
    app()


if __name__ == "__main__":
    app()

"""``python -m lmls.transcribe`` -- speech to text, standalone.

Emits JSONL ``TextFrame``s on stdout, which is exactly what the correction and
translation CLIs read, so the stages pipe together as ordinary processes.

The streaming path drives the same segmenter the offline path uses, because in the graph
that segmentation is a separate node and this CLI has no graph around it.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from ..core.audioutil import load_wav
from ..core.codec import aread_frames, write_event
from ..core.registry import available, build, resolve
from ..core.types import SAMPLE_RATE, AudioFrame, Utterance

app = typer.Typer(add_completion=False, help="Speech to text (transcription module).")


def _transcribe_impls() -> list[str]:
    out = []
    for name in available():
        try:
            cls = resolve(name)
        except Exception:  # pragma: no cover - broken optional dep
            continue
        if set(cls.inputs.values()) == {Utterance}:
            out.append(name)
    return out


@app.command("list")
def list_impls() -> None:
    """Show available transcriber implementations."""
    for name in _transcribe_impls():
        typer.echo(f"  {name}")


@app.command()
def run(
    impl: str = typer.Argument("mlx_whisper", help="Implementation name."),
    infile: Optional[Path] = typer.Option(None, "--in", help="Audio file to transcribe."),
    raw: bool = typer.Option(False, help="Read f32le PCM frames from stdin."),
    model: Optional[str] = typer.Option(None, help="Override the model id."),
    segmenter: str = typer.Option("energy", help="energy | silero (standalone path)."),
    text_only: bool = typer.Option(
        False, help="Print plain text instead of JSONL (for reading, not piping)."
    ),
) -> None:
    """Transcribe a file or a live PCM stream."""
    options = {"segmenter": segmenter}
    if model:
        options["model"] = model
    transcriber = build(impl, name=impl, **options)

    if raw:

        async def pump() -> None:
            from ..segment import make_segmenter

            await transcriber.start()
            segment = make_segmenter(segmenter)

            async def utterances():
                async for frame in aread_frames():
                    for utterance in segment.push(frame):
                        yield utterance
                for utterance in segment.close():
                    yield utterance

            async for utterance in utterances():
                for frame in await transcriber.process(utterance):
                    _write(frame, text_only)

        asyncio.run(pump())
        return

    if infile is None:
        typer.secho("need --in FILE or --raw", err=True, fg=typer.colors.RED)
        raise typer.Exit(2)

    pcm = load_wav(infile, SAMPLE_RATE)
    asyncio.run(transcriber.start())
    t0 = time.perf_counter()
    frames = transcriber.transcribe_array(pcm, SAMPLE_RATE)
    elapsed = time.perf_counter() - t0
    for frame in frames:
        _write(frame, text_only)

    duration = len(pcm) / SAMPLE_RATE
    typer.secho(
        f"\n{impl}: {len(frames)} utterances from {duration:.1f}s of audio in "
        f"{elapsed:.2f}s ({duration / max(elapsed, 1e-6):.1f}x realtime)",
        err=True,
        fg=typer.colors.GREEN,
    )


def _write(frame, text_only: bool) -> None:
    if text_only:
        marker = " " if frame.is_final else "~"
        print(f"[{frame.segment_id}]{marker}{frame.text}", flush=True)
    else:
        write_event(frame)


@app.command()
def devices() -> None:
    """Report which transcription backends are importable on this machine."""
    for name in _transcribe_impls():
        try:
            build(name, name=name)
            status, colour = "available", typer.colors.GREEN
        except Exception as exc:
            status, colour = f"unavailable ({type(exc).__name__})", typer.colors.YELLOW
        typer.secho(f"  {name:<16} {status}", fg=colour)


if __name__ == "__main__":
    app()
"""``python -m lmls.correct`` -- error correction, standalone.

Reads JSONL ``TextFrame``s from stdin (what the transcription CLI writes) or a single
``--text`` line, and writes JSONL to stdout for the translation CLI to read.

The discourse context is maintained by the corrector itself (internal state), so the
standalone path and the graph path behave identically.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Optional

import typer

from ..core.codec import read_events, write_event
from ..core.registry import available, build
from ..core.types import Lineage, TextFrame

app = typer.Typer(add_completion=False, help="Error correction (correction module).")


@app.command("list")
def list_impls() -> None:
    """Show available corrector implementations."""
    for name in available():
        typer.echo(f"  {name}")


def _frame(text: str, segment_id: str = "cli") -> TextFrame:
    return TextFrame(
        text=text,
        lineage=replace(Lineage.new(segment_id=segment_id), t_audio_end=time.time()),
    )


@app.command()
def run(
    impl: str = typer.Argument("mlx_llm_corrector", help="Implementation name."),
    text: Optional[str] = typer.Option(None, help="Correct one line and exit."),
    model: Optional[str] = typer.Option(None, help="Override the model id."),
    host: Optional[str] = typer.Option(
        None, help="Ollama server URL (impl = ollama_corrector)."
    ),
    timeout: Optional[float] = typer.Option(
        None, help="Per-call HTTP timeout in seconds (impl = ollama_corrector)."
    ),
    glossary: Optional[Path] = typer.Option(None, help="Glossary JSON (rules impl)."),
    text_only: bool = typer.Option(
        False, help="Print plain text instead of JSONL."
    ),
    show_diff: bool = typer.Option(
        False, help="Show original -> corrected for lines that changed."
    ),
) -> None:
    """Correct a single line, or a JSONL stream on stdin."""
    options = {}
    if model:
        options["model"] = model
    if host:
        options["host"] = host
    if timeout is not None:
        options["timeout"] = timeout
    if glossary:
        options["glossary"] = glossary
    corrector = build(impl, name=impl, **options)

    async def go() -> None:
        await corrector.start()

        async def handle(frame: TextFrame) -> None:
            t0 = time.perf_counter()
            out = await corrector.process(frame)
            elapsed = (time.perf_counter() - t0) * 1000
            if out is None:
                return
            if text_only or show_diff:
                changed = out.meta.get("corrected")
                if show_diff and changed:
                    typer.echo(f"  - {frame.text}")
                    typer.secho(f"  + {out.text}", fg=typer.colors.GREEN)
                else:
                    typer.echo(out.text)
            else:
                write_event(out)
            if show_diff:
                typer.secho(f"    ({elapsed:.0f} ms)", err=True, dim=True)

        if text is not None:
            await handle(_frame(text))
        else:
            for frame in read_events():
                await handle(frame)

    asyncio.run(go())


@app.command()
def bench(
    impl: str = typer.Argument("mlx_llm_corrector"),
    model: Optional[str] = typer.Option(None),
    lines: Optional[Path] = typer.Option(
        None, help="File of erroneous lines, one per line. Defaults to a built-in set."
    ),
) -> None:
    """Time the corrector on lines containing realistic ASR errors."""
    from ..transcribe.mock import DEFAULT_SCRIPT

    cases = (
        [ln.strip() for ln in lines.read_text().splitlines() if ln.strip()]
        if lines
        else list(DEFAULT_SCRIPT)
    )
    options = {"model": model} if model else {}
    corrector = build(impl, name=impl, **options)

    async def go() -> None:
        await corrector.start()
        timings = []
        changed = 0
        for line in cases:
            frame = _frame(line, segment_id="b")
            t0 = time.perf_counter()
            out = await corrector.process(frame)
            ms = (time.perf_counter() - t0) * 1000
            timings.append(ms)
            if out is not None and out.text != line:
                changed += 1
                typer.echo(f"  - {line}")
                typer.secho(f"  + {out.text}  ({ms:.0f} ms)", fg=typer.colors.GREEN)
            else:
                typer.echo(f"  = {line}  ({ms:.0f} ms)")
        timings.sort()
        typer.secho(
            f"\n{impl}: {len(cases)} lines, {changed} changed; "
            f"p50 {timings[len(timings) // 2]:.0f} ms, "
            f"p95 {timings[int(len(timings) * 0.95) - 1]:.0f} ms, "
            f"max {timings[-1]:.0f} ms",
            fg=typer.colors.CYAN,
        )
        typer.echo(f"  {corrector.describe()}")

    asyncio.run(go())


if __name__ == "__main__":
    app()
"""``python -m livesub.translate`` -- translation, standalone.

``--repair`` sets repair_mode, the behaviour used when there is no correction stage, so
both wirings can be compared from the command line on identical input. Translators
without a repair mode are swapped for their fused counterpart, which does the same job:

    livesub.transcribe run mock --in x.wav | livesub.correct run mlx_llm_corrector | livesub.translate run mlx_llm_translator
    livesub.transcribe run mock --in x.wav | livesub.translate run mlx_llm_translator --repair
    livesub.transcribe run mock --in x.wav | livesub.translate run ollama_translator --repair   # -> fused_ollama
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Optional

import typer

from ..core.codec import read_events, write_event
from ..core.registry import available, build
from ..core.types import Lineage, TextFrame

app = typer.Typer(add_completion=False, help="Translation (translation module).")

#: Translators without a repair mode, mapped to the fused stage that does the same
#: one-call repair+translate job. Passing ``repair_mode`` to them would be silently
#: swallowed by their ``**_`` and produce a quietly wrong comparison.
_FUSED_COUNTERPART = {
    "ollama_translator": "fused_ollama",
    "mlx_llm_translator": "fused_llm",
}

#: The corrector that pairs with each translator in the split-wiring column.
_CORRECTOR_OF = {
    "ollama_translator": "ollama_corrector",
    "mlx_llm_translator": "mlx_llm_corrector",
    "cloud_llm_translator": "cloud_llm_corrector",
}


@app.command("list")
def list_impls() -> None:
    """Show available translator implementations."""
    for name in available():
        typer.echo(f"  {name}")


def _frame(text: str, segment_id: str = "cli") -> TextFrame:
    return TextFrame(
        text=text,
        lineage=replace(Lineage.new(segment_id=segment_id), t_audio_end=time.time()),
    )


@app.command()
def run(
    impl: str = typer.Argument("mlx_llm_translator", help="Implementation name."),
    target: str = typer.Option("vi", help="Target language code: vi, zh, ..."),
    text: Optional[str] = typer.Option(None, help="Translate one line and exit."),
    model: Optional[str] = typer.Option(None, help="Override the model id."),
    host: Optional[str] = typer.Option(
        None, help="Ollama server URL (impl = ollama_translator)."
    ),
    timeout: Optional[float] = typer.Option(
        None, help="Per-call HTTP timeout in seconds (impl = ollama_translator)."
    ),
    repair: bool = typer.Option(
        False,
        help="Repair ASR errors while translating (the no-correction-stage wiring). "
             "Translators without a repair mode use their fused counterpart.",
    ),
    text_only: bool = typer.Option(False, help="Print plain text instead of JSONL."),
) -> None:
    """Translate a single line, or a JSONL stream on stdin."""
    impl = _FUSED_COUNTERPART.get(impl, impl) if repair else impl
    options: dict = {"target": target, "repair_mode": repair}
    if model:
        options["model"] = model
    if host:
        options["host"] = host
    if timeout is not None:
        options["timeout"] = timeout
    translator = build(impl, name=impl, **options)

    async def go() -> None:
        await translator.start()

        def _emit(frame: TextFrame) -> None:
            if text_only:
                prefix = "EN" if frame.lang == "en" else frame.lang.upper()
                colour = (
                    typer.colors.WHITE if frame.lang == "en" else typer.colors.CYAN
                )
                typer.secho(f"  {prefix}  {frame.text}", fg=colour)
            else:
                write_event(frame)

        async def handle(frame: TextFrame) -> None:
            t0 = time.perf_counter()
            produced = await translator.process(frame)
            elapsed = (time.perf_counter() - t0) * 1000
            if produced is None:
                return
            outs = (
                list(produced.values()) if isinstance(produced, dict) else [produced]
            )
            for out in outs:
                _emit(out)
            if text_only and outs:
                typer.secho(f"      ({elapsed:.0f} ms)", err=True, dim=True)

        if text is not None:
            await handle(_frame(text))
        else:
            for frame in read_events():
                await handle(frame)

    asyncio.run(go())


@app.command()
def compare(
    target: str = typer.Option("vi"),
    model: Optional[str] = typer.Option(None),
    impl: str = typer.Argument("mlx_llm_translator"),
) -> None:
    """Run the same erroneous lines through both wirings and time them.

    Left column: correct-then-translate (two model calls).
    Right column: repair-and-translate (one model call) -- the translator's repair mode,
    or its fused counterpart where the repair mode was dropped.
    This is the measurement behind the report's "is the correction stage worth a separate
    node?" question.
    """
    from ..transcribe.mock import DEFAULT_SCRIPT

    options: dict = {"target": target}
    if model:
        options["model"] = model
    corrector = build(_CORRECTOR_OF.get(impl, "mlx_llm_corrector"), name="correct", **(
        {"model": model} if model else {}))
    faithful = build(impl, name="faithful", repair_mode=False, **options)
    repairing = build(_FUSED_COUNTERPART.get(impl, impl), name="repairing",
                      repair_mode=True, **options)

    def _frames(produced) -> list[TextFrame]:
        if produced is None:
            return []
        return list(produced.values()) if isinstance(produced, dict) else [produced]

    async def go() -> None:
        for stage in (corrector, faithful, repairing):
            await stage.start()

        split_ms: list[float] = []
        fused_ms: list[float] = []
        for line in DEFAULT_SCRIPT[:6]:
            frame = _frame(line, segment_id="c")
            typer.secho(f"\nASR : {line}", fg=typer.colors.YELLOW)

            t0 = time.perf_counter()
            corrected = await corrector.process(frame)
            split = (time.perf_counter() - t0) * 1000
            out = await faithful.process(corrected)
            split_ms.append(split)
            typer.echo(f"  split  EN {corrected.text}")
            for e in _frames(out):
                typer.echo(f"         {e.lang.upper()} {e.text}")
            typer.secho(f"         {split:.0f} ms (2 calls)", dim=True)

            t0 = time.perf_counter()
            out = await repairing.process(frame)
            fused = (time.perf_counter() - t0) * 1000
            fused_ms.append(fused)
            for e in _frames(out):
                typer.echo(f"  repair {e.lang.upper()} {e.text}")
            typer.secho(f"         {fused:.0f} ms (1 call)", dim=True)

        typer.secho(
            f"\nmean: split {sum(split_ms) / len(split_ms):.0f} ms, "
            f"repair-in-translate {sum(fused_ms) / len(fused_ms):.0f} ms",
            fg=typer.colors.CYAN,
        )

    asyncio.run(go())


if __name__ == "__main__":
    app()
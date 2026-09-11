"""``python -m lmls.denoise`` -- noise reduction, standalone.

Also carries the tools that make the spec's noisy-condition testing reproducible:
``mix-noise`` builds a test file at an exact SNR, and ``compare`` runs every registered
denoiser over the same audio so the report's table is measured rather than asserted.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np
import typer

from ..core.audioutil import align, db, load_wav, mix_at_snr, rms, save_wav, snr_db
from ..core.codec import read_frames, write_frame
from ..core.registry import available, build, resolve
from ..core.types import SAMPLE_RATE, AudioFrame

app = typer.Typer(add_completion=False, help="Noise reduction (denoise module).")


def _denoise_impls() -> list[str]:
    """Impls whose ports are audio-in, audio-out. Found by type, not by name."""
    out = []
    for name in available():
        try:
            cls = resolve(name)
        except Exception:  # pragma: no cover - broken optional dep
            continue
        if (
            set(cls.inputs.values()) == {AudioFrame}
            and set(cls.outputs.values()) == {AudioFrame}
        ):
            out.append(name)
    return out


@app.command("list")
def list_impls() -> None:
    """Show available denoiser implementations."""
    for name in _denoise_impls():
        typer.echo(f"  {name}")


@app.command()
def run(
    impl: str = typer.Argument("spectral", help="Implementation name."),
    infile: Optional[Path] = typer.Option(None, "--in", help="Input audio file."),
    out: Optional[Path] = typer.Option(None, "--out", help="Output wav file."),
    raw: bool = typer.Option(False, help="Read/write f32le PCM on stdin/stdout."),
    report_snr: bool = typer.Option(
        False, "--report-snr", help="Report level change and estimated noise removed."
    ),
    stream: bool = typer.Option(
        True,
        help="Use the frame-by-frame streaming path (what the live pipeline uses) "
        "rather than the whole-file path.",
    ),
) -> None:
    """Denoise a file or a PCM stream."""
    denoiser = build(impl, name=impl)

    if raw:
        for frame in read_frames():
            write_frame(denoiser.process(frame))
        return

    if infile is None:
        typer.secho("need --in FILE or --raw", err=True, fg=typer.colors.RED)
        raise typer.Exit(2)

    pcm = load_wav(infile, SAMPLE_RATE)
    t0 = time.perf_counter()
    cleaned = (
        denoiser.process_array(pcm, SAMPLE_RATE)
        if not stream
        else _stream_array(denoiser, pcm)
    )
    elapsed = time.perf_counter() - t0

    if out is not None:
        save_wav(out, cleaned, SAMPLE_RATE)
        typer.secho(f"wrote {out}", err=True, fg=typer.colors.GREEN)

    duration = len(pcm) / SAMPLE_RATE
    typer.echo(
        f"{impl}: {duration:.2f}s audio in {elapsed:.3f}s "
        f"({duration / max(elapsed, 1e-6):.0f}x realtime), "
        f"added latency {getattr(denoiser, 'latency_ms', 0.0):.0f} ms",
        err=True,
    )
    if report_snr:
        removed = pcm[: len(cleaned)] - cleaned
        typer.echo(
            f"  input {db(rms(pcm)):.1f} dBFS -> output {db(rms(cleaned)):.1f} dBFS; "
            f"removed component {db(rms(removed)):.1f} dBFS",
            err=True,
        )


def _stream_array(denoiser, pcm: np.ndarray) -> np.ndarray:
    """Push a whole file through the live per-frame path, so a file measurement and a
    live run exercise identical code."""
    from ..core.types import FRAME_SAMPLES, AudioFrame

    chunks = []
    for i in range(0, len(pcm), FRAME_SAMPLES):
        block = pcm[i : i + FRAME_SAMPLES]
        if len(block) < FRAME_SAMPLES:
            block = np.pad(block, (0, FRAME_SAMPLES - len(block)))
        chunks.append(denoiser.process(AudioFrame(block, SAMPLE_RATE, i)).pcm)
    return np.concatenate(chunks)[: len(pcm)] if chunks else pcm


@app.command("mix-noise")
def mix_noise(
    speech: Path = typer.Option(..., help="Clean speech file."),
    noise: Path = typer.Option(..., help="Noise bed (classroom babble, fan, ...)."),
    snr: float = typer.Option(5.0, help="Target signal-to-noise ratio in dB."),
    out: Path = typer.Option(..., help="Output wav."),
) -> None:
    """Build a noisy test file at an exact, repeatable SNR.

    A microphone cannot reproduce the same noisy conditions twice, so "we tested it in a
    noisy room" is not a measurement. Mixing a known noise bed into clean speech at a
    stated SNR is, and it makes denoise-on vs denoise-off word error rates comparable.
    """
    s = load_wav(speech, SAMPLE_RATE)
    n = load_wav(noise, SAMPLE_RATE)
    mixed = mix_at_snr(s, n, snr)
    save_wav(out, mixed, SAMPLE_RATE)
    typer.secho(
        f"wrote {out}: {len(mixed) / SAMPLE_RATE:.1f}s at {snr:+.0f} dB SNR "
        f"(speech {db(rms(s)):.1f} dBFS, mixed {db(rms(mixed)):.1f} dBFS)",
        fg=typer.colors.GREEN,
    )


@app.command()
def compare(
    speech: Path = typer.Option(..., help="Clean reference speech."),
    noise: Optional[Path] = typer.Option(None, help="Noise bed to mix in."),
    snr: float = typer.Option(5.0, help="SNR for the mix."),
    outdir: Optional[Path] = typer.Option(None, help="Also write each result as wav."),
) -> None:
    """Run every denoiser over identical audio and report speed and residual error.

    ``resid vs clean`` is the SNR of each output against the *clean* reference, after
    compensating for the delay each denoiser introduces. Because it compares against
    clean speech rather than against the noise, a denoiser that chews up consonants
    scores worse rather than better -- which is the failure mode aggressive suppression
    actually has, and the one that costs word error rate.

    Treat this as a fast proxy. The measurement that decides the question is word error
    rate through the real ASR: ``python -m lmls bench --denoise ...``.
    """
    clean = load_wav(speech, SAMPLE_RATE)
    if noise is not None:
        noisy = mix_at_snr(clean, load_wav(noise, SAMPLE_RATE), snr)
        typer.echo(f"input: {speech.name} + {noise.name} at {snr:+.0f} dB SNR")
    else:
        noisy = clean
        typer.echo(f"input: {speech.name} (no noise added)")

    typer.echo(
        f"\n  {'impl':<16} {'added lat':>10} {'speed':>8} {'lag':>7} "
        f"{'out level':>10} {'resid vs clean':>15}"
    )
    ref, aligned, _ = align(clean, noisy)
    typer.echo(
        f"  {'(noisy input)':<16} {'-':>10} {'-':>8} {'-':>7} "
        f"{db(rms(noisy)):>9.1f}dB {snr_db(ref, aligned - ref):>14.1f}dB"
    )

    for impl in _denoise_impls():
        try:
            d = build(impl, name=impl)
        except Exception as exc:  # optional deps may be absent; that is informative
            typer.echo(f"  {impl:<16} unavailable: {type(exc).__name__}")
            continue
        t0 = time.perf_counter()
        cleaned = _stream_array(d, noisy)
        elapsed = time.perf_counter() - t0
        ref, aligned, lag = align(clean, cleaned)
        residual = snr_db(ref, aligned - ref)
        typer.echo(
            f"  {impl:<16} {getattr(d, 'latency_ms', 0.0):>8.0f}ms "
            f"{len(noisy) / SAMPLE_RATE / max(elapsed, 1e-6):>7.0f}x "
            f"{1000 * lag / SAMPLE_RATE:>6.0f}ms "
            f"{db(rms(cleaned)):>9.1f}dB {residual:>14.1f}dB"
        )
        if outdir is not None:
            save_wav(Path(outdir) / f"{impl}.wav", cleaned, SAMPLE_RATE)

    typer.echo(
        "\n'lag' is the delay measured by cross-correlation and removed before comparing;"
        "\nit should track each implementation's declared added latency."
    )


if __name__ == "__main__":
    app()

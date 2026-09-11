"""``python -m lmls.input`` -- capture audio, standalone.

Writes either a wav file (``--out``) or canonical PCM on stdout (``--raw``) for piping
into the next stage.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import typer

from ..core.audioutil import rms, db, save_wav
from ..core.codec import write_frame
from ..core.interfaces import Module
from ..core.types import SAMPLE_RATE

app = typer.Typer(add_completion=False, help="Audio capture (input module).")


async def _drain(
    source: Module,
    seconds: float | None,
    out: Path | None,
    raw: bool,
    quiet: bool,
) -> np.ndarray:
    collected: list[np.ndarray] = []
    frames = 0
    peak = 0.0
    await source.start()
    try:
        async for frame in source.run():
            frames += 1
            peak = max(peak, float(np.max(np.abs(frame.pcm))) if len(frame.pcm) else 0.0)
            if raw:
                write_frame(frame)
            if out is not None or not raw:
                collected.append(frame.pcm)
            if seconds and frames * frame.duration >= seconds:
                break
    finally:
        await source.stop()

    pcm = np.concatenate(collected) if collected else np.zeros(0, dtype=np.float32)
    if out is not None:
        save_wav(out, pcm, SAMPLE_RATE)
    if not quiet and not raw:
        level = db(rms(pcm))
        typer.secho(
            f"captured {len(pcm) / SAMPLE_RATE:.2f}s  "
            f"({frames} frames, rms {level:.1f} dBFS, peak {db(peak):.1f} dBFS)",
            err=True,
            fg=typer.colors.GREEN if level > -60 else typer.colors.YELLOW,
        )
        if level <= -60:
            typer.secho(
                "  signal is essentially silent -- check the device selection and, on "
                "macOS, that the terminal has microphone permission "
                "(System Settings > Privacy & Security > Microphone).",
                err=True,
                fg=typer.colors.YELLOW,
            )
    return pcm


@app.command("list-devices")
def list_devices(
    ffmpeg: bool = typer.Option(False, help="Also list ffmpeg avfoundation devices."),
) -> None:
    """Show input devices usable by --device."""
    from .mic import MicSource

    typer.echo("PortAudio input devices (use with: input mic --device NAME):")
    for d in MicSource.list_devices():
        mark = " *default" if d["default"] else ""
        typer.echo(
            f"  [{d['index']}] {d['name']}  "
            f"({d['channels']}ch @ {d['samplerate']} Hz){mark}"
        )
    if ffmpeg:
        from .ffmpeg_source import FfmpegSource

        typer.echo("\navfoundation audio devices (use with: input ffmpeg --device):")
        for d in FfmpegSource.list_devices():
            typer.echo(f"  [{d['index']}] {d['name']}")
    typer.echo(
        "\nTip: a virtual device such as 'Background Music' or 'BlackHole' carries "
        "system audio,\nwhich is how you caption a video call or a playing video "
        "instead of the room."
    )


@app.command()
def mic(
    device: Optional[str] = typer.Option(None, help="Device name substring or index."),
    seconds: float = typer.Option(5.0, help="Capture duration; 0 means run forever."),
    out: Optional[Path] = typer.Option(None, help="Write a wav file here."),
    raw: bool = typer.Option(False, help="Emit f32le PCM on stdout for piping."),
    block_ms: int = typer.Option(20, help="PortAudio buffer period."),
) -> None:
    """Capture from the system microphone (or any input device)."""
    from .mic import MicSource

    source = MicSource(device=device, block_ms=block_ms)
    asyncio.run(_drain(source, seconds or None, out, raw, quiet=raw))


@app.command()
def ffmpeg(
    url: str = typer.Argument(..., help="File path, stream URL, or device name."),
    device: bool = typer.Option(False, help="Treat URL as an avfoundation device."),
    seconds: float = typer.Option(0.0, help="Stop after N seconds; 0 means run to EOF."),
    out: Optional[Path] = typer.Option(None, help="Write a wav file here."),
    raw: bool = typer.Option(False, help="Emit f32le PCM on stdout for piping."),
    realtime: bool = typer.Option(True, help="Pace file playback at wall-clock speed."),
) -> None:
    """Decode a file, URL, or capture device through ffmpeg."""
    from .ffmpeg_source import FfmpegSource

    source = FfmpegSource(url=url, device=device, realtime=realtime)
    asyncio.run(_drain(source, seconds or None, out, raw, quiet=raw))


@app.command()
def wav(
    path: Path = typer.Argument(..., help="Audio file to replay."),
    seconds: float = typer.Option(0.0, help="Stop after N seconds."),
    out: Optional[Path] = typer.Option(None, help="Write a wav file here."),
    raw: bool = typer.Option(False, help="Emit f32le PCM on stdout for piping."),
    realtime: bool = typer.Option(
        True, help="Pace at wall-clock speed; --no-realtime for fast offline runs."
    ),
) -> None:
    """Replay a file as if it were live -- the reproducible stand-in for a microphone."""
    from .wav_replay import WavReplaySource

    source = WavReplaySource(path=path, realtime=realtime)
    asyncio.run(_drain(source, seconds or None, out, raw, quiet=raw))


@app.command()
def level(
    device: Optional[str] = typer.Option(None, help="Device name substring or index."),
    seconds: float = typer.Option(5.0),
) -> None:
    """Live input level meter -- the fastest way to confirm the mic actually works."""
    from .mic import MicSource

    async def run() -> None:
        source = MicSource(device=device)
        await source.start()
        elapsed = 0.0
        try:
            async for frame in source.run():
                elapsed += frame.duration
                level_db = db(rms(frame.pcm))
                bars = int(max(0.0, (level_db + 60) / 60) * 40)
                sys.stderr.write(f"\r{level_db:7.1f} dBFS |{'#' * bars:<40}|")
                sys.stderr.flush()
                if elapsed >= seconds:
                    break
        finally:
            await source.stop()
            sys.stderr.write("\n")

    asyncio.run(run())


if __name__ == "__main__":
    app()

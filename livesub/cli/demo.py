"""``livesub demo`` -- the live demonstration: play the audio aloud, caption it live.

The spec requires showing speech going in and bilingual subtitles coming out, with
the delay visible. This runs the real pipeline against a real file, plays that file
through the speakers, and prints English and the translation as they are produced.

Two details make it an honest demonstration rather than a rehearsed one:

**Models are warmed before playback starts.** Loading a local LLM and a Whisper model
takes several seconds on a cold process, and Whisper in particular loads lazily on its
first call. If audio began at the same moment, every subtitle would appear that much late
and the delay on screen would be a measurement of the model loader, not of the pipeline.
So a dummy inference is forced through every stage first, and only then does the audio
start. The latency you see afterwards is the real one.

**Playback and the pipeline read the same file, started together.** The pipeline does not
listen to the speakers -- that would need a loopback device and would add the sound card's
own latency to the number being demonstrated. Both are paced at wall-clock speed from the
same instant, so what you hear and what you read line up.

To caption actual speaker output instead (a video call, a browser tab), use the loopback
device path documented in the README -- that is a different demonstration, of the input
module rather than of the latency.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np
import typer

from ..core.audioutil import save_wav
from ..core.config import load_config
from ..core.graph import Graph
from ..core.registry import resolve
from ..core.types import SAMPLE_RATE, AudioFrame, TextFrame, Utterance


def _slice_to_wav(source: Path, start: float, seconds: float) -> Path:
    """Normalise (and optionally cut) the source to a 16 kHz mono wav.

    Done up front rather than streaming through ffmpeg so that the audio player and the
    pipeline are reading byte-identical audio, and so a slice starts instantly.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH; `brew install ffmpeg`")
    out = Path(tempfile.gettempdir()) / f"livesub_demo_{os.getpid()}.wav"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if start:
        cmd += ["-ss", str(start)]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-i", str(source), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), str(out)]
    subprocess.run(cmd, check=True)
    return out


async def _warm(graph: Graph, target: str) -> float:
    """Force every model to load and run once. Returns seconds spent.

    Dispatches on the module's declared port types rather than on class hierarchy:
    an Utterance input is a transcriber (warm via the one-shot path), a TextFrame
    input is an LLM text stage (warm with a probe frame), an AudioFrame in-and-out is
    a denoiser (warm with one silent frame).
    """
    t0 = time.perf_counter()
    silence = np.zeros(SAMPLE_RATE, dtype=np.float32)  # 1 s, long enough to not be skipped
    loop = asyncio.get_running_loop()

    for node in graph.nodes:
        stage = node.stage
        cls = type(stage)
        in_types = set(cls.inputs.values())
        with contextlib.suppress(Exception):
            if Utterance in in_types and hasattr(stage, "transcribe_array"):
                await loop.run_in_executor(
                    None, stage.transcribe_array, silence, SAMPLE_RATE
                )
            elif TextFrame in in_types:
                from dataclasses import replace
                from ..core.types import Lineage

                probe = TextFrame(
                    text="This is a warm up sentence.",
                    lineage=replace(Lineage.new(segment_id="_warmup"),
                                    t_audio_end=time.time()),
                )
                await stage.process(probe)
            elif AudioFrame in in_types and AudioFrame in set(cls.outputs.values()):
                frame = AudioFrame(np.zeros(320, dtype=np.float32), SAMPLE_RATE, 0)
                await loop.run_in_executor(None, stage.process, frame)
    return time.perf_counter() - t0


def _play(path: Path) -> subprocess.Popen | None:
    """Start audible playback. ``afplay`` on macOS, ``ffplay`` elsewhere."""
    if shutil.which("afplay"):
        cmd = ["afplay", str(path)]
    elif shutil.which("ffplay"):
        cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)]
    else:
        return None
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def register(app: typer.Typer) -> None:
    @app.command()
    def demo(
        infile: Path = typer.Option(..., "--in", help="Audio or video file to caption."),
        target: str = typer.Option("vi", help="Target language: vi, zh, ..."),
        config: Path = typer.Option(
            Path("config/default.toml"), "--config", "-c",
            help="Wiring to demonstrate (config/mlx.toml / config/fused.toml for Apple Silicon).",
        ),
        start: float = typer.Option(0.0, help="Skip this many seconds in."),
        seconds: float = typer.Option(
            90.0, help="Length to demo; 0 plays the whole file."
        ),
        audio: bool = typer.Option(True, help="Play the audio aloud."),
        model: Optional[str] = typer.Option(None, help="Override the LLM model id."),
    ) -> None:
        """Play a file aloud and print live bilingual subtitles beside it."""
        if not infile.exists():
            typer.secho(f"no such file: {infile}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2)

        typer.secho("preparing audio...", fg=typer.colors.BLUE, err=True)
        clip = _slice_to_wav(infile, start, seconds)

        cfg = load_config(config)
        for node in cfg.nodes:
            if not node.inputs:  # a source: declares no input ports
                node.impl = "wav"
                node.outputs = {"audio": "audio.raw"}
                node.inputs = {}
                node.options = {"path": str(clip), "realtime": True}
            if node.impl.endswith("_translator") or node.impl.startswith("fused"):
                node.options["target"] = target
            if (node.impl.endswith(("_corrector", "_translator"))
                    or node.impl.startswith("fused")) and model:
                node.options["model"] = model
        graph = Graph(cfg)

        async def go() -> None:
            # graph.start(), not node.stage.start(): it records what was started, so
            # graph.run() below will not start (and re-bind / re-load) anything twice.
            await graph.start()
            typer.secho(
                "loading models (playback starts once they are warm)...",
                fg=typer.colors.BLUE, err=True,
            )
            warm = await _warm(graph, target)
            typer.secho(f"ready in {warm:.1f}s\n", fg=typer.colors.GREEN, err=True)

            player = _play(clip) if audio else None
            if audio and player is None:
                typer.secho("no audio player found; running silently",
                            fg=typer.colors.YELLOW, err=True)
            typer.secho(
                f"--- playing {infile.name} | subtitles EN + {target.upper()} "
                f"| Ctrl-C to stop ---\n",
                fg=typer.colors.BLUE, err=True,
            )
            try:
                await graph.run()
            finally:
                if player is not None and player.poll() is None:
                    player.terminate()

        try:
            asyncio.run(go())
        except KeyboardInterrupt:
            pass
        finally:
            with contextlib.suppress(OSError):
                clip.unlink()

        typer.secho(graph.metrics.format_table(), err=True)

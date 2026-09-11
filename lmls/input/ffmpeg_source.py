"""Everything that is not a plain microphone, normalised through one ffmpeg subprocess.

One adapter covers local video and audio files, HTTP/HLS/RTSP/RTMP stream URLs, and
macOS ``avfoundation`` capture devices, because ffmpeg already solves demuxing, decoding,
channel downmixing and resampling for all of them. The spec's "media and data
formats" question is answered here: whatever goes in, what comes out of this stage is
always 16 kHz mono float32, identical to what the microphone produces, so no downstream
module can tell the difference.

Live sources are read at their natural rate. Files are read with ``-re`` by default so a
recorded lecture replays at wall-clock speed and the measured latency means the same
thing it would live; ``realtime=false`` lifts that for fast benchmark runs.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from typing import Any, AsyncIterator, ClassVar

import numpy as np

from ..core.interfaces import Module
from ..core.types import FRAME_SAMPLES, SAMPLE_RATE, AudioFrame


class FfmpegSource(Module):
    """Decode any ffmpeg-readable source to the canonical frame format.

    Args:
        url: file path, stream URL, or -- with ``device=True`` -- an avfoundation audio
            device name or index such as ``"Background Music"``.
        device: treat ``url`` as an ``avfoundation`` capture device (macOS).
        realtime: throttle file playback to wall-clock speed (``-re``). Ignored for
            devices and live network streams, which are inherently realtime.
        extra_args: raw ffmpeg arguments inserted before the input, for anything this
            wrapper does not expose.
    """

    inputs: ClassVar[dict[str, type]] = {}
    outputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}

    def __init__(
        self,
        url: str | None = None,
        source: str | None = None,
        device: bool = False,
        realtime: bool = True,
        loop: bool = False,
        ffmpeg: str = "ffmpeg",
        extra_args: list[str] | None = None,
        **_: Any,
    ):
        super().__init__()
        self.url = url or source
        if not self.url:
            raise ValueError("ffmpeg source needs 'url' (or 'source')")
        self.device = device or _looks_like_device(self.url)
        self.realtime = realtime
        self.loop = loop
        self.ffmpeg = ffmpeg
        self.extra_args = extra_args or []
        self._proc: asyncio.subprocess.Process | None = None
        self._seq = 0
        self._stderr_tail: list[str] = []

    # -- device discovery -----------------------------------------------------

    @staticmethod
    def list_devices(ffmpeg: str = "ffmpeg") -> list[dict[str, Any]]:
        """Parse ``ffmpeg -f avfoundation -list_devices true`` (macOS).

        ffmpeg exits non-zero after printing the list -- that is expected, not a failure.
        """
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-f", "avfoundation", "-list_devices", "true",
             "-i", ""],
            capture_output=True,
            text=True,
        )
        devices: list[dict[str, Any]] = []
        section = None
        for line in proc.stderr.splitlines():
            if "AVFoundation video devices" in line:
                section = "video"
                continue
            if "AVFoundation audio devices" in line:
                section = "audio"
                continue
            if section and "] [" in line:
                try:
                    idx = line.split("] [", 1)[1].split("]", 1)[0]
                    name = line.split("] ", 2)[-1].strip()
                    devices.append({"kind": section, "index": int(idx), "name": name})
                except (ValueError, IndexError):
                    continue
        return [d for d in devices if d["kind"] == "audio"]

    @classmethod
    def _resolve_device(cls, name: str, ffmpeg: str) -> str:
        """avfoundation wants ``:<audio-index>``; accept a human name too."""
        if name.startswith(":"):
            return name
        if name.isdigit():
            return f":{name}"
        for d in cls.list_devices(ffmpeg):
            if name.lower() in d["name"].lower():
                return f":{d['index']}"
        available = ", ".join(repr(d["name"]) for d in cls.list_devices(ffmpeg))
        raise ValueError(f"no avfoundation audio device matching {name!r}; have: {available}")

    # -- lifecycle ------------------------------------------------------------

    def _build_command(self) -> list[str]:
        if shutil.which(self.ffmpeg) is None:
            raise RuntimeError(
                f"{self.ffmpeg!r} not found on PATH; install it with `brew install ffmpeg`"
            )
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if self.device:
            cmd += ["-f", "avfoundation", "-i",
                    self._resolve_device(str(self.url), self.ffmpeg)]
        else:
            if self.loop:
                cmd += ["-stream_loop", "-1"]
            if self.realtime:
                cmd += ["-re"]  # pace a file at wall-clock speed
            cmd += self.extra_args + ["-i", str(self.url)]
        # -vn drops any video track; the rest is the canonical output format.
        cmd += ["-vn", "-map", "a", "-f", "f32le", "-acodec", "pcm_f32le",
                "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
        return cmd

    async def start(self) -> None:
        cmd = self._build_command()
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        async for line in self._proc.stderr:
            text = line.decode(errors="replace").rstrip()
            if text:
                self._stderr_tail = (self._stderr_tail + [text])[-10:]

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2)
            except asyncio.TimeoutError:  # pragma: no cover
                self._proc.kill()
        self._proc = None

    # -- streaming ------------------------------------------------------------

    async def run(self) -> AsyncIterator[AudioFrame]:
        if self._proc is None:
            await self.start()
        assert self._proc and self._proc.stdout
        nbytes = FRAME_SAMPLES * 4
        while True:
            buf = await _read_exactly(self._proc.stdout, nbytes)
            if not buf:
                break
            if len(buf) < nbytes:
                buf = buf + b"\x00" * (nbytes - len(buf))
            yield AudioFrame(
                np.frombuffer(buf, dtype=np.float32).copy(),
                SAMPLE_RATE,
                self._seq,
                time.time(),
            )
            self._seq += 1

        rc = await self._proc.wait() if self._proc else 0
        if rc not in (0, None) and self._stderr_tail:
            raise RuntimeError(
                f"ffmpeg exited {rc} reading {self.url!r}:\n  "
                + "\n  ".join(self._stderr_tail)
            )

    def describe(self) -> dict[str, Any]:
        return {
            "stage": "FfmpegSource",
            "name": self.name,
            "url": self.url,
            "device": self.device,
            "realtime": self.realtime,
        }


async def _read_exactly(stream: asyncio.StreamReader, n: int) -> bytes:
    """``readexactly`` that returns a short final chunk instead of raising at EOF."""
    try:
        return await stream.readexactly(n)
    except asyncio.IncompleteReadError as exc:
        return bytes(exc.partial)


def _looks_like_device(url: str) -> bool:
    return url.startswith(":")

"""Everything that is not a plain microphone, normalised through one ffmpeg subprocess.

One adapter covers local video and audio files, HTTP/HLS/RTSP/RTMP stream URLs,
macOS ``avfoundation`` capture devices, and Linux PipeWire/PulseAudio capture,
because ffmpeg already solves demuxing, decoding, channel downmixing and resampling
for all of them. The spec's "media and data
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

#: names that mean "record the default output monitor" (all application audio)
_MONITOR_ALIASES = ("monitor", "default", "system")


class FfmpegSource(Module):
    """Decode any ffmpeg-readable source to the canonical frame format.

    Args:
        url: file path or stream URL; with ``device=True`` an ``avfoundation`` audio
            device name or index such as ``"Background Music"`` (macOS). Optional when
            ``pulse`` is set.
        device: treat ``url`` as an ``avfoundation`` capture device (macOS).
        pulse: capture system/application audio on Linux through PipeWire's PulseAudio
            interface. ``True`` (or ``"monitor"``) records the default output monitor,
            which hears *every application*. A string is resolved against ``pactl``:
            first as a literal source name, then as a sink substring (a ``.monitor``
            source is derived), then as a source substring.
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
        pulse: str | bool | None = None,
        realtime: bool = True,
        loop: bool = False,
        ffmpeg: str = "ffmpeg",
        extra_args: list[str] | None = None,
        **_: Any,
    ):
        super().__init__()
        self.url = url or source
        # False/'' mean "not capturing system audio", exactly like None; that keeps
        # editor round-trips (a select's empty choice) lossless through TOML export.
        self.pulse = pulse or None
        if not self.url and not self.pulse:
            raise ValueError("ffmpeg source needs 'url' (or 'source')")
        self.device = device or _looks_like_device(self.url or "")
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
    def option_choices(cls, name: str) -> list[str] | None:
        """Choices the editor offers for an option; ``None`` when free-form.

        ``pulse`` lists the default-monitor alias plus every source the server can
        see, so the studio can offer real devices instead of a blank text field.
        """
        if name != 'pulse':
            return None
        choices, seen = ['monitor'], {'monitor'}
        for d in cls.list_pulse_sources():
            # Sinks are skipped: each sink's ``.monitor`` source is already listed,
            # and both names resolve to the same capture.
            if d['kind'] == 'source' and d['name'] not in seen:
                seen.add(d['name'])
                choices.append(d['name'])
        return choices

    @staticmethod
    def list_pulse_sources(pactl: str = "pactl") -> list[dict[str, Any]]:
        """List PipeWire/PulseAudio sources and sinks via ``pactl`` (Linux).

        Sinks are listed too because capturing an application's output means
        recording the *monitor* source of the sink it plays into. Returns ``[]``
        when ``pactl`` is unavailable (e.g. macOS), so callers can degrade quietly.
        """
        if shutil.which(pactl) is None:
            return []
        out: list[dict[str, Any]] = []
        for kind, args in (
            ("source", [pactl, "list", "sources", "short"]),
            ("sink", [pactl, "list", "sinks", "short"]),
        ):
            proc = subprocess.run(args, capture_output=True, text=True)
            for line in proc.stdout.splitlines():
                cols = line.split("\t")
                if len(cols) >= 2 and cols[1]:
                    out.append(
                        {
                            "kind": kind,
                            "name": cols[1],
                            "monitor": cols[1].endswith(".monitor"),
                        }
                    )
        return out

    @classmethod
    def _resolve_pulse(cls, name: str | bool, pactl: str = "pactl") -> str:
        """Map a ``pulse`` option to a pulse source name.

        The pulse server understands ``@DEFAULT_MONITOR@`` itself, so ``True`` needs
        no lookup; a human-friendly substring only can be resolved when ``pactl`` is
        present, and is passed through verbatim otherwise (ffmpeg will report the
        failure with the server's own device list).
        """
        if name is True or str(name).lower() in _MONITOR_ALIASES:
            return "@DEFAULT_MONITOR@"
        name = str(name)
        devices = cls.list_pulse_sources(pactl)
        sources = [d["name"] for d in devices if d["kind"] == "source"]
        sinks = [d["name"] for d in devices if d["kind"] == "sink"]
        if name in sources:
            return name
        if not name.endswith(".monitor"):
            for sink in sinks:
                if name.lower() in sink.lower():
                    return f"{sink}.monitor"
        for src in sources:
            if name.lower() in src.lower():
                return src
        if not devices:  # no pactl -- let the pulse server validate the name
            return name
        available = ", ".join(
            f"{d['name']}{' (sink)' if d['kind'] == 'sink' else ''}"
            for d in devices
        )
        raise ValueError(f"no pulse source matching {name!r}; have: {available}")

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
        if self.pulse:
            cmd += self.extra_args + ["-f", "pulse", "-i",
                                      self._resolve_pulse(self.pulse)]
        elif self.device:
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
            "pulse": self.pulse or None,
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

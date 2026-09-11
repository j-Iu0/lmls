"""System microphone capture via PortAudio (``sounddevice``).

Chosen over reading the mic through ffmpeg because the callback API hands us buffers
directly from CoreAudio with no subprocess or container in between -- roughly one buffer
period of latency instead of ffmpeg's internal queueing. The ffmpeg source exists for
everything that is *not* a plain microphone.

The PortAudio callback runs on a realtime thread that must never block. It therefore does
the absolute minimum -- copy, push to a thread-safe queue -- and all resampling and
framing happen on the asyncio side.
"""

from __future__ import annotations

import asyncio
import queue
import time
from typing import Any, AsyncIterator, ClassVar

import numpy as np

from ..core.audioutil import resample, to_mono
from ..core.interfaces import Module
from ..core.types import FRAME_SAMPLES, SAMPLE_RATE, AudioFrame


class MicSource(Module):
    """Live capture from an input device.

    Args:
        device: device name substring or PortAudio index; ``None`` uses the system
            default. On macOS, ``"Background Music"`` (or BlackHole) is a virtual device
            that carries *system output* -- that is how a video call or a playing video
            gets captured rather than the room.
        samplerate: device rate to request. ``None`` asks the device for its own default
            (48 kHz on Apple hardware) and resamples to 16 kHz here.
        block_ms: PortAudio buffer period. Smaller means lower capture latency and more
            callbacks; 20 ms matches our frame size exactly.
        max_queue: bounded backlog, in frames, between the audio thread and asyncio. If
            the consumer stalls, old audio is dropped rather than allowed to grow.
    """

    inputs: ClassVar[dict[str, type]] = {}
    outputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}

    def __init__(
        self,
        device: str | int | None = None,
        samplerate: int | None = None,
        block_ms: int = 20,
        channels: int = 1,
        max_queue: int = 256,
        **_: Any,
    ):
        super().__init__()
        self.device = device
        self.requested_rate = samplerate
        self.block_ms = block_ms
        self.channels = channels
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._stream = None
        self._device_rate = samplerate or SAMPLE_RATE
        self._residual = np.zeros(0, dtype=np.float32)
        self._seq = 0
        self.dropped_blocks = 0
        self._stopped = asyncio.Event()

    # -- device discovery -----------------------------------------------------

    @staticmethod
    def list_devices() -> list[dict[str, Any]]:
        import sounddevice as sd

        out = []
        default_in = sd.default.device[0]
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                out.append(
                    {
                        "index": i,
                        "name": d["name"],
                        "channels": d["max_input_channels"],
                        "samplerate": int(d["default_samplerate"]),
                        "default": i == default_in,
                    }
                )
        return out

    @classmethod
    def _resolve(cls, device: str | int | None) -> int | None:
        if device is None or isinstance(device, int):
            return device
        if device.isdigit():
            return int(device)
        if device.lower() == "default":
            return None
        matches = [
            d for d in cls.list_devices() if device.lower() in d["name"].lower()
        ]
        if not matches:
            names = ", ".join(repr(d["name"]) for d in cls.list_devices())
            raise ValueError(f"no input device matching {device!r}; available: {names}")
        return matches[0]["index"]

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        import sounddevice as sd

        index = self._resolve(self.device)
        info = sd.query_devices(index if index is not None else sd.default.device[0])
        self._device_rate = self.requested_rate or int(info["default_samplerate"])
        blocksize = int(self._device_rate * self.block_ms / 1000)

        def callback(indata, _frames, _time_info, status):  # runs on the audio thread
            if status:  # overflow/underflow reported by PortAudio
                self.dropped_blocks += 1
            try:
                self._queue.put_nowait((indata.copy(), time.time()))
            except queue.Full:
                self.dropped_blocks += 1

        self._stream = sd.InputStream(
            device=index,
            channels=min(self.channels, int(info["max_input_channels"])),
            samplerate=self._device_rate,
            blocksize=blocksize,
            dtype="float32",
            callback=callback,
        )
        self._stream.start()

    async def stop(self) -> None:
        self._stopped.set()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    # -- streaming ------------------------------------------------------------

    async def run(self) -> AsyncIterator[AudioFrame]:
        if self._stream is None:
            await self.start()
        loop = asyncio.get_running_loop()
        while not self._stopped.is_set():
            try:
                block, captured_at = await loop.run_in_executor(
                    None, self._queue.get, True, 0.5
                )
            except queue.Empty:
                continue
            mono = resample(to_mono(block), self._device_rate, SAMPLE_RATE)
            self._residual = np.concatenate([self._residual, mono])
            while len(self._residual) >= FRAME_SAMPLES:
                chunk, self._residual = (
                    self._residual[:FRAME_SAMPLES],
                    self._residual[FRAME_SAMPLES:],
                )
                yield AudioFrame(chunk, SAMPLE_RATE, self._seq, captured_at)
                self._seq += 1

    def describe(self) -> dict[str, Any]:
        return {
            "stage": "MicSource",
            "name": self.name,
            "device": self.device or "default",
            "device_rate": self._device_rate,
            "dropped_blocks": self.dropped_blocks,
        }

"""Replay an audio file as if it were arriving live.

This is the source every reproducible measurement uses. A microphone cannot produce the
same audio twice, so denoise-on vs denoise-off word error rates, or local vs cloud
latency, could never be compared fairly against it. Replaying a fixture file at
wall-clock speed gives the rest of the pipeline a stream indistinguishable from a live
one while keeping the input byte-identical between runs.

``realtime=False`` drops the pacing and pushes frames as fast as they are consumed, which
is what the test suite and the offline benchmark use.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, AsyncIterator, ClassVar

import numpy as np

from ..core.audioutil import load_wav
from ..core.interfaces import Module
from ..core.types import FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE, AudioFrame


class WavReplaySource(Module):
    """Stream a file from disk as canonical frames.

    Args:
        path: any libsndfile-readable audio file. (For video containers or exotic
            codecs, use the ffmpeg source instead.)
        realtime: pace output at wall-clock speed.
        loop: restart at the end, for long soak tests.
        pad_tail_ms: trailing silence appended once the file ends, so the segmenter sees
            an end-of-speech boundary and flushes the final utterance instead of leaving
            it unterminated.
    """

    inputs: ClassVar[dict[str, type]] = {}
    outputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}

    def __init__(
        self,
        path: str | Path | None = None,
        realtime: bool = True,
        loop: bool = False,
        pad_tail_ms: int = 600,
        **_: Any,
    ):
        super().__init__()
        if path is None:
            raise ValueError("wav source needs 'path'")
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"audio file not found: {self.path}")
        self.realtime = realtime
        self.loop = loop
        self.pad_tail_ms = pad_tail_ms
        self._pcm: np.ndarray | None = None
        self._seq = 0

    async def start(self) -> None:
        self._pcm = load_wav(self.path, SAMPLE_RATE)

    @property
    def duration(self) -> float:
        if self._pcm is None:
            self._pcm = load_wav(self.path, SAMPLE_RATE)
        return len(self._pcm) / SAMPLE_RATE

    async def run(self) -> AsyncIterator[AudioFrame]:
        if self._pcm is None:
            await self.start()
        assert self._pcm is not None

        pad = np.zeros(int(SAMPLE_RATE * self.pad_tail_ms / 1000), dtype=np.float32)
        frame_period = FRAME_MS / 1000
        started = time.time()

        while True:
            pcm = np.concatenate([self._pcm, pad])
            for offset in range(0, len(pcm), FRAME_SAMPLES):
                block = pcm[offset : offset + FRAME_SAMPLES]
                if len(block) < FRAME_SAMPLES:
                    block = np.pad(block, (0, FRAME_SAMPLES - len(block)))
                if self.realtime:
                    # Sleep until this frame's scheduled time rather than sleeping a
                    # fixed 20 ms each iteration, so processing time does not accumulate
                    # into drift over a long file.
                    due = started + self._seq * frame_period
                    delay = due - time.time()
                    if delay > 0:
                        await asyncio.sleep(delay)
                else:
                    await asyncio.sleep(0)  # stay cooperative
                yield AudioFrame(block.copy(), SAMPLE_RATE, self._seq, time.time())
                self._seq += 1
            if not self.loop:
                return
            started = time.time()
            self._seq = 0

    def describe(self) -> dict[str, Any]:
        return {
            "stage": "WavReplaySource",
            "name": self.name,
            "path": str(self.path),
            "realtime": self.realtime,
            "duration_s": round(self.duration, 2),
        }

"""Read canonical PCM frames from stdin.

This is the adapter that lets the graph be spliced into a Unix pipe -- it is how

    lmls-input mic --raw | lmls-denoise spectral --raw | lmls-transcribe ...

works. The upstream process is just another audio source as far as the graph is
concerned.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, ClassVar

from ..core.codec import aread_frames
from ..core.interfaces import Module
from ..core.types import AudioFrame


class StdinPcmSource(Module):
    """16 kHz mono float32 PCM on standard input."""

    inputs: ClassVar[dict[str, type]] = {}
    outputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}

    def __init__(self, **_: Any):
        super().__init__()

    async def run(self) -> AsyncIterator[AudioFrame]:
        async for frame in aread_frames():
            yield frame

    def describe(self) -> dict[str, Any]:
        return {"stage": "StdinPcmSource", "name": self.name, "format": "f32le/16k/mono"}

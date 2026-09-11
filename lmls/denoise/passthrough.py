"""No-op denoiser.

Not filler: this is the control condition. Every claim about what noise reduction buys is
measured against this, and ``config/no_denoise.toml`` removes the node entirely to prove
the graph runs without it. Also the right choice on a clean close-talk microphone, where
suppression can remove more speech than noise.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from ..core.interfaces import Module
from ..core.types import AudioFrame


class PassthroughDenoiser(Module):
    """Returns the frame unchanged. Zero added latency."""

    inputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}
    outputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}

    latency_ms = 0.0

    def __init__(self, **_: Any):
        super().__init__()

    def process(self, frame: AudioFrame) -> AudioFrame:
        return frame

    def process_array(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray:
        return pcm.astype(np.float32, copy=False)

    def describe(self) -> dict[str, Any]:
        return {"stage": "PassthroughDenoiser", "name": self.name, "latency_ms": 0.0}

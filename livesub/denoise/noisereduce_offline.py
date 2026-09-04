"""``noisereduce``'s whole-signal spectral gate, for offline comparison.

Included so the report can compare the streaming gate against a well-known reference
implementation on identical audio, rather than only against "no denoising".

It is deliberately **not** a good live choice, and the code says so rather than hiding it:
``reduce_noise`` wants the whole signal to build its noise profile. Driving it frame by
frame means re-estimating the profile from 20 ms of audio each time -- expensive and
discontinuous. Here the streaming path buffers ``chunk_ms`` of audio and processes it in
blocks, which works but adds that whole buffer to the latency budget. Use it on files;
prefer ``spectral`` live.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from ..core.interfaces import Module
from ..core.types import SAMPLE_RATE, AudioFrame


class NoiseReduceDenoiser(Module):
    """Args:
        stationary: assume a fixed noise profile (fan, hum) rather than tracking it.
            Non-stationary mode adapts to babble but costs noticeably more CPU.
        prop_decrease: fraction of the estimated noise to remove, 0..1.
        chunk_ms: streaming buffer size. This lands directly in the latency budget.
    """

    inputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}
    outputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}

    def __init__(
        self,
        stationary: bool = True,
        prop_decrease: float = 0.8,
        chunk_ms: int = 480,
        **_: Any,
    ):
        super().__init__()
        import noisereduce  # fail here, at construction, not at import of the package

        self._nr = noisereduce
        self.stationary = stationary
        self.prop_decrease = prop_decrease
        self.chunk_ms = chunk_ms
        self._chunk = int(SAMPLE_RATE * chunk_ms / 1000)
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_buf = np.zeros(0, dtype=np.float32)

    @property
    def latency_ms(self) -> float:
        return float(self.chunk_ms)

    def process(self, frame: AudioFrame) -> AudioFrame:
        self._in_buf = np.concatenate([self._in_buf, frame.pcm])
        while len(self._in_buf) >= self._chunk:
            block, self._in_buf = self._in_buf[: self._chunk], self._in_buf[self._chunk :]
            self._out_buf = np.concatenate(
                [self._out_buf, self.process_array(block, SAMPLE_RATE)]
            )
        n = len(frame.pcm)
        if len(self._out_buf) >= n:
            out, self._out_buf = self._out_buf[:n], self._out_buf[n:]
        else:
            out = np.zeros(n, dtype=np.float32)  # warm-up
        return AudioFrame(out, frame.sample_rate, frame.seq, frame.t_capture)

    def process_array(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray:
        return self._nr.reduce_noise(
            y=pcm.astype(np.float32),
            sr=sample_rate,
            stationary=self.stationary,
            prop_decrease=self.prop_decrease,
        ).astype(np.float32)

    def describe(self) -> dict[str, Any]:
        return {
            "stage": "NoiseReduceDenoiser",
            "name": self.name,
            "stationary": self.stationary,
            "prop_decrease": self.prop_decrease,
            "latency_ms": self.latency_ms,
        }

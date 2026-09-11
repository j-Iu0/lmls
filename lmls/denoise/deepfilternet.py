"""DeepFilterNet3 adapter (optional, heavier, better on non-stationary noise).

Where the spectral gate computes one gain per frequency bin, DeepFilterNet predicts a
complex filter per bin, which is what lets it suppress overlapping background speech and
sudden clatter rather than only steady hum. The trade is roughly 40 ms of algorithmic
latency against the streaming gate's 20 ms, plus a real neural forward pass per frame.

Not installed by default -- ``pip install deepfilternet`` pulls in torch. Registered so
the report's noise comparison can include it on machines that have it.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from ..core.audioutil import resample
from ..core.interfaces import Module
from ..core.startup import StartupPhase
from ..core.types import SAMPLE_RATE, AudioFrame

_DF_RATE = 48_000  # DeepFilterNet is trained at 48 kHz


class DeepFilterNetDenoiser(Module):
    """Args:
        chunk_ms: audio buffered per forward pass. DeepFilterNet's own hop is 10 ms, but
            calling it per 20 ms frame from Python costs more in overhead than in
            compute, so a slightly larger block is the sane default.
        atten_lim_db: cap on suppression. Unlimited suppression sounds cleaner to a human
            but removes low-energy consonants an ASR relies on; 20-30 dB is a good range.
    """

    inputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}
    outputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}

    def __init__(self, chunk_ms: int = 40, atten_lim_db: float = 25.0, **_: Any):
        super().__init__()
        try:
            from df.enhance import enhance, init_df  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError(
                "DeepFilterNet is not installed. `pip install deepfilternet` "
                "(pulls in torch), or use denoise impl 'spectral'."
            ) from exc

        import torch  # type: ignore

        self._torch = torch
        self._enhance = enhance
        self._model, self._state, _ = init_df()
        self.chunk_ms = chunk_ms
        self.atten_lim_db = atten_lim_db
        self._chunk = int(SAMPLE_RATE * chunk_ms / 1000)
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_buf = np.zeros(0, dtype=np.float32)

    async def start(self) -> None:
        # Model loaded during __init__; just signal readiness.
        self._report_startup(StartupPhase.READY)

    @property
    def latency_ms(self) -> float:
        return float(self.chunk_ms) + 40.0  # buffering + the model's own look-ahead

    def _enhance_block(self, pcm: np.ndarray) -> np.ndarray:
        up = resample(pcm, SAMPLE_RATE, _DF_RATE)
        tensor = self._torch.from_numpy(up).unsqueeze(0)
        out = self._enhance(self._model, self._state, tensor,
                            atten_lim_db=self.atten_lim_db)
        return resample(out.squeeze(0).numpy().astype(np.float32), _DF_RATE, SAMPLE_RATE)

    def process(self, frame: AudioFrame) -> AudioFrame:
        self._in_buf = np.concatenate([self._in_buf, frame.pcm])
        while len(self._in_buf) >= self._chunk:
            block, self._in_buf = self._in_buf[: self._chunk], self._in_buf[self._chunk :]
            self._out_buf = np.concatenate([self._out_buf, self._enhance_block(block)])
        n = len(frame.pcm)
        if len(self._out_buf) >= n:
            out, self._out_buf = self._out_buf[:n], self._out_buf[n:]
        else:
            out = np.zeros(n, dtype=np.float32)
        return AudioFrame(out, frame.sample_rate, frame.seq, frame.t_capture)

    def process_array(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray:
        return self._enhance_block(resample(pcm, sample_rate, SAMPLE_RATE))

    def describe(self) -> dict[str, Any]:
        return {
            "stage": "DeepFilterNetDenoiser",
            "name": self.name,
            "atten_lim_db": self.atten_lim_db,
            "latency_ms": self.latency_ms,
        }

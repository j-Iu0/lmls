"""Streaming spectral-subtraction noise gate (STFT domain).

Why this is written out rather than calling ``noisereduce.reduce_noise`` per frame: that
function is designed for whole signals. Called on 20 ms slices it re-estimates the noise
profile from each slice, which both costs far more CPU than necessary and produces
audible discontinuities at slice edges. The ``noisereduce`` implementation is still
available as a separate registered adapter for the *offline* comparison in the benchmark
(``denoise.noisereduce``), where its whole-signal design is the right one.

Design:

* 40 ms Hann window, 20 ms hop -- exactly 50% overlap, which satisfies the constant
  overlap-add condition so resynthesis is transparent when the gain is 1.
* One FFT per incoming frame in steady state, and exactly one hop (20 ms) of algorithmic
  latency. That is the honest number to put in the report; it is the delay this stage
  adds to the 3-second budget.
* Noise magnitude is tracked per frequency bin with an asymmetric follower -- fast down,
  slow up. Noise floors drop quickly when a fan is switched off but should never chase a
  held vowel upward, which is what makes speech eat its own noise estimate and get
  suppressed.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from ..core.interfaces import Module
from ..core.types import FRAME_SAMPLES, AudioFrame

_WINDOW = 2 * FRAME_SAMPLES  # 640 samples = 40 ms
_HOP = FRAME_SAMPLES  # 320 samples = 20 ms


class SpectralDenoiser(Module):
    """Per-bin spectral gate with an adaptive noise estimate.

    Args:
        over_subtraction: how many times the estimated noise magnitude to remove. Above
            ~2.0 the residual "musical noise" (isolated surviving bins warbling between
            frames) becomes worse for the ASR than the noise it replaced.
        floor_gain: minimum gain per bin. Gating fully to zero is what creates musical
            noise; leaving a -20 dB floor keeps the residual sounding like quiet room
            tone, which Whisper handles far better than silence punctuated by artefacts.
        smoothing: temporal smoothing of the gain mask between frames (0 = none).
        init_frames: frames at startup assumed to be noise, used to seed the estimate.
            The first fifth of a second of a lecture is almost always room tone.
    """

    inputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}
    outputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}

    def __init__(
        self,
        over_subtraction: float = 1.5,
        floor_gain: float = 0.1,
        smoothing: float = 0.6,
        init_frames: int = 10,
        adapt_up: float = 0.995,
        adapt_down: float = 0.5,
        **_: Any,
    ):
        super().__init__()
        self.over_subtraction = over_subtraction
        self.floor_gain = floor_gain
        self.smoothing = smoothing
        self.init_frames = init_frames
        self.adapt_up = adapt_up
        self.adapt_down = adapt_down

        # Square-root Hann, applied on both analysis and synthesis. Their product is a
        # periodic Hann, which sums to exactly 1.0 at 50% overlap -- so a gain of 1
        # reconstructs the input sample-for-sample. Using a full Hann on both sides
        # instead would sum to 0.5*(1+cos^2) and impose a 3 dB amplitude ripple at the
        # hop rate on everything passing through.
        self._win = np.sqrt(
            np.hanning(_WINDOW + 1)[:_WINDOW].astype(np.float32)
        ).astype(np.float32)
        nbins = _WINDOW // 2 + 1
        self._noise = np.zeros(nbins, dtype=np.float32)
        self._prev_gain = np.ones(nbins, dtype=np.float32)
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_buf = np.zeros(_WINDOW, dtype=np.float32)
        self._frames_seen = 0

    @property
    def latency_ms(self) -> float:
        return 1000.0 * _HOP / 16_000  # one hop = 20 ms

    def reset(self) -> None:
        self._noise[:] = 0
        self._prev_gain[:] = 1
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_buf = np.zeros(_WINDOW, dtype=np.float32)
        self._frames_seen = 0

    def _update_noise(self, mag: np.ndarray) -> None:
        if self._frames_seen < self.init_frames:
            # Seed from the opening frames, assumed to be room tone.
            n = self._frames_seen + 1
            self._noise += (mag - self._noise) / n
            return
        going_down = mag < self._noise
        self._noise = np.where(
            going_down,
            self.adapt_down * mag + (1 - self.adapt_down) * self._noise,
            self.adapt_up * self._noise + (1 - self.adapt_up) * mag,
        ).astype(np.float32)

    def _gain_for(self, mag: np.ndarray) -> np.ndarray:
        subtracted = mag - self.over_subtraction * self._noise
        gain = np.clip(subtracted / np.maximum(mag, 1e-9), self.floor_gain, 1.0)
        gain = (
            self.smoothing * self._prev_gain + (1 - self.smoothing) * gain
        ).astype(np.float32)
        self._prev_gain = gain
        return gain

    def process(self, frame: AudioFrame) -> AudioFrame:
        self._in_buf = np.concatenate([self._in_buf, frame.pcm])
        produced = np.zeros(0, dtype=np.float32)

        while len(self._in_buf) >= _WINDOW:
            block = self._in_buf[:_WINDOW] * self._win
            spectrum = np.fft.rfft(block)
            mag = np.abs(spectrum).astype(np.float32)

            self._update_noise(mag)
            gain = self._gain_for(mag)
            self._frames_seen += 1

            cleaned = np.fft.irfft(spectrum * gain, n=_WINDOW).astype(np.float32)
            self._out_buf[:] += cleaned * self._win  # sqrt-Hann twice = Hann = COLA

            produced = np.concatenate([produced, self._out_buf[:_HOP].copy()])
            self._out_buf = np.concatenate(
                [self._out_buf[_HOP:], np.zeros(_HOP, dtype=np.float32)]
            )
            self._in_buf = self._in_buf[_HOP:]

        if len(produced) < len(frame.pcm):  # warm-up: emit silence, keep the frame count
            produced = np.pad(produced, (len(frame.pcm) - len(produced), 0))
        out = produced[-len(frame.pcm):]
        return AudioFrame(out.astype(np.float32), frame.sample_rate, frame.seq,
                          frame.t_capture)

    def describe(self) -> dict[str, Any]:
        return {
            "stage": "SpectralDenoiser",
            "name": self.name,
            "over_subtraction": self.over_subtraction,
            "floor_gain": self.floor_gain,
            "latency_ms": self.latency_ms,
        }

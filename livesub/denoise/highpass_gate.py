"""Cheapest useful noise handling: a high-pass filter plus a hysteretic noise gate.

Aimed squarely at the two most common classroom problems that are *not* babble --
ventilation rumble and desk/laptop-fan hum, which live almost entirely below 100 Hz where
there is no speech information to lose. Removing them before the ASR also stops them from
dominating the spectral denoiser's noise estimate.

The gate uses separate open/close thresholds and a hold time. A single threshold chatters
on and off during the quiet tail of a word and chops the ends off sentences, which costs
more word error rate than the noise it removes.

Adds no algorithmic latency: both stages are causal and operate in place.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

from ..core.audioutil import rms
from ..core.interfaces import Module
from ..core.types import SAMPLE_RATE, AudioFrame


class HighpassGateDenoiser(Module):
    """High-pass + noise gate.

    Args:
        cutoff_hz: high-pass corner. 80 Hz sits below the lowest male fundamental
            (~85 Hz) and well above nothing useful.
        order: filter order; 4 gives ~24 dB/octave.
        open_db: level above the running noise floor at which the gate opens.
        close_db: level at which it closes again -- lower than ``open_db`` on purpose.
        hold_ms: how long the gate stays open after the signal drops, so word tails and
            short pauses inside a sentence survive.
        floor_gain: attenuation applied when closed. Not zero: digital silence sounds
            unnatural to a Whisper model trained on real recordings, and total gaps can
            make it hallucinate. -20 dB is enough.
    """

    inputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}
    outputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}

    def __init__(
        self,
        cutoff_hz: float = 80.0,
        order: int = 4,
        open_db: float = 9.0,
        close_db: float = 5.0,
        hold_ms: float = 220.0,
        floor_gain: float = 0.1,
        noise_adapt: float = 0.05,
        **_: Any,
    ):
        super().__init__()
        self.cutoff_hz = cutoff_hz
        self.open_db = open_db
        self.close_db = close_db
        self.hold_ms = hold_ms
        self.floor_gain = floor_gain
        self.noise_adapt = noise_adapt

        self._sos = butter(order, cutoff_hz, btype="highpass", fs=SAMPLE_RATE,
                           output="sos")
        self._zi = sosfilt_zi(self._sos) * 0.0
        self._noise_rms = 1e-4
        self._open = False
        self._hold_left = 0.0
        self._gain = self.floor_gain

    @property
    def latency_ms(self) -> float:
        return 0.0

    def reset(self) -> None:
        self._zi = sosfilt_zi(self._sos) * 0.0
        self._noise_rms = 1e-4
        self._open = False
        self._hold_left = 0.0
        self._gain = self.floor_gain

    def process(self, frame: AudioFrame) -> AudioFrame:
        filtered, self._zi = sosfilt(self._sos, frame.pcm, zi=self._zi)
        filtered = filtered.astype(np.float32)

        level = rms(filtered)
        ratio_db = 20 * np.log10(max(level, 1e-9) / max(self._noise_rms, 1e-9))
        frame_ms = frame.duration * 1000

        if ratio_db > self.open_db:
            self._open = True
            self._hold_left = self.hold_ms
        elif ratio_db < self.close_db:
            self._hold_left -= frame_ms
            if self._hold_left <= 0:
                self._open = False

        if not self._open:
            # Only learn the noise floor while the gate is shut, so speech never gets
            # averaged into the estimate and pushed the threshold up over itself.
            self._noise_rms = (
                1 - self.noise_adapt
            ) * self._noise_rms + self.noise_adapt * level

        target = 1.0 if self._open else self.floor_gain
        # Ramp the gain across the frame instead of stepping it, which would click.
        ramp = np.linspace(self._gain, target, len(filtered), dtype=np.float32)
        self._gain = target
        out = filtered * ramp

        return AudioFrame(out, frame.sample_rate, frame.seq, frame.t_capture)

    def describe(self) -> dict[str, Any]:
        return {
            "stage": "HighpassGateDenoiser",
            "name": self.name,
            "cutoff_hz": self.cutoff_hz,
            "latency_ms": 0.0,
            "noise_floor_dbfs": round(20 * float(np.log10(max(self._noise_rms, 1e-12))), 1),
        }

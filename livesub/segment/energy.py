"""Adaptive energy + spectral-flatness voice activity detection.

The default segmenter, chosen so the base install needs neither torch nor onnxruntime.

Plain energy thresholding fails in a classroom: the absolute level depends on how far the
speaker is from the microphone, and a fan or projector raises the floor over the course of
a lecture. Two things fix that here.

*Adaptive floor.* The noise level is tracked continuously and only updated while the
detector believes it is *not* hearing speech, so a long sentence cannot drag the floor up
over itself. The decision is made on the ratio of frame level to that floor, not on an
absolute threshold, which makes it independent of microphone distance and gain.

*Spectral flatness.* Energy alone fires on door slams and keyboard clatter. Voiced speech
is strongly harmonic, so its spectrum is peaky (low flatness); broadband noise is flat.
Requiring both energy *and* peakiness rejects most impulsive classroom noise without
rejecting speech.

Hysteresis on the trigger (higher to start, lower to continue) stops the detector
chattering during the quiet parts of a word.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from ..core.audioutil import rms
from ..core.interfaces import Module
from ..core.types import AudioFrame, Utterance
from .base import Segmenter, SegmenterConfig


def spectral_flatness(pcm: np.ndarray) -> float:
    """Ratio of geometric to arithmetic mean of the power spectrum, in [0, 1].

    Near 0 for a harmonic (voiced) sound, near 1 for white noise.
    """
    spec = np.abs(np.fft.rfft(pcm * np.hanning(len(pcm)))) ** 2 + 1e-12
    return float(np.exp(np.mean(np.log(spec))) / np.mean(spec))


class _EnergySegmenterImpl(Segmenter):
    """Args:
        start_db: dB above the tracked noise floor needed to declare speech.
        continue_db: lower threshold used once speech has started (hysteresis).
        max_flatness: reject frames flatter than this even if they are loud -- that is
            broadband noise, not voice.
        adapt: how fast the noise floor follows the input while not in speech.
        absolute_floor_dbfs: a hard gate. Below this, nothing counts as speech no matter
            how it compares to the floor, which stops the detector from "adapting" down
            into digital silence and then triggering on its own dither.
    """

    def __init__(
        self,
        config: SegmenterConfig | None = None,
        start_db: float = 10.0,
        continue_db: float = 5.0,
        max_flatness: float = 0.45,
        adapt: float = 0.03,
        absolute_floor_dbfs: float = -55.0,
        **_: Any,
    ):
        super().__init__(config)
        self.start_db = start_db
        self.continue_db = continue_db
        self.max_flatness = max_flatness
        self.adapt = adapt
        self.absolute_floor = 10 ** (absolute_floor_dbfs / 20)
        self._floor = 1e-4

    def is_speech(self, frame: AudioFrame) -> bool:
        level = rms(frame.pcm)
        if level < self.absolute_floor:
            self._floor = (1 - self.adapt) * self._floor + self.adapt * level
            return False

        ratio_db = 20 * np.log10(max(level, 1e-9) / max(self._floor, 1e-9))
        threshold = self.continue_db if self._in_speech else self.start_db
        loud_enough = ratio_db > threshold
        voiced = spectral_flatness(frame.pcm) < self.max_flatness
        speech = bool(loud_enough and voiced)

        if not speech:
            self._floor = (1 - self.adapt) * self._floor + self.adapt * level
        return speech

    def describe(self) -> dict[str, Any]:
        return {
            "segmenter": "EnergySegmenter",
            "start_db": self.start_db,
            "noise_floor_dbfs": round(20 * float(np.log10(max(self._floor, 1e-12))), 1),
            **self.config.__dict__,
        }


class EnergySegmenter(Module):
    """Pipeline node wrapping the energy VAD segmenter.

    Declares the port shape that makes segmentation a first-class graph stage:
    audio frames in, utterances out. ``process`` returns a list because one frame
    can complete zero or more utterances; ``drain`` flushes the utterance still
    being accumulated when the audio ends.
    """

    inputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}
    outputs: ClassVar[dict[str, type]] = {"utterance": Utterance}

    def __init__(self, **kwargs: Any):
        super().__init__()
        timing_keys = set(SegmenterConfig.__dataclass_fields__)
        config = SegmenterConfig(**{k: v for k, v in kwargs.items() if k in timing_keys})
        rest = {k: v for k, v in kwargs.items() if k not in timing_keys}
        self._inner = _EnergySegmenterImpl(config, **rest)

    def process(self, frame: AudioFrame) -> list[Utterance]:
        return list(self._inner.push(frame))

    def drain(self) -> list[Utterance]:
        return list(self._inner.close())

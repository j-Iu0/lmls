"""Silero VAD segmenter -- a small neural voice activity detector.

Better than the energy detector in exactly the case this project cares about: a noisy
classroom where the background *is* speech. Energy and spectral flatness cannot tell the
lecturer from two students talking at the back, because both are harmonic and both are
loud; Silero was trained to, and it also handles a speaker whose level varies as they
move around the room.

The cost is a dependency (``silero-vad``, plus onnxruntime or torch) and roughly a
millisecond per 32 ms window on this hardware. Optional for that reason -- the energy
segmenter is the zero-dependency default.

Silero expects exactly 512-sample windows at 16 kHz, so incoming 320-sample frames are
re-blocked here and the frame inherits the most recent window's decision.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from ..core.interfaces import Module
from ..core.types import AudioFrame, Utterance
from .base import Segmenter, SegmenterConfig

_WINDOW = 512  # required by the model at 16 kHz


class _SileroSegmenterImpl(Segmenter):
    """Args:
        threshold: speech probability above which a window counts as speech.
        onnx: use the ONNX runtime rather than torch. Much lighter to install; prefer it
            unless torch is already present for another reason.
    """

    def __init__(
        self,
        config: SegmenterConfig | None = None,
        threshold: float = 0.5,
        onnx: bool = True,
        **_: Any,
    ):
        super().__init__(config)
        try:
            from silero_vad import load_silero_vad  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError(
                "silero-vad is not installed. `pip install silero-vad onnxruntime`, "
                "or use the default 'energy' segmenter."
            ) from exc

        self.threshold = threshold
        self._model = load_silero_vad(onnx=onnx)
        self._onnx = onnx
        self._pending = np.zeros(0, dtype=np.float32)
        self._last = False
        if not onnx:
            import torch  # type: ignore

            self._torch = torch

    def _probability(self, window: np.ndarray) -> float:
        if self._onnx:
            return float(self._model(window[np.newaxis, :], 16_000).item())
        tensor = self._torch.from_numpy(window)
        return float(self._model(tensor, 16_000).item())

    def is_speech(self, frame: AudioFrame) -> bool:
        self._pending = np.concatenate([self._pending, frame.pcm])
        while len(self._pending) >= _WINDOW:
            window, self._pending = self._pending[:_WINDOW], self._pending[_WINDOW:]
            self._last = self._probability(window.astype(np.float32)) >= self.threshold
        return self._last

    def describe(self) -> dict[str, Any]:
        return {
            "segmenter": "SileroSegmenter",
            "threshold": self.threshold,
            "runtime": "onnx" if self._onnx else "torch",
            **self.config.__dict__,
        }


class SileroSegmenter(Module):
    """Pipeline node wrapping the Silero VAD segmenter.

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
        self._inner = _SileroSegmenterImpl(config, **rest)

    def process(self, frame: AudioFrame) -> list[Utterance]:
        return list(self._inner.push(frame))

    def drain(self) -> list[Utterance]:
        return list(self._inner.close())

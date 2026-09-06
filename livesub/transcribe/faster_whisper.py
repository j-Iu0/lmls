"""Whisper via CTranslate2 -- the portable fallback.

Present so the project runs on machines that are not Apple Silicon, and so the claim made
about MLX in the report is checkable rather than asserted: run both on the same fixture
and compare.

CTranslate2 has **no Metal backend**, so on an M-series Mac this executes on the
CPU cores and leaves the GPU idle. It is not a slower way of doing the same
thing -- it is a different device. On a machine with an NVIDIA GPU the position
reverses completely and this becomes the fast option.

``int8`` is the right quantisation for CPU inference: CTranslate2's int8 GEMM kernels are
what make CPU Whisper viable at all, and on speech the accuracy cost against float16 is
small compared to the speed difference.

In the graph this adapter is a pure utterance consumer: a segmenter node upstream cuts
the stream into ``Utterance``s, and ``process`` decodes one of them per call. The
one-shot ``transcribe_array`` path keeps its own segmenter because it runs standalone,
without a graph.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any, ClassVar

import numpy as np

from ..core.interfaces import Module
from ..core.startup import StartupPhase
from ..core.types import SAMPLE_RATE, AudioFrame, Lineage, TextFrame, Utterance
from ..segment import make_segmenter
from .mlx_whisper import looks_hallucinated

log = logging.getLogger("livesub.transcribe.ct2")


class FasterWhisperTranscriber(Module):
    """Args:
        model: model size or a CTranslate2 model directory (``small.en``, ``base.en``,
            ``distil-small.en``, ...).
        compute_type: ``int8`` on CPU, ``float16`` on CUDA.
        device: ``cpu``, ``cuda`` or ``auto``. On Apple Silicon this is always CPU.
        beam_size: 1 is greedy. Beam search costs latency for a small accuracy gain and
            is rarely worth it live.
        segmenter: only used by the offline ``transcribe_array`` path (the graph puts a
            segmenter node upstream).
    """

    inputs: ClassVar[dict[str, type]] = {"audio": Utterance}
    outputs: ClassVar[dict[str, type]] = {"text": TextFrame}

    def __init__(
        self,
        model: str = "small.en",
        compute_type: str = "int8",
        device: str = "auto",
        language: str | None = "en",
        beam_size: int = 1,
        min_utterance_ms: float = 300.0,
        segmenter: str = "energy",
        **options: Any,
    ):
        super().__init__()
        self.model = model
        self.compute_type = compute_type
        self.device = device
        self.language = language
        self.beam_size = beam_size
        self.min_utterance_ms = min_utterance_ms
        self.segmenter_kind = segmenter
        self.segmenter_options = options
        self._model = None

    async def start(self) -> None:
        self._load()

    def _load(self):
        if self._model is not None:
            return self._model
        self._report_startup(
            StartupPhase.IN_PROGRESS, f"loading faster-whisper/{self.model}"
        )
        try:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.model, device=self.device, compute_type=self.compute_type
            )
            log.info("faster-whisper %s (%s/%s) ready", self.model, self.device,
                     self.compute_type)
            self._report_startup(StartupPhase.READY)
        except Exception as exc:
            self._report_startup(StartupPhase.FAILED, str(exc))
            raise
        return self._model

    def _decode(self, pcm: np.ndarray, is_final: bool) -> str:
        if len(pcm) < SAMPLE_RATE * self.min_utterance_ms / 1000:
            return ""
        model = self._load()
        segments, _info = model.transcribe(
            pcm.astype(np.float32),
            language=self.language,
            beam_size=self.beam_size,
            # faster-whisper has its own Silero VAD; the pipeline has already segmented,
            # so running it again would only trim the edges of a deliberate cut.
            vad_filter=False,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text for s in segments).strip()
        return "" if looks_hallucinated(text) else text

    async def process(self, utterance: Utterance) -> list[TextFrame]:
        text = await asyncio.get_running_loop().run_in_executor(
            None, self._decode, utterance.pcm, utterance.is_final
        )
        if not text:
            return []
        lineage = replace(
            Lineage.new(segment_id=utterance.id), t_audio_end=utterance.t_end
        )
        return [
            TextFrame(
                text=text,
                lang=self.language or "en",
                is_final=utterance.is_final,
                lineage=lineage,
                meta={"model": f"faster-whisper/{self.model}",
                      "audio_s": round(utterance.duration, 2)},
            )
        ]

    def transcribe_array(self, pcm: np.ndarray, sample_rate: int) -> list[TextFrame]:
        """One-shot whole-file path used by the CLI and the benchmark.

        Not called by the graph.
        """
        from ..core.audioutil import resample
        from ..core.types import FRAME_SAMPLES

        pcm = resample(pcm, sample_rate, SAMPLE_RATE)
        segmenter = make_segmenter(self.segmenter_kind, **self.segmenter_options)
        frames: list[TextFrame] = []

        def emit(utterance) -> None:
            text = self._decode(utterance.pcm, True)
            if not text:
                return
            lineage = replace(
                Lineage.new(segment_id=utterance.id), t_audio_end=utterance.t_end
            )
            frames.append(
                TextFrame(
                    text=text,
                    lang=self.language or "en",
                    lineage=lineage,
                    meta={"model": f"faster-whisper/{self.model}",
                          "audio_s": round(utterance.duration, 2)},
                )
            )

        for i in range(0, len(pcm), FRAME_SAMPLES):
            block = pcm[i : i + FRAME_SAMPLES]
            if len(block) < FRAME_SAMPLES:
                block = np.pad(block, (0, FRAME_SAMPLES - len(block)))
            frame = AudioFrame(block, SAMPLE_RATE, i // FRAME_SAMPLES, i / SAMPLE_RATE)
            for utterance in segmenter.push(frame):
                if utterance.is_final:
                    emit(utterance)
        for utterance in segmenter.close():
            emit(utterance)
        return frames

    def describe(self) -> dict[str, Any]:
        return {
            "module": "FasterWhisperTranscriber",
            "name": self.name,
            "model": self.model,
            "backend": f"ctranslate2 ({self.device}/{self.compute_type}) -- no Metal",
        }

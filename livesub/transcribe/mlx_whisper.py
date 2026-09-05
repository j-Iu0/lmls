"""Whisper on Apple Silicon via MLX -- the default transcriber on this hardware.

Why MLX and not ``faster-whisper``, which is the usual recommendation: faster-whisper runs
on CTranslate2, and CTranslate2 has no Metal backend. On an M1/M2/M3 it therefore runs on
the CPU cores only, leaving the GPU idle. ``mlx-whisper`` uses Apple's MLX framework, so
the same model runs on the integrated GPU through Metal with unified memory and no
host-device copies. Both are kept -- faster-whisper is the portable fallback for machines
without Apple Silicon, and the benchmark reports both so the claim is measured rather
than repeated.

Model sizing for a live lecture on 16 GB: ``whisper-small.en`` (~0.5 GB) leaves room for
the 4-bit Qwen3.5 used by the correction stage (~3.1 GB) and comfortably beats realtime.
``distil-large-v3`` is more accurate and still usable; ``large-v3`` is not, once the LLM
is resident too.

In the graph this adapter is a pure utterance consumer: a segmenter node upstream cuts
the stream into ``Utterance``s, and ``process`` decodes one of them per call. The
one-shot ``transcribe_array`` path keeps its own segmenter because it runs standalone,
without a graph.

Whisper-specific care taken here:

* ``condition_on_previous_text=False``. Whisper's default is to feed its own prior output
  back as context. On a live stream that turns one bad transcription into a run of bad
  ones, and is the main source of the loop-until-the-clip-ends failure.
* utterances shorter than ~300 ms are not sent at all -- Whisper pads everything to a
  30 s window internally and will happily invent a sentence from a cough.
* a hallucination filter drops the handful of stock phrases Whisper emits for silence
  ("Thank you.", "you", subtitle-site credits), which otherwise appear as subtitles
  during every pause in the lecture.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from typing import Any, ClassVar

import numpy as np

from ..core.interfaces import Module
from ..core.startup import StartupPhase
from ..core.types import SAMPLE_RATE, AudioFrame, Lineage, TextFrame, Utterance
from ..segment import make_segmenter

log = logging.getLogger("livesub.transcribe.mlx")

DEFAULT_MODEL = "mlx-community/whisper-small.en-mlx"

#: Phrases Whisper emits when given silence or noise. Matched case-insensitively on the
#: whole utterance only, so a genuine "thank you" inside a sentence is untouched.
HALLUCINATIONS = {
    "thank you", "thanks for watching", "thank you for watching", "you",
    "bye", "bye.", "please subscribe", "subscribe to my channel",
    "subtitles by the amara.org community", "www.mooji.org", ".", "...",
    "transcription by castingwords",
}


def looks_hallucinated(text: str) -> bool:
    stripped = re.sub(r"[^\w\s.]", "", text).strip().lower().rstrip(".")
    if not stripped:
        return True
    if stripped in {h.rstrip(".") for h in HALLUCINATIONS}:
        return True
    words = stripped.split()
    # A single token repeated over and over is the classic Whisper loop.
    return len(words) >= 6 and len(set(words)) <= 2


class MlxWhisperTranscriber(Module):
    """Args:
        model: an mlx-community Whisper repo id or a local path.
        language: forced decode language. ``.en`` models are English-only anyway; setting
            it explicitly on multilingual models stops Whisper from language-hopping on a
            noisy segment.
        temperature: 0 is greedy. Whisper's fallback ladder retries at higher
            temperatures when a decode looks bad, which is slower and, for live use,
            usually not worth the latency spike.
        min_utterance_ms: below this, do not call the model at all.
        segmenter: only used by the offline ``transcribe_array`` path (the graph puts a
            segmenter node upstream). ``energy`` (default, no extra dependency) or
            ``silero``.
    """

    inputs: ClassVar[dict[str, type]] = {"audio": Utterance}
    outputs: ClassVar[dict[str, type]] = {"text": TextFrame}

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        language: str | None = "en",
        temperature: float = 0.0,
        min_utterance_ms: float = 300.0,
        condition_on_previous_text: bool = False,
        segmenter: str = "energy",
        **options: Any,
    ):
        super().__init__()
        self.model = model
        self.language = language
        self.temperature = temperature
        self.min_utterance_ms = min_utterance_ms
        self.condition_on_previous_text = condition_on_previous_text
        self.segmenter_kind = segmenter
        self.segmenter_options = options
        self._mlx = None

    async def start(self) -> None:
        self._load()

    def _load(self) -> None:
        if self._mlx is not None:
            return
        self._report_startup(
            StartupPhase.IN_PROGRESS,
            f"importing mlx_whisper; {self.model} weights load on first call",
        )
        try:
            import mlx_whisper  # noqa: F401  -- import cost is the model-independent part

            self._mlx = mlx_whisper
            log.info("mlx-whisper ready (model %s loads on first call)", self.model)
            self._report_startup(StartupPhase.READY)
        except Exception as exc:
            self._report_startup(StartupPhase.FAILED, str(exc))
            raise

    def _decode(self, pcm: np.ndarray, is_final: bool) -> str:
        if len(pcm) < SAMPLE_RATE * self.min_utterance_ms / 1000:
            return ""
        self._load()
        assert self._mlx is not None
        result = self._mlx.transcribe(
            pcm.astype(np.float32),
            path_or_hf_repo=self.model,
            language=self.language,
            temperature=self.temperature,
            condition_on_previous_text=self.condition_on_previous_text,
            fp16=True,
            verbose=None,
        )
        text = (result.get("text") or "").strip()
        if looks_hallucinated(text):
            log.debug("dropped likely hallucination: %r", text)
            return ""
        return text

    async def process(self, utterance: Utterance) -> list[TextFrame]:
        # The decode holds the GIL only in bursts but blocks for hundreds of
        # milliseconds; off the event loop is the difference between a stuttering
        # pipeline and a smooth one.
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
                meta={"model": self.model, "audio_s": round(utterance.duration, 2)},
            )
        ]

    def transcribe_array(self, pcm: np.ndarray, sample_rate: int) -> list[TextFrame]:
        """One-shot whole-file path used by the CLI and the benchmark.

        Not called by the graph. Segments first rather than handing Whisper the whole
        file, so the text is broken into the same utterances the live path would
        produce and the two are comparable.
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
                    meta={"model": self.model,
                          "audio_s": round(utterance.duration, 2)},
                )
            )

        for i in range(0, len(pcm), FRAME_SAMPLES):
            block = pcm[i : i + FRAME_SAMPLES]
            if len(block) < FRAME_SAMPLES:
                block = np.pad(block, (0, FRAME_SAMPLES - len(block)))
            frame = AudioFrame(block, SAMPLE_RATE, i // FRAME_SAMPLES,
                               i / SAMPLE_RATE)
            for utterance in segmenter.push(frame):
                if utterance.is_final:
                    emit(utterance)
        for utterance in segmenter.close():
            emit(utterance)
        return frames

    def describe(self) -> dict[str, Any]:
        return {
            "module": "MlxWhisperTranscriber",
            "name": self.name,
            "model": self.model,
            "backend": "mlx (Metal GPU)",
            "segmenter": self.segmenter_kind,
        }

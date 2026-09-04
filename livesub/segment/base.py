"""Utterance segmentation -- shared by the transcription adapters.

Deliberately *not* a sixth pipeline stage. How audio is cut into pieces is an ASR
concern: a streaming transducer wants 200 ms chunks, a Whisper model wants whole
utterances of a few seconds, and a cloud API does its own endpointing server-side. Making
segmentation a graph node would force one policy on all of them.

The policy that matters for the three-second budget:

* **flush on trailing silence** -- the natural end of a clause, and the point at which
  Whisper has enough context for accurate punctuation. The threshold is the sensitive
  number here. Too high and it never fires: measured on the project's own test fixture,
  the longest gap between sentences is 240 ms, so a 350 ms threshold never triggered at
  all and *every* utterance fell through to the length cap, splitting sentences at
  arbitrary points and giving Whisper truncated audio to hallucinate over. 220 ms fires
  reliably between sentences while staying above the 100-150 ms gaps that occur *inside*
  a phrase;
* **force-flush at a maximum length** -- someone who does not pause must still get
  subtitles. But cutting at an arbitrary sample slices a word in half, and half a word is
  what makes Whisper invent the other half. So the cut is placed at the quietest point in
  a search window near the cap, and the audio after the cut is carried into the next
  utterance rather than thrown away;
* **ignore very short blips** -- a cough or a chair scrape otherwise becomes a
  hallucinated sentence, which is worse than no subtitle;
* **keep a little pre-roll** -- speech is detected a few frames *after* it starts, so the
  segmenter emits audio from shortly before the trigger or the first phoneme is clipped.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterator

import numpy as np

from ..core.audioutil import rms
from ..core.types import SAMPLE_RATE, AudioFrame, Utterance


@dataclass
class SegmenterConfig:
    #: Trailing silence that ends an utterance. See the module docstring -- this is the
    #: number that decides whether the segmenter works at all.
    silence_ms: float = 220.0
    min_speech_ms: float = 400.0
    #: Hard cap. Lower is better for latency: a subtitle cannot appear until its
    #: utterance ends, so a 6 s cap bounds how stale the display can get when the speaker
    #: does not pause.
    max_utterance_ms: float = 6000.0
    pre_roll_ms: float = 250.0
    #: How far back from the cap to search for a quiet point to cut at.
    flush_search_ms: float = 700.0
    #: Emit an in-progress partial this often while someone is still talking, so the
    #: first English line appears about a second into a sentence instead of after it.
    partial_every_ms: float = 900.0
    partial_min_ms: float = 700.0


class Segmenter(ABC):
    """Consumes frames, yields utterances (final, and optionally partial)."""

    def __init__(self, config: SegmenterConfig | None = None):
        self.config = config or SegmenterConfig()
        self._pre_roll: deque[np.ndarray] = deque(
            maxlen=max(1, int(self.config.pre_roll_ms / 20))
        )
        self._buffer: list[np.ndarray] = []
        self._levels: list[float] = []  # per-frame RMS, parallel to _buffer
        self._in_speech = False
        self._silence_ms = 0.0
        self._speech_ms = 0.0
        self._since_partial = 0.0
        self._t_start = 0.0
        self._t_last = 0.0
        self._counter = 0
        self._segment_id: str | None = None

    @abstractmethod
    def is_speech(self, frame: AudioFrame) -> bool:
        """Per-frame voice activity decision."""

    def reset(self) -> None:
        self._buffer.clear()
        self._levels.clear()
        self._in_speech = False
        self._silence_ms = 0.0
        self._speech_ms = 0.0
        self._since_partial = 0.0
        self._segment_id = None

    def _new_id(self) -> str:
        self._counter += 1
        return f"u{self._counter:04d}"

    # -- frame intake ---------------------------------------------------------

    def push(self, frame: AudioFrame) -> Iterator[Utterance]:
        """Feed one frame; yields zero or more utterances."""
        cfg = self.config
        frame_ms = frame.duration * 1000
        speech = self.is_speech(frame)

        if not self._in_speech:
            self._pre_roll.append(frame.pcm)
            if speech:
                self._start(frame, frame_ms)
            return

        self._buffer.append(frame.pcm)
        self._levels.append(rms(frame.pcm))
        self._t_last = frame.t_capture + frame.duration
        self._speech_ms += frame_ms
        self._since_partial += frame_ms
        self._silence_ms = 0.0 if speech else self._silence_ms + frame_ms

        if self._silence_ms >= cfg.silence_ms:
            yield from self._flush_final()
            return

        if self._speech_ms >= cfg.max_utterance_ms:
            yield from self._flush_at_quiet_point(frame)
            return

        if (
            cfg.partial_every_ms
            and self._since_partial >= cfg.partial_every_ms
            and self._speech_ms >= cfg.partial_min_ms
        ):
            self._since_partial = 0.0
            yield Utterance(
                id=self._segment_id or self._new_id(),
                pcm=np.concatenate(self._buffer),
                t_start=self._t_start,
                t_end=self._t_last,
                is_final=False,
            )

    def _start(self, frame: AudioFrame, frame_ms: float) -> None:
        self._in_speech = True
        self._segment_id = self._new_id()
        self._buffer = list(self._pre_roll)
        self._levels = [rms(b) for b in self._pre_roll]
        self._t_start = frame.t_capture - len(self._pre_roll) * frame.duration
        self._speech_ms = frame_ms
        self._silence_ms = 0.0
        self._since_partial = 0.0
        self._pre_roll.clear()

    # -- flushing -------------------------------------------------------------

    def _emit(self, pcm: np.ndarray, seg_id: str, t_start: float, t_end: float,
              voiced_ms: float) -> Iterator[Utterance]:
        if voiced_ms >= self.config.min_speech_ms and len(pcm):
            yield Utterance(id=seg_id, pcm=pcm, t_start=t_start, t_end=t_end,
                            is_final=True)

    def _flush_final(self) -> Iterator[Utterance]:
        """End of utterance detected by trailing silence."""
        pcm = np.concatenate(self._buffer) if self._buffer else np.zeros(0, np.float32)
        seg_id = self._segment_id or self._new_id()
        voiced_ms = self._speech_ms - self._silence_ms
        t_start, t_end = self._t_start, self._t_last
        self.reset()
        yield from self._emit(pcm, seg_id, t_start, t_end, voiced_ms)

    def _flush_at_quiet_point(self, frame: AudioFrame) -> Iterator[Utterance]:
        """Hit the length cap without a pause: cut at the quietest nearby frame.

        Cutting at the cap exactly would slice mid-word, and a half word is precisely
        what makes Whisper hallucinate the rest of it. The tail after the cut is carried
        forward as the beginning of the next utterance, so no audio is lost and the next
        subtitle starts from a word boundary.
        """
        search = max(1, int(self.config.flush_search_ms / 20))
        window_start = max(1, len(self._levels) - search)
        candidates = self._levels[window_start:]
        cut = window_start + (int(np.argmin(candidates)) if candidates else 0)
        cut = max(1, min(cut, len(self._buffer) - 1))

        head, tail = self._buffer[:cut], self._buffer[cut:]
        head_levels, tail_levels = self._levels[:cut], self._levels[cut:]
        seg_id = self._segment_id or self._new_id()
        t_start = self._t_start
        cut_time = t_start + cut * frame.duration
        voiced_ms = len(head) * 20.0

        pcm = np.concatenate(head) if head else np.zeros(0, np.float32)

        # Carry the tail into a fresh utterance rather than discarding it.
        self.reset()
        self._in_speech = True
        self._segment_id = self._new_id()
        self._buffer = tail
        self._levels = tail_levels
        self._t_start = cut_time
        self._t_last = self._t_start + len(tail) * frame.duration
        self._speech_ms = len(tail) * 20.0

        yield from self._emit(pcm, seg_id, t_start, cut_time, voiced_ms)

    def close(self) -> Iterator[Utterance]:
        """Flush whatever is buffered when the stream ends."""
        if self._in_speech:
            yield from self._flush_final()

    def describe(self) -> dict[str, Any]:
        return {"segmenter": type(self).__name__, **self.config.__dict__}

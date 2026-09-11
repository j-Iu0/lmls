"""Data contracts shared by every module.

These types are the *only* thing the pipeline modules have in common. A module imports
from ``lmls.core`` and nothing else in the package, which is what lets any
implementation be swapped for another without touching its neighbours.

Canonical audio format everywhere in the system: 16 kHz, mono, float32 in [-1, 1].
Input adapters normalise to it; denoisers are frame-in/frame-out with identical shape.

Payload types on the bus, and how they are routed:

* ``AudioFrame``  -- fixed-size block of audio, published on ``audio.*`` topics.
* ``Utterance``   -- a speech region cut out by a segmenter, published on
  ``utterance.*`` topics.
* ``TextFrame``   -- one line of text at some stage of refinement, published on
  ``text.*`` topics. Its provenance (segment id, revision, audio timing, per-stage
  latencies) lives in the attached :class:`Lineage`.

The bus routes by Python type, never by a ``kind`` field: a topic's payload type is
declared by the module's output port and registered with the bus at wiring time.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

SAMPLE_RATE = 16_000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 320


@dataclass(slots=True)
class AudioFrame:
    """A fixed-size block of mono audio.

    ``t_capture`` is a wall-clock ``time.time()`` stamp taken as close to the moment the
    audio entered the process as the source can manage. Every latency number the system
    reports is measured against it, so sources must not backfill it lazily.
    """

    pcm: np.ndarray  # float32, shape (FRAME_SAMPLES,)
    sample_rate: int = SAMPLE_RATE
    seq: int = 0
    t_capture: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.pcm.dtype != np.float32:
            self.pcm = self.pcm.astype(np.float32)

    @property
    def duration(self) -> float:
        return len(self.pcm) / self.sample_rate


@dataclass(slots=True)
class Utterance:
    """A speech region cut out of the stream by a segmenter, ready to transcribe."""

    id: str
    pcm: np.ndarray  # float32, arbitrary length
    t_start: float  # wall clock of first sample
    t_end: float  # wall clock of last sample
    is_final: bool = True  # False for a partial/in-progress cut

    @property
    def duration(self) -> float:
        return len(self.pcm) / SAMPLE_RATE


@dataclass
class Lineage:
    """Provenance of one speech region, carried unchanged through the text stages.

    ``segment_id`` is stable across every revision of one subtitle line: sinks and
    clients keep a block per id and replace its contents as higher revisions arrive.

    ``revision`` is assigned by the **bus** at publish time, keyed by
    ``(segment_id, topic)``. Modules must never set or increment it -- they copy the
    lineage (or call :meth:`record_latency`, which preserves it) and let the bus stamp
    the next revision when the frame is published.

    ``t_audio_end`` is set once, by the transcriber, when it builds a ``TextFrame`` from
    an ``Utterance``. Every downstream stage copies it unchanged; it anchors the
    end-to-end latency measurement (end of speech -> text on screen).
    """

    segment_id: str
    revision: int = 0  # assigned by the bus; modules do not set this
    t_audio_end: float | None = None  # wall clock of last audio sample
    stage_latency_ms: dict[str, float] = field(default_factory=dict)

    @staticmethod
    def new(segment_id: str | None = None) -> "Lineage":
        """Create a fresh lineage, e.g. when a transcriber creates a TextFrame from an
        Utterance."""
        return Lineage(segment_id=segment_id or uuid.uuid4().hex[:12])

    def record_latency(self, stage: str, seconds: float) -> "Lineage":
        """Return a new Lineage with this stage's latency added. Does not mutate self."""
        updated = dict(self.stage_latency_ms)
        updated[stage] = round(seconds * 1000, 2)
        return replace(self, stage_latency_ms=updated)


@dataclass(slots=True)
class TextFrame:
    """One line of text at some stage of refinement.

    Every text topic in the graph carries this same type. That uniformity is deliberate
    and load-bearing: it is what allows the correction stage to be omitted entirely and
    the translator to subscribe directly to raw ASR output, with no code change anywhere.

    Which stage produced a frame is visible from the *topic* it was published to, not
    from a field -- routing is configuration, not payload content.

    ``segment_id`` + ``revision`` drive incremental display. A sink replaces the line
    whose ``segment_id`` matches when a higher ``revision`` arrives and leaves every
    other line on screen untouched.
    """

    text: str
    lang: str = "en"  # BCP-47, e.g. "en", "vi", "zh"
    is_final: bool = True
    lineage: Lineage = field(default_factory=Lineage.new)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def segment_id(self) -> str:
        """Convenience accessor."""
        return self.lineage.segment_id

    @property
    def revision(self) -> int:
        """Convenience accessor. This value is assigned by the bus, not by the module."""
        return self.lineage.revision

    @property
    def end_to_end_ms(self) -> float:
        """Milliseconds from last audio sample to this frame being emitted.

        This is the number the spec caps at ~3000 ms.
        """
        if not self.lineage.t_audio_end:
            return 0.0
        return round((time.time() - self.lineage.t_audio_end) * 1000, 2)


# Topic-name conventions, and nothing more. A topic (pipe) name is an arbitrary
# identifier: no behaviour anywhere in the system may be inferred from it. What a topic
# carries is declared by the producing module's output port and registered with the bus
# at wiring time; these constants exist purely so humans name topics consistently.
AUDIO_TOPIC_PREFIX = "audio."
TEXT_TOPIC_PREFIX = "text."
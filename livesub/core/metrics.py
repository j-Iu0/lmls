"""Latency bookkeeping.

The spec caps end-to-end delay at roughly three seconds, and asks for the delay to
be *measured and reported*, not asserted. Everything needed for that table is collected
here: per-stage service time, and per-frame time from end-of-speech to display.

Frames are bucketed by ``lang`` rather than by a ``kind`` field: the English line and the
translated line have very different latencies and averaging them together would hide
both. Payload routing happens by topic, so language is what distinguishes the fast raw
line from the slower translated one.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .types import TextFrame


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 1)


@dataclass
class Metrics:
    """Lifetime aggregates with a bounded recent window for percentiles.

    Only record_stage counts processing; frame lineage is provenance, not new work.
    """

    stage_ms: dict[str, deque[float]] = field(default_factory=dict)
    end_to_end_ms: dict[str, deque[float]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    max_samples: int = 2048
    stage_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    stage_totals: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    _end_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _end_max: dict[str, float] = field(default_factory=lambda: defaultdict(float))

    def __post_init__(self) -> None:
        if self.max_samples < 1:
            raise ValueError("max_samples must be positive")

    def record_stage(self, stage: str, ms: float) -> None:
        if stage not in self.stage_ms:
            self.stage_ms[stage] = deque(maxlen=self.max_samples)
        self.stage_ms[stage].append(ms)
        self.stage_counts[stage] += 1
        self.stage_totals[stage] += ms

    def record_event(self, event: TextFrame) -> None:
        """Record an emitted subtitle frame, bucketed by language."""
        key = event.lang
        self.counts[key] += 1
        if event.lineage.t_audio_end and event.is_final:
            ms = event.end_to_end_ms
            if key not in self.end_to_end_ms:
                self.end_to_end_ms[key] = deque(maxlen=self.max_samples)
            self.end_to_end_ms[key].append(ms)
            self._end_counts[key] += 1
            self._end_max[key] = max(self._end_max[key], ms)

    def summary(self) -> dict[str, Any]:
        return {
            "counts": dict(self.counts),
            "stages": {
                name: {
                    "n": self.stage_counts[name],
                    "p50": percentile(v, 0.50),
                    "p95": percentile(v, 0.95),
                    "mean": round(self.stage_totals[name] / self.stage_counts[name], 1)
                    if self.stage_counts[name] else 0.0,
                }
                for name, v in sorted(self.stage_ms.items())
            },
            "end_to_end": {
                lang: {
                    "n": self._end_counts[lang],
                    "p50": percentile(v, 0.50),
                    "p95": percentile(v, 0.95),
                    "max": round(self._end_max[lang], 1),
                }
                for lang, v in sorted(self.end_to_end_ms.items())
            },
        }

    def format_table(self) -> str:
        s = self.summary()
        rows = ["", "per-stage service time (ms)", "  stage                  n    p50    p95   mean"]
        for name, v in s["stages"].items():
            rows.append(
                f"  {name:<20} {v['n']:>4} {v['p50']:>6.1f} {v['p95']:>6.1f} "
                f"{v['mean']:>6.1f}"
            )
        rows += ["", "end-to-end: end of speech -> emitted (ms)",
                 "  lang                   n    p50    p95    max"]
        for lang, v in s["end_to_end"].items():
            rows.append(
                f"  {lang:<20} {v['n']:>4} {v['p50']:>6.1f} {v['p95']:>6.1f} "
                f"{v['max']:>6.1f}"
            )
        # The budget applies to the final display line: the translation when there is
        # one, otherwise the corrected English.
        budget = next(
            (v for lang, v in sorted(s["end_to_end"].items()) if lang != "en"),
            s["end_to_end"].get("en"),
        )
        if budget:
            verdict = "PASS" if budget["p50"] <= 3000 else "OVER BUDGET"
            rows += ["", f"  3000 ms budget: {verdict} (p50 {budget['p50']:.0f} ms)"]
        return "\n".join(rows)

"""In-memory sink. Used by the benchmark and by tests to inspect what was displayed.

Keeps every revision in arrival order, and separately the final state per segment and
language, so a caller can ask both "what did the viewer end up seeing?" and "what did
they see first, and how long before it was corrected?".
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..core.interfaces import Module
from ..core.types import TextFrame


class CollectSink(Module):
    inputs: ClassVar[dict[str, type]] = {"text": TextFrame}
    outputs: ClassVar[dict[str, type]] = {}

    def __init__(self, **_: Any):
        super().__init__()
        self.events: list[TextFrame] = []
        self.latest: dict[tuple[str, str], TextFrame] = {}

    async def process(self, frame: TextFrame) -> None:
        self.events.append(frame)
        key = (frame.segment_id, frame.lang)
        current = self.latest.get(key)
        if current is None or frame.revision >= current.revision:
            self.latest[key] = frame

    def finals(self, lang: str | None = None) -> list[TextFrame]:
        """Final revisions of one language, in the order their segments first appeared."""
        order = {e.segment_id: i for i, e in enumerate(reversed(self.events))}
        chosen = [
            e for e in self.latest.values()
            if e.is_final and (lang is None or e.lang == lang)
        ]
        return sorted(chosen, key=lambda e: -order.get(e.segment_id, 0))

    def text(self, lang: str | None = None) -> str:
        return " ".join(e.text for e in self.finals(lang))

    def describe(self) -> dict[str, Any]:
        return {"stage": "CollectSink", "name": self.name, "events": len(self.events)}

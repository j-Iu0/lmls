"""Append every event to a JSON Lines file (or stdout).

This is the evidence deliverable. Each line carries the segment id, revision, kind, text,
the wall-clock end of the audio it came from, when it was emitted, and the per-stage
service times -- which is everything needed to reconstruct the latency table afterwards,
and to show that the corrected English reached the display before the translation did
rather than merely asserting it.

It records revisions rather than final state on purpose: a log that only kept the last
version of each line could not show that a correction happened at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, ClassVar, TextIO

from ..core.codec import event_to_dict
from ..core.interfaces import Module
from ..core.types import TextFrame


class JsonlSink(Module):
    """Args:
        path: file to append to; omit to write to stdout.
        flush_every: fsync-free flush cadence. 1 (the default) means a crash still leaves
            a complete log, which matters when the thing you are debugging is a hang.
    """

    inputs: ClassVar[dict[str, type]] = {"text": TextFrame}
    outputs: ClassVar[dict[str, type]] = {}

    def __init__(
        self,
        path: str | Path | None = None,
        flush_every: int = 1,
        stream: TextIO | None = None,
        **_: Any,
    ):
        super().__init__()
        self.path = Path(path) if path else None
        self.flush_every = max(1, flush_every)
        self._external = stream
        self._file: TextIO | None = None
        self._count = 0

    async def start(self) -> None:
        if self._external is not None:
            self._file = self._external
        elif self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("a", encoding="utf-8")
        else:
            self._file = sys.stdout

    async def process(self, frame: TextFrame) -> None:
        if self._file is None:
            await self.start()
        assert self._file is not None
        self._file.write(json.dumps(event_to_dict(frame), ensure_ascii=False) + "\n")
        self._count += 1
        if self._count % self.flush_every == 0:
            self._file.flush()

    async def stop(self) -> None:
        if self._file is not None and self.path is not None:
            self._file.close()
        self._file = None

    def describe(self) -> dict[str, Any]:
        return {"stage": "JsonlSink", "name": self.name,
                "path": str(self.path) if self.path else "stdout",
                "events": self._count}

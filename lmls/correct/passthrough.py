"""Correction that changes nothing.

The control condition for measuring what correction is worth, and a legitimate production
choice when the ASR is accurate enough that an LLM pass costs more latency than it buys
accuracy.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, ClassVar

from ..core.interfaces import Module
from ..core.types import TextFrame


class PassthroughCorrector(Module):
    inputs: ClassVar[dict[str, type]] = {"text_in": TextFrame}
    outputs: ClassVar[dict[str, type]] = {"text_out": TextFrame}

    def __init__(self, **_: Any):
        super().__init__()

    async def process(self, frame: TextFrame) -> TextFrame:
        # Same lineage (the bus stamps the revision); only the bookkeeping meta changes.
        return replace(frame, meta={**frame.meta, "corrected": False})

    def describe(self) -> dict[str, Any]:
        return {"module": "PassthroughCorrector", "name": self.name}
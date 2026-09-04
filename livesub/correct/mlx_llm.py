"""LLM correction on a local MLX model.

Runs the model off the event loop and applies two guards that matter more than the prompt
itself:

**Only correct finals.** A partial line is by definition incomplete; asking a model to
"fix" half a sentence makes it invent the ending. Partials pass through untouched -- they
are the fast, provisional tier of the display, and the corrected revision replaces them
shortly after.

**Reject implausible rewrites.** A small quantised model occasionally returns a fluent
sentence that has little to do with the input, or drops half the line. When the result
diverges too far from the original by token overlap, the original is kept. For a live
subtitle, an uncorrected true line beats a confident false one, and the failure is
counted so the report can state how often it happens rather than hiding it.

The discourse context (the last few finalised lines) is internal module state -- a
bounded deque updated as final frames pass through -- never a method parameter.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import replace
from typing import Any, ClassVar

from ..core.interfaces import Module
from ..core.types import TextFrame
from ..llm.mlx_engine import DEFAULT_MODEL, get_engine
from ..llm.prompts import CORRECT_SYSTEM, correct_user

log = logging.getLogger("livesub.correct.mlx")


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of lowercased word sets. Cheap, and adequate for a sanity check."""
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


class MlxLlmCorrector(Module):
    """Args:
        model: mlx-community repo id. Must be a non-thinking instruct model -- a
            reasoning model emits hundreds of tokens before the answer and cannot meet
            the latency budget.
        max_tokens: hard cap on generation. Correction output is about as long as the
            input, so this bounds worst-case latency.
        min_similarity: reject a correction sharing less than this fraction of words with
            the original. 0 disables the guard.
        correct_partials: leave False; see the module docstring.
        context_lines: how many finalised lines of discourse to give the model.
    """

    inputs: ClassVar[dict[str, type]] = {"text_in": TextFrame}
    outputs: ClassVar[dict[str, type]] = {"text_out": TextFrame}

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 160,
        min_similarity: float = 0.4,
        correct_partials: bool = False,
        context_lines: int = 2,
        **_: Any,
    ):
        super().__init__()
        self.model = model
        self.max_tokens = max_tokens
        self.min_similarity = min_similarity
        self.correct_partials = correct_partials
        self._context: deque[str] = deque(maxlen=context_lines)
        self._engine = None
        self.rejected = 0

    async def start(self) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._load)

    def _load(self):
        if self._engine is None:
            self._engine = get_engine(self.model, max_tokens=self.max_tokens)
        return self._engine

    def _correct_sync(self, text: str, context: list[str]) -> tuple[str, bool]:
        engine = self._load()
        data = engine.chat_json(
            CORRECT_SYSTEM,
            correct_user(text, context),
            required=("corrected",),
            max_tokens=self.max_tokens,
        )
        if not data:
            return text, False
        corrected = str(data["corrected"]).strip()
        if not corrected:
            return text, False
        if self.min_similarity and similarity(text, corrected) < self.min_similarity:
            self.rejected += 1
            log.warning(
                "rejected implausible correction (overlap %.2f): %r -> %r",
                similarity(text, corrected), text, corrected,
            )
            return text, False
        return corrected, corrected != text

    async def process(self, frame: TextFrame) -> TextFrame:
        if not frame.is_final and not self.correct_partials:
            return replace(
                frame,
                meta={**frame.meta, "corrected": False, "skipped": "partial"},
            )
        text, changed = await asyncio.get_running_loop().run_in_executor(
            None, self._correct_sync, frame.text, list(self._context)
        )
        if frame.is_final:
            self._context.append(text)
        # Same lineage (the bus stamps the revision); new text, bookkeeping meta.
        return replace(
            frame,
            text=text,
            meta={**frame.meta, "corrected": changed, "original": frame.text},
        )

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "module": "MlxLlmCorrector",
            "name": self.name,
            "model": self.model,
            "rejected": self.rejected,
        }
        if self._engine is not None:
            info.update(self._engine.stats.summary())
        return info
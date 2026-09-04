"""LLM correction against a local Ollama server.

Same guards as the MLX corrector, same prompts, different runtime: the model lives in an
Ollama server process, which makes this the correction backend for machines without
Apple Silicon -- or without the memory to load a second copy of the model, since the
server shares one loaded model with every other Ollama adapter in the graph.

**Only correct finals.** A partial line is by definition incomplete; asking a model to
"fix" half a sentence makes it invent the ending. Partials pass through untouched.

**Reject implausible rewrites.** When the result diverges too far from the original by
token overlap, the original is kept: for a live subtitle, an uncorrected true line beats
a confident false one. The rejection is counted so the report can state how often it
happens rather than hiding it.

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
from ..llm.ollama_engine import DEFAULT_HOST, DEFAULT_MODEL, get_ollama_engine
from ..llm.prompts import CORRECT_SYSTEM, correct_user
from .mlx_llm import similarity

log = logging.getLogger("livesub.correct.ollama")


class OllamaLlmCorrector(Module):
    """Args:
        model: model name as ``ollama list`` shows it. Prefer a non-thinking instruct
            model -- reasoning tokens blow the latency budget (``think=False`` is passed
            regardless, but a small non-thinking model is the better choice anyway).
        host: base URL of the Ollama server.
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
        host: str = DEFAULT_HOST,
        max_tokens: int = 160,
        timeout: float = 8.0,
        think: bool = False,
        min_similarity: float = 0.4,
        correct_partials: bool = False,
        context_lines: int = 2,
        **_: Any,
    ):
        super().__init__()
        self.model = model
        self.host = host
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.think = think
        self.min_similarity = min_similarity
        self.correct_partials = correct_partials
        self._context: deque[str] = deque(maxlen=context_lines)
        self._engine = None
        self.rejected = 0

    async def start(self) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._load)

    def _load(self):
        if self._engine is None:
            engine = get_ollama_engine(
                self.model, host=self.host, max_tokens=self.max_tokens,
                timeout=self.timeout, think=self.think,
            )
            engine.verify()  # once, loudly; later failures degrade quietly
            self._engine = engine
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
        return replace(
            frame,
            text=text,
            meta={**frame.meta, "corrected": changed, "original": frame.text},
        )

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "module": "OllamaLlmCorrector",
            "name": self.name,
            "model": self.model,
            "host": self.host,
            "rejected": self.rejected,
        }
        if self._engine is not None:
            info.update(self._engine.stats.summary())
        return info
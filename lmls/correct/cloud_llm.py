"""LLM correction against a cloud API (Anthropic or OpenAI).

Same prompts and same guards as the local corrector, so a comparison between them measures
the model rather than the prompt. Off by default -- see ``lmls/llm/cloud_engine.py`` for
the privacy, network and cost trade-offs.

One behavioural difference from the local path, and it is deliberate: when the call fails
or times out, the original text is returned unchanged rather than raising. A network blip
during a lecture should degrade the subtitle to "uncorrected", not stop the pipeline.

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
from ..llm.cloud_engine import get_cloud_engine
from ..llm.prompts import CORRECT_SYSTEM, correct_user
from .mlx_llm import similarity

log = logging.getLogger("lmls.correct.cloud")


class CloudLlmCorrector(Module):
    """Args:
        provider: ``anthropic`` or ``openai``.
        model: provider model id; a sensible default per provider if omitted.
        api_key: falls back to ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``.
        timeout: fail fast -- a late subtitle is worthless.
        context_lines: how many finalised lines of discourse to give the model.
    """

    inputs: ClassVar[dict[str, type]] = {"text_in": TextFrame}
    outputs: ClassVar[dict[str, type]] = {"text_out": TextFrame}

    def __init__(
        self,
        provider: str = "anthropic",
        model: str | None = None,
        api_key: str | None = None,
        max_tokens: int = 160,
        timeout: float = 8.0,
        min_similarity: float = 0.4,
        correct_partials: bool = False,
        context_lines: int = 2,
        **_: Any,
    ):
        super().__init__()
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.min_similarity = min_similarity
        self.correct_partials = correct_partials
        self._context: deque[str] = deque(maxlen=context_lines)
        self._engine = None
        self.rejected = 0

    async def start(self) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._load)

    def _load(self):
        if self._engine is None:
            self._engine = get_cloud_engine(
                self.provider, self.model, api_key=self.api_key,
                max_tokens=self.max_tokens, timeout=self.timeout,
            )
        return self._engine

    def _correct_sync(self, text: str, context: list[str]) -> tuple[str, bool]:
        data = self._load().chat_json(
            CORRECT_SYSTEM, correct_user(text, context),
            required=("corrected",), max_tokens=self.max_tokens,
        )
        if not data:
            return text, False  # network failure degrades to uncorrected, never to empty
        corrected = str(data["corrected"]).strip()
        if not corrected:
            return text, False
        if self.min_similarity and similarity(text, corrected) < self.min_similarity:
            self.rejected += 1
            log.warning("rejected implausible correction: %r -> %r", text, corrected)
            return text, False
        return corrected, corrected != text

    async def process(self, frame: TextFrame) -> TextFrame:
        if not frame.is_final and not self.correct_partials:
            return replace(frame, meta={**frame.meta, "corrected": False})
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
        info: dict[str, Any] = {"module": "CloudLlmCorrector", "name": self.name,
                                "provider": self.provider, "rejected": self.rejected}
        if self._engine is not None:
            info.update(self._engine.describe())
        return info
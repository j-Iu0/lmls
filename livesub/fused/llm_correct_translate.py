"""One node, one model call, two outputs: corrected English and its translation.

The spec explicitly permits combining correction and translation into a single
language-model step "if the resulting latency and accuracy are acceptable". This is that
step, and the reason it exists is arithmetic: on a local 4B model each generation costs
roughly 400-900 ms, so a split configuration spends that twice and lands near or past the
three-second budget on its own, before the ASR is even counted.

It is one module with two output ports::

    [[node]]
    name = "fix_translate"
    impl = "fused_llm"
    in = "text.raw"
    out = { corrected = "text.corrected", translated = "text.out" }
    target = "vi"

``process`` returns a dict keyed by output port name; the graph routes each port to the
topic declared for it. Downstream, nothing can tell that one node produced both -- a
display subscribed to ``text.corrected`` and a logger subscribed to ``text.out`` behave
exactly as they would with two separate nodes.

What is genuinely given up, and belongs in the report: correction and translation can no
longer use different models, the corrected English cannot reach the screen *before* the
translation finishes (in the split wiring it can, because they are separate topics with
separate subscribers), and a translation failure takes the correction down with it.

The discourse context is internal module state -- a bounded deque updated as final
frames pass through -- never a method parameter.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import replace
from typing import Any, ClassVar

from ..core.interfaces import Module
from ..core.startup import StartupPhase
from ..core.types import TextFrame
from ..llm.mlx_engine import DEFAULT_MODEL, get_engine
from ..llm.prompts import FUSED_SYSTEM, fused_user, language_name

log = logging.getLogger("livesub.fused")


class FusedLlmStage(Module):
    """Args:
        target: language code for the translation half.
        max_tokens: caps the combined output. Both fields together are roughly twice one
            line, so this is the single biggest lever on this stage's latency.
        min_similarity: reject a "correction" that shares too few words with the input;
            the original English is kept instead. See ``correct.mlx_llm`` for why.
        handle_partials: leave False -- partials pass through untouched.
    """

    inputs: ClassVar[dict[str, type]] = {"text_in": TextFrame}
    outputs: ClassVar[dict[str, type]] = {
        "corrected": TextFrame,
        "translated": TextFrame,
    }

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        target: str = "vi",
        max_tokens: int = 320,
        min_similarity: float = 0.4,
        handle_partials: bool = False,
        context_lines: int = 2,
        **_: Any,
    ):
        super().__init__()
        self.model = model
        self.target = target
        self.max_tokens = max_tokens
        self.min_similarity = min_similarity
        self.handle_partials = handle_partials
        self._context: deque[str] = deque(maxlen=context_lines)
        self._engine = None
        self.rejected = 0

    async def start(self) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._load)

    def _load(self):
        if self._engine is not None:
            return self._engine
        from ..llm.mlx_engine import _CACHE
        cached = self.model in _CACHE
        self._report_startup(
            StartupPhase.IN_PROGRESS,
            f"{'loading from cache' if cached else 'downloading'} {self.model}",
        )
        try:
            self._engine = get_engine(self.model, max_tokens=self.max_tokens)
            self._report_startup(StartupPhase.READY)
        except Exception as exc:
            self._report_startup(StartupPhase.FAILED, str(exc))
            raise
        return self._engine

    def _run_sync(self, text: str, target: str, context: list[str]
                  ) -> tuple[str, str]:
        engine = self._load()
        data = engine.chat_json(
            FUSED_SYSTEM.format(language=language_name(target)),
            fused_user(text, context),
            required=("corrected", "translation"),
            max_tokens=self.max_tokens,
        )
        if not data:
            return text, ""
        corrected = str(data.get("corrected", "")).strip() or text
        translation = str(data.get("translation", "")).strip()

        if self.min_similarity:
            from ..correct.mlx_llm import similarity  # same guard, one definition

            if similarity(text, corrected) < self.min_similarity:
                self.rejected += 1
                log.warning("rejected implausible correction: %r -> %r", text, corrected)
                corrected = text
        return corrected, translation

    async def process(self, frame: TextFrame) -> dict[str, TextFrame] | None:
        if not frame.is_final and not self.handle_partials:
            return None
        corrected, translation = await asyncio.get_running_loop().run_in_executor(
            None, self._run_sync, frame.text, self.target, list(self._context)
        )
        if frame.is_final:
            self._context.append(corrected)

        out: dict[str, TextFrame] = {
            "corrected": replace(
                frame,
                lang="en",
                text=corrected,
                meta={**frame.meta, "corrected": corrected != frame.text,
                      "original": frame.text, "by": "fused"},
            )
        }
        if translation:
            out["translated"] = replace(
                frame,
                lang=self.target,
                text=translation,
                meta={**frame.meta, "source_text": corrected, "by": "fused"},
            )
        return out

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "module": "FusedLlmStage",
            "name": self.name,
            "model": self.model,
            "target": self.target,
            "rejected": self.rejected,
        }
        if self._engine is not None:
            info.update(self._engine.stats.summary())
        return info
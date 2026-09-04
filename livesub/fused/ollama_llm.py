"""Fused correction+translation against a local Ollama server.

The Ollama twin of :class:`~livesub.fused.llm_correct_translate.FusedLlmStage`: one node,
one model call, two output ports -- but the model lives in an Ollama server process, so
the fused wiring no longer requires Apple Silicon. This is the implementation
``config/default.toml`` runs, and the reason ``OllamaLlmTranslator`` has no repair mode:
what that mode did -- fix the ASR errors and translate in one call, emit both frames --
is this stage. Subclassing keeps it that way without duplicating the engine plumbing:
shared engine per (host, model) kept warm by the server, ``verify()`` once at start,
``think=False`` for latency, ``format="json"`` constrained decoding.

The trade-offs are the fused ones (see the MLX stage's docstring for the arithmetic):
correction and translation share one model, and the corrected English reaches the screen
with the translation rather than before it. On top sits the implausibility guard the
correctors use: a "correction" that shares too few words with the input is rejected and
the original English kept -- for a live subtitle, an uncorrected true line beats a
confident false one.

The discourse context is internal module state -- a bounded deque updated as final
frames pass through -- never a method parameter.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any, ClassVar

from ..core.types import TextFrame
from ..correct.mlx_llm import similarity  # same guard, one definition
from ..llm.ollama_engine import DEFAULT_HOST, DEFAULT_MODEL
from ..llm.prompts import FUSED_SYSTEM, fused_user, language_name
from ..translate.ollama_llm import OllamaLlmTranslator

log = logging.getLogger("livesub.fused.ollama")


class OllamaFusedStage(OllamaLlmTranslator):
    """Args:
        model: name as ``ollama list`` shows it. Prefer a non-thinking instruct model --
            reasoning tokens blow the latency budget (``think=False`` is passed anyway).
        host: base URL of the Ollama server. Point it at another machine on the LAN to
            keep the lecture audio on-device while borrowing its GPU.
        target: language code for the translation half.
        max_tokens: caps the combined output -- corrected English plus translation, so
            roughly twice one line and the single biggest lever on this stage's latency.
        min_similarity: reject a "correction" sharing less than this fraction of words
            with the original; the original English is kept instead. 0 disables the guard.
        handle_partials: leave False -- partials pass through untouched.
        context_lines: inherited: how many finalised lines of discourse to give the model.
    """

    outputs: ClassVar[dict[str, type]] = {
        "corrected": TextFrame,
        "translated": TextFrame,
    }

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        target: str = "vi",
        max_tokens: int = 220,
        timeout: float = 8.0,
        think: bool = False,
        min_similarity: float = 0.4,
        handle_partials: bool = False,
        context_lines: int = 2,
        **_: Any,
    ):
        super().__init__(
            model=model, host=host, target=target, max_tokens=max_tokens,
            timeout=timeout, think=think, context_lines=context_lines,
        )
        self.min_similarity = min_similarity
        self.handle_partials = handle_partials
        self.rejected = 0

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
            "module": "OllamaFusedStage",
            "name": self.name,
            "model": self.model,
            "host": self.host,
            "target": self.target,
            "rejected": self.rejected,
        }
        if self._engine is not None:
            info.update(self._engine.stats.summary())
        return info

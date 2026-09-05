"""Translation on a local MLX model, with a faithful mode and a repair mode.

This adapter is where the graph's "correction is optional" property becomes real
behaviour rather than just permissive wiring.

* Wired downstream of a corrector (``repair_mode=False``), it translates faithfully.
  The English has already been repaired; second-guessing it would only add latency.
* Wired straight to raw ASR output (``repair_mode=True``), it repairs the ASR errors
  *while* translating, and returns **two** frames from one call: the repaired English
  (port ``corrected``) and the translation (port ``text_out``). The config routes each
  port to its own topic, so a display subscribed to the corrected topic still shows a
  corrected English line even though no correction stage exists anywhere in the graph.

The second mode is not merely a fallback. Translating from uncorrected text and repairing
separately are different operations: a translator that can see the target language often
resolves an English homophone that a monolingual corrector cannot, because only one
reading survives translation. It also costs one model round trip instead of two. What it
gives up is the ability to run correction and translation on different models, and the
clean provenance of knowing which stage changed what -- which is why both modes ship
and the benchmark compares them.

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
from ..llm.prompts import (
    REPAIR_TRANSLATE_SYSTEM,
    TRANSLATE_SYSTEM,
    fused_user,
    language_name,
    translate_user,
)

log = logging.getLogger("livesub.translate.mlx")


class MlxLlmTranslator(Module):
    """Args:
        target: language code (``vi``, ``zh``, ...). Two translator nodes on the same
            topic give two languages from one correction pass.
        repair_mode: repair ASR errors while translating and also emit the repaired
            English on the ``corrected`` port. Set this when the node is wired straight
            to raw ASR output with no corrector in the graph.
        translate_partials: leave False -- translating a half sentence produces a half
            translation that is replaced a moment later, churn on screen for no
            information gain.
        context_lines: how many finalised lines of discourse to give the model.
    """

    inputs: ClassVar[dict[str, type]] = {"text_in": TextFrame}
    outputs: ClassVar[dict[str, type]] = {
        "text_out": TextFrame,
        "corrected": TextFrame,
    }

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        target: str = "vi",
        max_tokens: int = 220,
        repair_mode: bool = False,
        translate_partials: bool = False,
        context_lines: int = 2,
        **_: Any,
    ):
        super().__init__()
        self.model = model
        self.target = target
        self.max_tokens = max_tokens
        self.repair_mode = repair_mode
        self.translate_partials = translate_partials
        self._context: deque[str] = deque(maxlen=context_lines)
        self._engine = None

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

    def _run_sync(self, text: str, target: str, context: list[str], repair: bool
                  ) -> tuple[str | None, str]:
        """Returns ``(corrected_english_or_None, translation)``."""
        engine = self._load()
        language = language_name(target)
        if repair:
            data = engine.chat_json(
                REPAIR_TRANSLATE_SYSTEM.format(language=language),
                fused_user(text, context),
                required=("corrected", "translation"),
                max_tokens=self.max_tokens,
            )
            if not data:
                return None, ""
            return str(data["corrected"]).strip(), str(data["translation"]).strip()

        data = engine.chat_json(
            TRANSLATE_SYSTEM.format(language=language),
            translate_user(text, context),
            required=("translation",),
            max_tokens=self.max_tokens,
        )
        if not data:
            return None, ""
        return None, str(data["translation"]).strip()

    async def process(
        self, frame: TextFrame
    ) -> TextFrame | dict[str, TextFrame] | None:
        if not frame.is_final and not self.translate_partials:
            return None

        corrected, translation = await asyncio.get_running_loop().run_in_executor(
            None, self._run_sync, frame.text, self.target, list(self._context),
            self.repair_mode,
        )

        if frame.is_final:
            self._context.append(corrected or frame.text)

        if self.repair_mode:
            out: dict[str, TextFrame] = {}
            if corrected:
                out["corrected"] = replace(
                    frame,
                    lang="en",
                    text=corrected,
                    meta={**frame.meta, "corrected": corrected != frame.text,
                          "original": frame.text, "by": "translator-repair"},
                )
            if translation:
                out["text_out"] = replace(
                    frame,
                    lang=self.target,
                    text=translation,
                    meta={**frame.meta, "source_text": corrected or frame.text,
                          "repair_mode": True},
                )
            return out or None

        if not translation:
            log.warning("no translation produced for %r", frame.text[:60])
            return None
        return replace(
            frame,
            lang=self.target,
            text=translation,
            meta={**frame.meta, "source_text": frame.text, "repair_mode": False},
        )

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "module": "MlxLlmTranslator",
            "name": self.name,
            "model": self.model,
            "target": self.target,
            "repair_mode": self.repair_mode,
        }
        if self._engine is not None:
            info.update(self._engine.stats.summary())
        return info
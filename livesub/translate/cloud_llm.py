"""Translation against a cloud API (Anthropic or OpenAI).

Mirrors the local translators exactly, including the faithful/repair split, so the
correction stage is just as optional with a cloud backend as with a local one.

Off by default. A failed call returns no translation rather than raising: the English line
stays on screen, which is a degraded but honest display.

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
from ..llm.cloud_engine import get_cloud_engine
from ..llm.prompts import (
    REPAIR_TRANSLATE_SYSTEM,
    TRANSLATE_SYSTEM,
    fused_user,
    language_name,
    translate_user,
)

log = logging.getLogger("livesub.translate.cloud")


class CloudLlmTranslator(Module):
    inputs: ClassVar[dict[str, type]] = {"text_in": TextFrame}
    default_output = "text_out"
    outputs: ClassVar[dict[str, type]] = {
        "text_out": TextFrame,
        "corrected": TextFrame,
    }

    def __init__(
        self,
        provider: str = "anthropic",
        model: str | None = None,
        api_key: str | None = None,
        target: str = "vi",
        max_tokens: int = 220,
        timeout: float = 8.0,
        repair_mode: bool = False,
        translate_partials: bool = False,
        context_lines: int = 2,
        **_: Any,
    ):
        super().__init__()
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.target = target
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.repair_mode = repair_mode
        self.translate_partials = translate_partials
        self._context: deque[str] = deque(maxlen=context_lines)
        self._engine = None

    async def start(self) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._load)

    def _load(self):
        if self._engine is not None:
            return self._engine
        self._report_startup(
            StartupPhase.IN_PROGRESS, f"initialising {self.provider} client"
        )
        try:
            self._engine = get_cloud_engine(
                self.provider, self.model, api_key=self.api_key,
                max_tokens=self.max_tokens, timeout=self.timeout,
            )
            self._report_startup(StartupPhase.READY)
        except Exception as exc:
            self._report_startup(StartupPhase.FAILED, str(exc))
            raise
        return self._engine

    def _run_sync(self, text: str, target: str, context: list[str], repair: bool
                  ) -> tuple[str | None, str]:
        engine = self._load()
        language = language_name(target)
        if repair:
            data = engine.chat_json(
                REPAIR_TRANSLATE_SYSTEM.format(language=language),
                fused_user(text, context),
                required=("corrected", "translation"), max_tokens=self.max_tokens,
            )
            if not data:
                return None, ""
            return str(data["corrected"]).strip(), str(data["translation"]).strip()

        data = engine.chat_json(
            TRANSLATE_SYSTEM.format(language=language),
            translate_user(text, context),
            required=("translation",), max_tokens=self.max_tokens,
        )
        return (None, str(data["translation"]).strip()) if data else (None, "")

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
            return None
        return replace(
            frame,
            lang=self.target,
            text=translation,
            meta={**frame.meta, "source_text": frame.text, "repair_mode": False},
        )

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {"module": "CloudLlmTranslator", "name": self.name,
                                "provider": self.provider, "target": self.target,
                                "repair_mode": self.repair_mode}
        if self._engine is not None:
            info.update(self._engine.describe())
        return info

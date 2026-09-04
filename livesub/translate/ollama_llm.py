"""Translation against a local Ollama server.

Mirrors the MLX translator's faithful mode: the model runs in an Ollama server process,
so this works on Linux, Windows, NVIDIA, AMD and CPU, and the model stays warm across
pipeline restarts. Latency is dominated by output tokens, hence the terse-JSON prompt and
the ``max_tokens`` cap, with ``think=False`` so Qwen3-style models do not reason their way
past the subtitle budget.

One job per implementation: this stage translates, and does not correct. Repairing ASR
errors and translating in one call is the fused stage's job (``fused_ollama``, which
subclasses this one for the engine plumbing) -- a ``repair_mode`` flag here would mean a
``corrected`` port that sits silent unless the flag is set, which reads as a broken
pipeline more often than as an option.

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
from ..core.types import TextFrame
from ..llm.ollama_engine import DEFAULT_HOST, DEFAULT_MODEL, get_ollama_engine
from ..llm.prompts import TRANSLATE_SYSTEM, language_name, translate_user

log = logging.getLogger("livesub.translate.ollama")


class OllamaLlmTranslator(Module):
    """Args:
        model: name as ``ollama list`` shows it. Must exist on the server (`ollama pull`
            first); verified once at start, not per call.
        host: base URL of the Ollama server. Point it at another machine on the LAN to
            keep the lecture audio on-device while borrowing its GPU.
        target: language code (``vi``, ``zh``, ...). Two translator nodes on the same
            topic give two languages from one correction pass.
        max_tokens: hard cap on generation -- output tokens are the latency.
        timeout: per-call HTTP timeout. A late subtitle is worthless; on timeout the
            caller keeps the previous text.
        think: leave False; a reasoning model cannot meet the latency budget.
        translate_partials: leave False; translating half a sentence produces churn on
            screen for no information gain.
        context_lines: how many finalised lines of discourse to give the model.
    """

    inputs: ClassVar[dict[str, type]] = {"text_in": TextFrame}
    outputs: ClassVar[dict[str, type]] = {"text_out": TextFrame}

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        target: str = "vi",
        max_tokens: int = 220,
        timeout: float = 8.0,
        think: bool = False,
        translate_partials: bool = False,
        context_lines: int = 2,
        **_: Any,
    ):
        super().__init__()
        self.model = model
        self.host = host
        self.target = target
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.think = think
        self.translate_partials = translate_partials
        self._context: deque[str] = deque(maxlen=context_lines)
        self._engine = None

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

    def _translate_sync(self, text: str, target: str, context: list[str]) -> str:
        """One model round trip; empty string when nothing usable comes back."""
        engine = self._load()
        data = engine.chat_json(
            TRANSLATE_SYSTEM.format(language=language_name(target)),
            translate_user(text, context),
            required=("translation",),
            max_tokens=self.max_tokens,
        )
        return str(data["translation"]).strip() if data else ""

    async def process(self, frame: TextFrame) -> TextFrame | None:
        if not frame.is_final and not self.translate_partials:
            return None

        translation = await asyncio.get_running_loop().run_in_executor(
            None, self._translate_sync, frame.text, self.target, list(self._context)
        )

        if frame.is_final:
            self._context.append(frame.text)

        if not translation:
            log.warning("no translation produced for %r", frame.text[:60])
            return None
        return replace(
            frame,
            lang=self.target,
            text=translation,
            meta={**frame.meta, "source_text": frame.text},
        )

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "module": "OllamaLlmTranslator",
            "name": self.name,
            "model": self.model,
            "host": self.host,
            "target": self.target,
        }
        if self._engine is not None:
            info.update(self._engine.stats.summary())
        return info

"""Deterministic stand-in translator -- no model, no network.

Lets the graph, sinks, WebSocket schema and tests be exercised without loading a multi-GB
model. It marks its output unmistakably (a ``[vi]`` prefix and a small real glossary) so
mock output can never be mistaken for a real translation in a screenshot or a demo.

``repair_mode=True`` mirrors the real translators: the node produces both a repaired
English frame (on its ``corrected`` port) and a translation (on ``text_out``) from one
``process`` call, so the no-corrector wiring still shows corrected English.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import replace
from typing import Any, ClassVar

from ..core.interfaces import Module
from ..core.types import TextFrame

#: A few genuine translations, so mock output is recognisable in a demo.
GLOSSARY: dict[str, dict[str, str]] = {
    "vi": {
        "good morning": "chào buổi sáng",
        "everyone": "mọi người",
        "thank you": "cảm ơn",
        "neural network": "mạng nơ-ron",
        "the loss": "hàm mất mát",
        "training": "huấn luyện",
        "lecture": "bài giảng",
    },
    "zh": {
        "good morning": "早上好",
        "everyone": "大家",
        "thank you": "谢谢",
        "neural network": "神经网络",
        "the loss": "损失",
        "training": "训练",
        "lecture": "讲座",
    },
}


class MockTranslator(Module):
    """Args:
        delay_ms: simulated model latency, so latency plumbing is exercised.
        repair_mode: also emit a repaired English frame on the ``corrected`` port,
            mirroring the real translators, so the two-topic routing is covered by tests.
    """

    inputs: ClassVar[dict[str, type]] = {"text_in": TextFrame}
    default_output = "text_out"
    outputs: ClassVar[dict[str, type]] = {
        "text_out": TextFrame,
        "corrected": TextFrame,
    }

    def __init__(
        self,
        target: str = "vi",
        delay_ms: float = 60.0,
        repair_mode: bool = False,
        translate_partials: bool = False,
        context_lines: int = 2,
        **_: Any,
    ):
        super().__init__()
        self.target = target
        self.delay_ms = delay_ms
        self.repair_mode = repair_mode
        self.translate_partials = translate_partials
        self._context: deque[str] = deque(maxlen=context_lines)

    def _render(self, text: str, target: str) -> str:
        glossary = GLOSSARY.get(target, {})
        lowered = text.lower()
        hits = [v for k, v in glossary.items() if k in lowered]
        body = " ".join(hits) if hits else text
        return f"[{target}] {body}"

    async def process(
        self, frame: TextFrame
    ) -> TextFrame | dict[str, TextFrame] | None:
        if not frame.is_final and not self.translate_partials:
            return None
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)

        if self.repair_mode:
            fixed = frame.text[:1].upper() + frame.text[1:] if frame.text else ""
            if fixed and fixed[-1] not in ".!?":
                fixed += "."
            self._context.append(fixed)
            return {
                "corrected": replace(
                    frame,
                    text=fixed,
                    lang="en",
                    meta={**frame.meta, "by": "mock-translator-repair"},
                ),
                "text_out": replace(
                    frame,
                    text=self._render(frame.text, self.target),
                    lang=self.target,
                    meta={**frame.meta, "mock": True},
                ),
            }

        if frame.is_final:
            self._context.append(frame.text)
        return replace(
            frame,
            text=self._render(frame.text, self.target),
            lang=self.target,
            meta={**frame.meta, "mock": True, "repair_mode": False},
        )

    def describe(self) -> dict[str, Any]:
        return {
            "module": "MockTranslator",
            "name": self.name,
            "target": self.target,
            "repair_mode": self.repair_mode,
        }

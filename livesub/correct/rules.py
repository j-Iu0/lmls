"""Glossary and rule-based correction -- no model, sub-millisecond.

Worth having as a real option, not just a test double. Most of what an ASR gets wrong in
a *specific* lecture is the same handful of course-specific terms, over and over:
proper nouns, jargon, the lecturer's name. A glossary fixes those for free, with no
latency and no chance of the model rewriting a sentence that was already right -- which
is the failure mode of LLM correction.

It is also useful *in front of* the LLM corrector: feeding the model text with the
course's own vocabulary already correct measurably reduces how much it changes.

Rules come from a JSON file so a group can tune their own glossary without touching code::

    {
      "gradient dissent": "gradient descent",
      "grade ee ent": "gradient",
      "back propagation": "backpropagation"
    }
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

from ..core.interfaces import Module
from ..core.types import TextFrame

#: Errors Whisper-family models actually make on ML/CS lecture audio.
DEFAULT_RULES: dict[str, str] = {
    "grade ee ent dissent": "gradient descent",
    "gradient dissent": "gradient descent",
    "grade ee ent": "gradient",
    "back propagation": "backpropagation",
    "neural net works": "neural networks",
    "neural net work": "neural network",
    "the sell": "the cell",
    "power house": "powerhouse",
    "eepocks": "epochs",
    "ee pocks": "epochs",
    "data set": "dataset",
    "over fitting": "overfitting",
    "under fitting": "underfitting",
    "hyper parameter": "hyperparameter",
    "activation funktion": "activation function",
    "rectified linear unit": "rectified linear unit",
    "pie torch": "PyTorch",
    "tensor flow": "TensorFlow",
    "num pie": "NumPy",
    "jupiter notebook": "Jupyter notebook",
}


class RuleCorrector(Module):
    """Args:
        glossary: path to a JSON object of ``{"wrong": "right"}`` pairs, merged over the
            built-in defaults.
        only: use *only* the file's rules, ignoring the defaults.
        capitalise: capitalise the first letter and ensure terminal punctuation, which is
            most of what makes a raw ASR line look unfinished on screen.
    """

    inputs: ClassVar[dict[str, type]] = {"text_in": TextFrame}
    outputs: ClassVar[dict[str, type]] = {"text_out": TextFrame}

    def __init__(
        self,
        glossary: str | Path | None = None,
        only: bool = False,
        capitalise: bool = True,
        **_: Any,
    ):
        super().__init__()
        rules: dict[str, str] = {} if only else dict(DEFAULT_RULES)
        if glossary:
            rules.update(json.loads(Path(glossary).read_text()))
        self.rules = rules
        self.capitalise = capitalise
        # Longest first, so "grade ee ent dissent" wins over "grade ee ent".
        ordered = sorted(rules, key=len, reverse=True)
        self._pattern = (
            re.compile(r"\b(" + "|".join(re.escape(k) for k in ordered) + r")\b",
                       re.IGNORECASE)
            if ordered
            else None
        )

    def apply(self, text: str) -> tuple[str, int]:
        changes = 0
        if self._pattern is not None:

            def swap(match: re.Match) -> str:
                nonlocal changes
                changes += 1
                return self.rules[match.group(0).lower()]

            text = self._pattern.sub(swap, text)
        if self.capitalise and text:
            text = text[0].upper() + text[1:]
            if text[-1] not in ".!?,:;":
                text += "."
        return text, changes

    async def process(self, frame: TextFrame) -> TextFrame:
        text, changes = self.apply(frame.text)
        # Same lineage (the bus stamps the revision); new text, bookkeeping meta.
        return replace(
            frame,
            text=text,
            meta={**frame.meta, "corrected": changes > 0, "rule_hits": changes},
        )

    def describe(self) -> dict[str, Any]:
        return {"module": "RuleCorrector", "name": self.name, "rules": len(self.rules)}
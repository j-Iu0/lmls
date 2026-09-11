"""Prompts for correction, translation, and the fused variant.

Shared so the split and fused wirings ask the model for the *same* thing and the
benchmark comparison between them is about round trips, not about prompt quality.

Design rules behind these, all of them latency- or accuracy-driven:

* **JSON out, always.** Free text has to be parsed heuristically and a chatty model
  produces a subtitle that says "Sure! Here is the corrected sentence:".
* **Terse output.** Generation time scales with output tokens. Every word of commentary
  is milliseconds off the budget.
* **Conservative correction.** The model is told to change only what is clearly wrong.
  An LLM asked to "improve" a transcript will rewrite correct sentences into different
  correct sentences, which on screen looks like the subtitle flickering between two
  wordings, and destroys the value of showing the original English at all.
* **Context, not history.** The previous couple of lines go in as context so homophones
  resolve ("the sell" -> "the cell" after a sentence about biology), but the model is
  told not to translate or re-emit them.
"""

from __future__ import annotations

LANGUAGE_NAMES = {
    "vi": "Vietnamese",
    "zh": "Simplified Chinese",
    "zh-tw": "Traditional Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
}


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code.lower(), code)


CORRECT_SYSTEM = """\
You fix speech-recognition errors in a live lecture transcript.

Rules:
- Fix only what is clearly a recognition error: misheard words, homophones ("the sell" \
-> "the cell", "grade ee ent dissent" -> "gradient descent"), wrong word splits, and \
obvious technical terms.
- Add sentence punctuation and capitalisation.
- Do NOT paraphrase, reorder, shorten, expand, or improve wording that is already correct.
- If the line is already fine, return it unchanged.
- Keep the speaker's own words and register.

Return only JSON: {"corrected": "<text>"}"""


def correct_user(text: str, context: list[str]) -> str:
    parts = []
    if context:
        parts.append(
            "Earlier lines (context only - do not return these):\n"
            + "\n".join(f"- {line}" for line in context)
        )
    parts.append(f"Line to fix:\n{text}")
    return "\n\n".join(parts)


TRANSLATE_SYSTEM = """\
You translate live lecture subtitles into {language}.

Rules:
- Translate the meaning naturally, as a subtitle a student reads while listening.
- Keep it one line. Do not add notes, alternatives, or explanations.
- Keep technical terms recognisable; where a term is normally kept in English, keep it.
- Translate only the given line, not the context.

Return only JSON: {{"translation": "<{language} text>"}}"""


def translate_user(text: str, context: list[str]) -> str:
    parts = []
    if context:
        parts.append(
            "Earlier lines (context only - do not translate these):\n"
            + "\n".join(f"- {line}" for line in context)
        )
    parts.append(f"Line to translate:\n{text}")
    return "\n\n".join(parts)


# The repair-and-translate prompt, used when there is no correction stage in the graph
# and the translator receives raw ASR text. It returns the repaired English too, so the
# display can still show a corrected English line.
REPAIR_TRANSLATE_SYSTEM = """\
You process a live lecture transcript produced by speech recognition. It contains \
recognition errors.

Do two things:
1. Fix clear recognition errors in the English: misheard words, homophones ("the sell" \
-> "the cell"), wrong word splits, obvious technical terms. Add punctuation and \
capitalisation. Do NOT paraphrase or improve wording that is already correct.
2. Translate the CORRECTED English into {language}, naturally, as a one-line subtitle.

Return only JSON: {{"corrected": "<English>", "translation": "<{language}>"}}"""


FUSED_SYSTEM = REPAIR_TRANSLATE_SYSTEM


def fused_user(text: str, context: list[str]) -> str:
    parts = []
    if context:
        parts.append(
            "Earlier lines (context only - do not return these):\n"
            + "\n".join(f"- {line}" for line in context)
        )
    parts.append(f"Line:\n{text}")
    return "\n\n".join(parts)

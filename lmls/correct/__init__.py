"""Correction module -- repairs ASR errors in a line of English. OPTIONAL in the graph.

    passthrough  changes nothing; the control condition for measuring correction's value
    rules        glossary/regex; zero latency, fixes course-specific jargon reliably
    mlx_llm      local LLM correction; the default
    cloud_llm    Anthropic/OpenAI correction
    fused_llm    one call that also translates (see lmls.fused)

Delete the node from the wiring and correction moves into translation's call: the
portable default uses the fused stage (``fused_ollama``, which repairs while it
translates), and the translators that keep a ``repair_mode`` (MLX, cloud) do the same on
their backends. ``config/no_correct.toml`` is translation without correction at all.

Run standalone::

    python -m lmls.correct run rules --text "grade ee ent dissent"
    python -m lmls.transcribe run mock --in x.wav | python -m lmls.correct run rules
"""

from .passthrough import PassthroughCorrector
from .rules import RuleCorrector

__all__ = ["PassthroughCorrector", "RuleCorrector"]

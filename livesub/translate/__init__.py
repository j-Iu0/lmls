"""Translation module -- corrected (or raw) English in, target language out.

    mock       deterministic, no model; for CI and schema work
    mlx_llm    local MLX model. Faithful when fed corrected text, repair-and-translate
               (repair_mode) when fed raw ASR text
    ollama     an Ollama server; the portable backend. Faithful only -- the one-call
               repair-and-translate job is livesub.fused's fused_ollama, its subclass
    cloud_llm  Anthropic/OpenAI

Run standalone::

    python -m livesub.translate run mlx_llm --target vi --text "Hello class"
    python -m livesub.translate run mlx_llm --target vi --repair --text "a for loop hear"
"""

from .mock import MockTranslator

__all__ = ["MockTranslator"]

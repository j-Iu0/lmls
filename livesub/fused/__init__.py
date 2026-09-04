"""Fused correction+translation -- one node, one model call, two output ports.

Not a sixth module kind: one stage that publishes both the corrected English frame and
the translated frame from a single language-model call. Downstream nothing can tell that
one node produced both -- a display and a logger subscribed to the two topics behave
exactly as they would with two separate nodes.

Two backends, one behaviour:

    fused_llm     in-process MLX; the Apple Silicon wiring (config/fused.toml)
    fused_ollama  an Ollama server; the portable wiring (config/default.toml)
"""

from .llm_correct_translate import FusedLlmStage
from .ollama_llm import OllamaFusedStage

__all__ = ["FusedLlmStage", "OllamaFusedStage"]

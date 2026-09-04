"""Language-model runners and prompts, shared by correction and translation.

Not one of the five pipeline modules. It exists because ``livesub.correct`` and
``livesub.translate`` both need an LLM but are forbidden from importing each other, and
because a 2.5 GB model must be loaded once and shared rather than once per node.
"""

from .mlx_engine import DEFAULT_MODEL, MlxEngine, extract_json, get_engine
from .prompts import language_name

__all__ = [
    "DEFAULT_MODEL",
    "MlxEngine",
    "extract_json",
    "get_engine",
    "language_name",
]

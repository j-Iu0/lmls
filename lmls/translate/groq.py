"""Low-latency translation through GroqCloud.

The API credential is deliberately not an environment-variable fallback or a public
constructor option.  The registry reads one named entry from ``api_key_file`` and
injects it as ``api_key``; this module retains only the private in-memory value needed
to initialise the Groq client.

Groq uses the same faithful/repair translation behavior and prompts as the other cloud
providers. Failed requests degrade to no translated frame, leaving the English subtitle
on screen.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..llm.cloud_engine import DEFAULT_GROQ_MODEL
from .cloud_llm import CloudLlmTranslator


class GroqLlmTranslator(CloudLlmTranslator):
    """Translate subtitle frames with Groq's hosted chat-completions API.

    Args:
        api_key: injected from ``api_key_file`` by the registry; never configure this
            value directly in TOML.
        model: Groq model id. The default is a fast production model with multilingual
            support and JSON mode.
        target: target language code (``vi``, ``zh``, ...).
        max_tokens: maximum completion length for one subtitle.
        timeout: per-call HTTP timeout in seconds.
        repair_mode: also repair raw ASR text and emit it on ``corrected``.
        translate_partials: translate non-final frames when true.
        context_lines: number of final source lines retained as discourse context.
    """

    secret_file_options: ClassVar = {
        "api_key": ("api_key_file", "api_key_name", "GROQ")
    }

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_GROQ_MODEL,
        target: str = "vi",
        max_tokens: int = 220,
        timeout: float = 8.0,
        repair_mode: bool = False,
        translate_partials: bool = False,
        context_lines: int = 2,
        **_: Any,
    ):
        if not api_key.strip():
            raise ValueError("Groq api_key must not be empty")
        super().__init__(
            provider="groq",
            model=model,
            api_key=api_key,
            target=target,
            max_tokens=max_tokens,
            timeout=timeout,
            repair_mode=repair_mode,
            translate_partials=translate_partials,
            context_lines=context_lines,
        )

    def describe(self) -> dict[str, Any]:
        info = super().describe()
        info["module"] = "GroqLlmTranslator"
        return info

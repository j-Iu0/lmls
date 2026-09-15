"""Cloud LLM runner (Anthropic, OpenAI, or Groq), sharing local prompts.

Off by default. It is included so the report's comparison is real rather than
hypothetical, and because there are situations where it is the better answer: a machine
without a GPU, or a lecture where accuracy matters more than the transcript staying on the
device.

The trade, stated plainly, since the spec asks for it:

* **privacy** -- every corrected sentence of the lecture is sent to a third party. That is
  a decision for the lecturer to make, not a default to inherit.
* **network** -- a round trip on classroom wifi is unpredictable in a way local inference
  is not. A local model at 1000 ms is better than a cloud model that is usually 400 ms and
  occasionally 4000 ms, because the subtitle is worthless once it is late.
* **cost** -- per token, on every sentence of every lecture.
* **accuracy** -- genuinely better, especially on technical vocabulary.

The same prompts are used as for the local engine, so a comparison measures the model, not
the prompt.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from .mlx_engine import GenerationStats, clean_bare_answer, extract_json

log = logging.getLogger("lmls.llm.cloud")

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"


class CloudEngine:
    """Args:
        provider: ``anthropic``, ``openai``, or ``groq``.
        model: provider model id.
        api_key: taken from ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` when omitted
            for those providers. Groq callers must inject it explicitly.
        timeout: fail fast. A late subtitle is not worth waiting for -- on timeout the
            caller keeps the uncorrected text, which is always a valid display.
    """

    def __init__(
        self,
        provider: str = "anthropic",
        model: str | None = None,
        api_key: str | None = None,
        max_tokens: int = 256,
        temperature: float = 0.0,
        timeout: float = 8.0,
    ):
        self.provider = provider.lower()
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.stats = GenerationStats()
        self._lock = threading.Lock()

        if self.provider == "anthropic":
            try:
                import anthropic
            except ImportError as exc:
                raise ImportError(
                    "`.venv/bin/pip install -r requirements-cloud.txt`"
                ) from exc
            key = api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set. Export it, or use a local "
                    "implementation (impl = \"mlx_llm\")."
                )
            self.model = model or DEFAULT_ANTHROPIC_MODEL
            self._client = anthropic.Anthropic(api_key=key, timeout=timeout)
        elif self.provider == "openai":
            try:
                import openai
            except ImportError as exc:
                raise ImportError(
                    "`.venv/bin/pip install -r requirements-cloud.txt`"
                ) from exc
            key = api_key or os.environ.get("OPENAI_API_KEY")
            if not key:
                raise RuntimeError(
                    "OPENAI_API_KEY is not set. Export it, or use a local "
                    "implementation (impl = \"mlx_llm\")."
                )
            self.model = model or DEFAULT_OPENAI_MODEL
            self._client = openai.OpenAI(api_key=key, timeout=timeout)
        elif self.provider == "groq":
            try:
                import groq
            except ImportError as exc:
                raise ImportError(
                    "`.venv/bin/pip install -r requirements-groq.txt`"
                ) from exc
            if not api_key or not api_key.strip():
                raise RuntimeError(
                    "Groq api_key was not supplied. Configure api_key_file and "
                    "api_key_name on the Groq translator."
                )
            self.model = model or DEFAULT_GROQ_MODEL
            # Do not let the SDK inspect the process environment: the registry injects
            # exactly one named value from the configured secret file.
            self._client = groq.Groq(api_key=api_key, timeout=timeout)
        else:
            raise ValueError(
                f"unknown provider {provider!r}; use anthropic, openai, or groq"
            )

    def chat(self, system: str, user: str, max_tokens: int | None = None) -> str:
        t0 = time.perf_counter()
        try:
            if self.provider == "anthropic":
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens or self.max_tokens,
                    temperature=self.temperature,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                text = "".join(
                    block.text for block in response.content
                    if getattr(block, "type", None) == "text"
                )
                used = getattr(response.usage, "output_tokens", 0)
            else:
                request: dict[str, Any] = {
                    "model": self.model,
                    "temperature": self.temperature,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                }
                if self.provider == "groq":
                    # GPT-OSS is a reasoning model, but translation does not benefit
                    # from a long hidden chain of thought. JSON mode also makes the
                    # subtitle response deterministic to parse.
                    request.update(
                        max_completion_tokens=max_tokens or self.max_tokens,
                        reasoning_effort="low",
                        include_reasoning=False,
                        response_format={"type": "json_object"},
                    )
                else:
                    request["max_tokens"] = max_tokens or self.max_tokens
                response = self._client.chat.completions.create(
                    **request,
                )
                text = response.choices[0].message.content or ""
                used = getattr(response.usage, "completion_tokens", 0)
        except Exception:
            self.stats.failures += 1
            log.warning("cloud LLM call failed; keeping the original text",
                        exc_info=True)
            return ""
        self.stats.record((time.perf_counter() - t0) * 1000, used)
        return text.strip()

    def chat_json(
        self,
        system: str,
        user: str,
        required: tuple[str, ...],
        max_tokens: int | None = None,
        single_key_fallback: bool = True,
    ) -> dict[str, Any] | None:
        raw = self.chat(system, user, max_tokens=max_tokens)
        if not raw:
            return None
        data = extract_json(raw) or extract_json(raw, lenient=True)
        if data is not None and all(k in data for k in required):
            return data
        if single_key_fallback and len(required) == 1:
            bare = clean_bare_answer(raw)
            if bare:
                return {required[0]: bare}
        self.stats.failures += 1
        return None

    def describe(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, **self.stats.summary()}


_CACHE: dict[tuple[str, str], CloudEngine] = {}
_CACHE_LOCK = threading.Lock()


def get_cloud_engine(provider: str = "anthropic", model: str | None = None,
                     **kwargs: Any) -> CloudEngine:
    # A Groq translator retains its engine after startup, so a process-wide cache buys
    # nothing there and could accidentally reuse a client created with another config's
    # credential after a hot reload.
    if provider.lower() == "groq":
        return CloudEngine(provider, model, **kwargs)
    with _CACHE_LOCK:
        key = (provider, model or "")
        engine = _CACHE.get(key)
        if engine is None:
            engine = CloudEngine(provider, model, **kwargs)
            _CACHE[key] = engine
        return engine

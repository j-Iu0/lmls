"""Shared Ollama language-model runner.

A third local-LLM path next to the MLX engine: instead of running the model inside this
process, it calls an Ollama server (`ollama serve`) over HTTP, usually on
``localhost:11434``. That buys two things MLX cannot offer: it runs on any OS (no Apple
Silicon) and on any hardware the server supports (NVIDIA, AMD, or plain CPU), and the
model is loaded and kept warm by the *server*, not by this process -- a pipeline restart
does not pay the model-load cost again.

The costs are stated because the report asks for them: an extra local HTTP hop (~1 ms,
irrelevant), and the server, not this code, decides batching and parallelism. Calls are
serialised by a lock anyway -- not for thread safety (the HTTP client is), but so that a
corrector and a translator sharing one model queue behind each other deterministically,
the same behaviour the MLX engine guarantees.

Graceful failure, as everywhere downstream of the ASR: a call that cannot be completed
logs a warning and returns no text, so the display keeps the previous line rather than
raising in the middle of a lecture. The one exception is :meth:`verify`, which runs once
at stage start and fails loudly -- a server that is down or a model that was never pulled
should surface as an instruction *before* the lecture starts, not as an empty subtitle
after it begins.

``think=False`` is the default and matters: Ollama serves Qwen3-style thinking models,
and a model that reasons for hundreds of tokens before answering cannot meet a live
subtitle budget (the same reasoning as in ``mlx_engine``). ``keep_alive`` pins the model
in server memory between subtitle lines instead of unloading it after five idle minutes.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .mlx_engine import GenerationStats, clean_bare_answer, extract_json

log = logging.getLogger("lmls.llm.ollama")

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen3.5:4b"
KEEP_ALIVE = "30m"


class OllamaEngine:
    """An Ollama server connection plus the chat/JSON plumbing the adapters share.

    Args:
        model: model name as ``ollama list`` shows it (e.g. ``qwen3.5:4b``).
        host: base URL of the Ollama server.
        max_tokens: default cap on generated tokens (``num_predict`` in Ollama terms).
        temperature: 0.0 -- greedy; for translation there is one right answer and
            sampling only adds variance to something a viewer reads once.
        timeout: per-call HTTP timeout. A late subtitle is worthless; on timeout the
            caller keeps the previous text.
        think: passed through to models with reasoning modes; False keeps Qwen3-style
            models from spending their token budget on hidden reasoning.
        keep_alive: how long the server keeps the model loaded between calls.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        max_tokens: int = 256,
        temperature: float = 0.0,
        timeout: float = 8.0,
        think: bool = False,
        keep_alive: str = KEEP_ALIVE,
    ):
        try:
            import ollama
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError(
                "the ollama client is not installed. "
                "`.venv/bin/pip install -r requirements-cpu.txt`"
            ) from exc

        self.model = model
        self.host = host
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.think = think
        self.keep_alive = keep_alive
        self.stats = GenerationStats()
        # Extra kwargs of Client() are forwarded to httpx, hence the timeout.
        self._client = ollama.Client(host=host, timeout=timeout)
        self._lock = threading.Lock()

    def verify(self) -> None:
        """Fail fast with an instruction if the server or the model is unavailable.

        Called once when the owning stage starts. Runtime calls do NOT verify -- a
        mid-lecture failure degrades to the previous subtitle instead.
        """
        try:
            self._client.show(self.model)
        except Exception as exc:
            raise RuntimeError(
                f"ollama: cannot use model {self.model!r} on {self.host}: {exc}. "
                f"Is the server running (`ollama serve`), and is the model pulled "
                f"(`ollama pull {self.model}`)?"
            ) from exc

    def chat(
        self,
        system: str,
        user: str,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        """One chat round trip; empty string on any failure, never an exception."""
        t0 = time.perf_counter()
        try:
            with self._lock:
                response = self._client.chat(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    stream=False,
                    think=self.think,
                    format="json" if json_mode else None,
                    options={
                        "temperature": self.temperature,
                        "num_predict": max_tokens or self.max_tokens,
                    },
                    keep_alive=self.keep_alive,
                )
        except Exception:
            self.stats.failures += 1
            log.warning(
                "ollama call failed (%s, model %s); keeping the previous text",
                self.host, self.model, exc_info=True,
            )
            return ""
        text = response.message.content or ""
        used = getattr(response, "eval_count", 0) or 0
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
        """Generate and parse a JSON object, tolerating the usual model sloppiness.

        ``format="json"`` makes the server constrain decoding to valid JSON, which
        removes most of the sloppiness ``mlx_engine`` has to repair; the same forgiving
        parser runs anyway so the two local engines behave identically when a model
        still manages to answer badly.
        """
        raw = self.chat(system, user, max_tokens=max_tokens, json_mode=True)
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
        log.debug("unusable model output: %r", raw[:200])
        return None

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "ollama",
            "host": self.host,
            "model": self.model,
            **self.stats.summary(),
        }


_CACHE: dict[tuple[str, str, bool], OllamaEngine] = {}
_CACHE_LOCK = threading.Lock()


def get_ollama_engine(
    model: str = DEFAULT_MODEL, host: str = DEFAULT_HOST, **kwargs: Any
) -> OllamaEngine:
    """Return a shared engine for ``(host, model)``, connecting at most once per process."""
    think = bool(kwargs.get("think", False))
    with _CACHE_LOCK:
        key = (host, model, think)
        engine = _CACHE.get(key)
        if engine is None:
            engine = OllamaEngine(model, host=host, **kwargs)
            _CACHE[key] = engine
        return engine

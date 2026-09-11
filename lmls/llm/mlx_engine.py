"""Shared MLX language-model runner.

Lives outside the five pipeline modules on purpose. Correction and translation both need
a local LLM, but ``lmls.correct`` may not import ``lmls.translate`` -- so the model
runner sits in a neutral place both can depend on, like ``core``.

Two things here earn their keep:

**One model, loaded once, shared.** The 4-bit 4B model is ~3.1 GB. Wiring a corrector and a
translator as separate nodes must not load it twice on a 16 GB machine, so instances are
cached by model id. This is what makes the split correct-then-translate configuration
merely *slower* than the fused one rather than impossible.

**Generation is serialised.** MLX is not thread-safe for concurrent generation on one
model, and two overlapping requests would in any case contend for the same GPU. A lock
means the split configuration issues two sequential calls, which is exactly the cost the
benchmark is there to expose.

Latency notes for the report: generation time is dominated by *output* tokens, not input.
That is the whole reason the prompts here demand terse JSON and cap ``max_tokens`` -- a
model that decides to explain its reasoning costs seconds, not milliseconds. Qwen3
Qwen 3.5's thinking mode is disabled in its chat template; otherwise it can emit hundreds
of reasoning tokens before the answer and cannot meet the budget at all.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("lmls.llm")

DEFAULT_MODEL = "mlx-community/Qwen3.5-4B-4bit"

_CACHE: dict[str, "MlxEngine"] = {}
_CACHE_LOCK = threading.Lock()


@dataclass
class GenerationStats:
    calls: int = 0
    total_ms: float = 0.0
    total_output_tokens: int = 0
    json_repairs: int = 0
    failures: int = 0

    def record(self, ms: float, tokens: int) -> None:
        self.calls += 1
        self.total_ms += ms
        self.total_output_tokens += tokens

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "mean_ms": round(self.total_ms / self.calls, 1) if self.calls else 0.0,
            "output_tokens": self.total_output_tokens,
            "tokens_per_s": (
                round(self.total_output_tokens / (self.total_ms / 1000), 1)
                if self.total_ms
                else 0.0
            ),
            "json_repairs": self.json_repairs,
            "failures": self.failures,
        }


class MlxEngine:
    """A loaded MLX model plus chat-template prompting and JSON extraction."""

    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = 256,
                 temperature: float = 0.0):
        try:
            from mlx_lm import generate, load
            from mlx_lm.sample_utils import make_sampler
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError(
                "mlx-lm is not installed. `.venv/bin/pip install -r requirements-mlx.txt`"
            ) from exc

        self.model_id = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._generate = generate
        t0 = time.perf_counter()
        self._model, self._tokenizer = load(model)
        log.info("loaded %s in %.1fs", model, time.perf_counter() - t0)
        # Greedy by default: for correction and translation there is one right answer,
        # and sampling only adds variance to something a viewer reads once.
        self._sampler = make_sampler(temp=temperature)
        self._lock = threading.Lock()
        self.stats = GenerationStats()

    def chat(
        self,
        system: str,
        user: str,
        max_tokens: int | None = None,
    ) -> str:
        prompt = self._tokenizer.apply_chat_template(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            add_generation_prompt=True,
            tokenize=False,
            enable_thinking=False,
        )
        t0 = time.perf_counter()
        with self._lock:  # MLX generation is not safe to run concurrently
            text = self._generate(
                self._model,
                self._tokenizer,
                prompt=prompt,
                max_tokens=max_tokens or self.max_tokens,
                sampler=self._sampler,
                verbose=False,
            )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self.stats.record(elapsed_ms, len(self._tokenizer.encode(text)))
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

        Returns ``None`` when nothing usable comes back, which callers treat as "leave
        the text alone" -- for a live subtitle, showing the uncorrected line is always
        better than showing an error or nothing.

        ``single_key_fallback`` handles a behaviour that is not sloppiness but a sensible
        instinct: asked for ``{"translation": "..."}``, an instruction-tuned model very
        often just answers with the translation. Two-field prompts get JSON reliably
        because the structure is load-bearing; one-field prompts frequently do not, and
        no amount of prompt insistence fixes it consistently. Since a bare answer *is*
        the value being asked for, it is accepted as such rather than thrown away and
        re-prompted at the cost of another full generation.
        """
        raw = self.chat(system, user, max_tokens=max_tokens)
        data = extract_json(raw)
        if data is None or not all(k in data for k in required):
            self.stats.json_repairs += 1
            data = extract_json(raw, lenient=True)
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
        return {"model": self.model_id, "max_tokens": self.max_tokens,
                **self.stats.summary()}


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str, lenient: bool = False) -> dict[str, Any] | None:
    """Pull a JSON object out of model output.

    Small quantised models wrap JSON in code fences, prepend "Here is the result:", or
    emit trailing commas. Re-prompting to fix that would cost another full generation and
    blow the latency budget, so the parsing is forgiving instead.
    """
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    blob = candidate[start : end + 1]
    try:
        parsed = json.loads(blob)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        if not lenient:
            return None
    repaired = re.sub(r",\s*([}\]])", r"\1", blob)  # trailing commas
    repaired = repaired.replace("\n", " ")
    try:
        parsed = json.loads(repaired)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


_PREAMBLE = re.compile(
    r"^(sure|okay|certainly|here(?:'s| is)[^:\n]*|translation|corrected)\s*[:,]?\s*",
    re.IGNORECASE,
)


def clean_bare_answer(text: str) -> str:
    """Reduce a free-text model answer to the single line it is actually asserting."""
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    candidate = _PREAMBLE.sub("", candidate).strip()
    # Keep the first non-empty line: anything after it is commentary or alternatives.
    for line in candidate.splitlines():
        line = line.strip().strip('"').strip()
        if line:
            return line
    return ""


def get_engine(model: str = DEFAULT_MODEL, **kwargs: Any) -> MlxEngine:
    """Return a shared engine for ``model``, loading it at most once per process."""
    with _CACHE_LOCK:
        engine = _CACHE.get(model)
        if engine is None:
            engine = MlxEngine(model, **kwargs)
            _CACHE[model] = engine
        return engine

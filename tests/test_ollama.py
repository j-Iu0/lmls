"""Ollama adapter tests.

Everything here runs without a server -- the adapters are exercised through a stub
engine and the engine's failure paths through a closed port. The two live tests at the
bottom are skipped unless an Ollama server answers on localhost:11434, so CI never
needs Ollama installed.
"""

from __future__ import annotations

import socket
import time
from dataclasses import replace

import pytest

from lmls.core.registry import build
from lmls.core.types import Lineage, TextFrame
from lmls.llm.ollama_engine import OllamaEngine

DEAD_HOST = "http://127.0.0.1:1"  # nothing listens on port 1; connection refused at once


class StubEngine:
    """Stands in for OllamaEngine: the adapters only use chat_json, verify, stats."""

    def __init__(self, data: dict | None):
        self.data = data
        from lmls.llm.mlx_engine import GenerationStats

        self.stats = GenerationStats()

    def verify(self) -> None:
        return None

    def chat_json(self, system, user, required, max_tokens=None, **_):
        if self.data is not None and all(k in self.data for k in required):
            return self.data
        return None


def _frame(text: str, segment_id: str = "u1", is_final: bool = True) -> TextFrame:
    return TextFrame(
        text=text,
        is_final=is_final,
        lineage=replace(
            Lineage.new(segment_id=segment_id), t_audio_end=time.time()
        ),
    )


# -- registry ----------------------------------------------------------------


def test_ollama_implementations_build_without_touching_the_server():
    """Construction is offline; the server is only contacted at start()/verify()."""
    translator = build("ollama_translator", name="vi", target="vi")
    corrector = build("ollama_corrector", name="fix")
    fused = build("fused_ollama", name="fix_translate", target="vi")
    assert translator.target == "vi"
    assert fused.target == "vi"
    assert corrector.model == translator.model == fused.model  # one default, one server


# -- translator adapter -------------------------------------------------------


async def test_translator_is_faithful_and_single_port():
    """No repair mode: the translator translates only. The one-call repair+translate
    job is the fused stage's (see the fused tests below)."""
    translator = build("ollama_translator", name="vi", target="vi")
    translator._engine = StubEngine({"translation": "Xin chào các bạn."})
    produced = await translator.process(_frame("hello everyone"))
    assert isinstance(produced, TextFrame)
    assert produced.lang == "vi"
    assert produced.meta["source_text"] == "hello everyone"
    assert translator.outputs == {"text_out": TextFrame}


async def test_partials_are_not_translated():
    translator = build("ollama_translator", name="vi", target="vi")
    translator._engine = StubEngine({"translation": "churn"})
    assert await translator.process(_frame("Hel", is_final=False)) is None


async def test_unusable_model_output_yields_no_translation():
    """A failed call degrades to no translation; the English line stays on screen."""
    translator = build("ollama_translator", name="vi", target="vi")
    translator._engine = StubEngine(None)
    assert await translator.process(_frame("hello everyone")) is None


# -- corrector adapter --------------------------------------------------------


async def test_corrector_fixes_and_reports_the_change():
    corrector = build("ollama_corrector", name="fix")
    corrector._engine = StubEngine(
        {"corrected": "gradient descent uses back propagation on the data set"}
    )
    original = "grade ee ent dissent uses back propagation on the data set"
    out = await corrector.process(_frame(original))
    assert out.meta["corrected"] is True
    assert out.meta["original"] == original
    assert out.lineage is not None


async def test_corrector_maintains_context_internally():
    """The context is module state; each final frame extends it, partials do not."""
    corrector = build("ollama_corrector", name="fix", context_lines=2,
                      min_similarity=0)  # disable the guard so stub output passes
    seen_contexts: list[list[str]] = []

    class RecordingStub(StubEngine):
        def chat_json(self, system, user, required, max_tokens=None, **_):
            seen_contexts.append(list(corrector._context))
            return {"corrected": "fixed"}

    corrector._engine = RecordingStub(None)
    await corrector.process(_frame("line one"))
    await corrector.process(_frame("line two", is_final=False))
    await corrector.process(_frame("line three"))

    assert len(seen_contexts) == 2, "the partial must not trigger a model call"
    assert seen_contexts[0] == []
    assert seen_contexts[1] == ["fixed"], "the partial must not enter the context"


async def test_corrector_rejects_an_implausible_rewrite():
    corrector = build("ollama_corrector", name="fix")
    corrector._engine = StubEngine(
        {"corrected": "The lecture will resume after a short break."}
    )
    original = "grade ee ent dissent uses back propagation"
    out = await corrector.process(_frame(original))
    assert out.text == original, "the original must be kept"
    assert out.meta["corrected"] is False
    assert corrector.rejected == 1


async def test_corrector_passes_partials_through_untouched():
    corrector = build("ollama_corrector", name="fix")
    corrector._engine = StubEngine({"corrected": "invented ending"})
    out = await corrector.process(_frame("half a sent", is_final=False))
    assert out.text == "half a sent"
    assert out.meta["skipped"] == "partial"


# -- fused adapter ---------------------------------------------------------------


async def test_fused_stage_returns_both_ports_from_one_call():
    """One node satisfies both halves: corrected English + translation, same lineage."""
    fused = build("fused_ollama", name="fix_translate", target="vi")
    calls: list[tuple[str, str]] = []

    class CountingStub(StubEngine):
        def chat_json(self, system, user, required, max_tokens=None, **_):
            calls.append((system, user))
            return super().chat_json(system, user, required, max_tokens, **_)

    fused._engine = CountingStub(
        {"corrected": "Gradient descent uses backpropagation.",
         "translation": "Gradient descent sử dụng backpropagation."}
    )
    produced = await fused.process(_frame("grade ee ent dissent"))

    assert isinstance(produced, dict)
    assert set(produced) == {"corrected", "translated"}
    english = produced["corrected"]
    translated = produced["translated"]
    assert english.lang == "en"
    assert translated.lang == "vi"
    assert english.meta["original"] == "grade ee ent dissent"
    assert translated.meta["source_text"] == english.text
    assert translated.lineage.segment_id == english.lineage.segment_id
    assert len(calls) == 1, "the fused stage must make exactly one model call"


async def test_fused_stage_skips_partials():
    fused = build("fused_ollama", name="fix_translate")
    fused._engine = StubEngine({"corrected": "x", "translation": "y"})
    assert await fused.process(_frame("Hel", is_final=False)) is None


async def test_fused_stage_degrades_to_the_original_english():
    """A failed call keeps the raw line as the correction and emits no translation."""
    fused = build("fused_ollama", name="fix_translate", target="vi")
    fused._engine = StubEngine(None)
    produced = await fused.process(_frame("hello everyone"))
    assert isinstance(produced, dict)
    assert set(produced) == {"corrected"}
    assert produced["corrected"].text == "hello everyone"
    assert produced["corrected"].meta["corrected"] is False


async def test_fused_stage_rejects_an_implausible_rewrite():
    fused = build("fused_ollama", name="fix_translate", target="vi")
    fused._engine = StubEngine(
        {"corrected": "The lecture will resume after a short break.",
         "translation": "Buổi học sẽ tạm dừng sau một giờ giải lao ngắn."}
    )
    original = "grade ee ent dissent uses back propagation"
    produced = await fused.process(_frame(original))
    assert produced["corrected"].text == original, "the original must be kept"
    assert fused.rejected == 1
    assert produced["translated"].meta["source_text"] == original


async def test_fused_stage_maintains_context_internally():
    """The context is module state; each final frame extends it, partials do not."""
    fused = build("fused_ollama", name="fix_translate", context_lines=2, min_similarity=0)
    seen_contexts: list[list[str]] = []

    class RecordingStub(StubEngine):
        def chat_json(self, system, user, required, max_tokens=None, **_):
            seen_contexts.append(list(fused._context))
            return {"corrected": "fixed", "translation": "dịch"}

    fused._engine = RecordingStub(None)
    await fused.process(_frame("line one"))
    await fused.process(_frame("line two", is_final=False))
    await fused.process(_frame("line three"))

    assert len(seen_contexts) == 2, "the partial must not trigger a model call"
    assert seen_contexts[0] == []
    assert seen_contexts[1] == ["fixed"], "the partial must not enter the context"


# -- engine, no server required ------------------------------------------------


def make_engine() -> OllamaEngine:
    return OllamaEngine(host=DEAD_HOST, timeout=1.0)


def test_chat_json_parses_clean_and_fenced_output():
    engine = make_engine()
    engine.chat = lambda *a, **k: '{"translation": "Xin chào"}'
    assert engine.chat_json("s", "u", required=("translation",)) == \
        {"translation": "Xin chào"}

    engine.chat = lambda *a, **k: 'Here you go:\n```json\n{"translation": "Xin chào"}\n```'
    assert engine.chat_json("s", "u", required=("translation",)) == \
        {"translation": "Xin chào"}


def test_chat_json_accepts_a_bare_single_key_answer():
    """Mirrors the mlx engine: a bare answer *is* the requested value."""
    engine = make_engine()
    engine.chat = lambda *a, **k: "Xin chào các bạn."
    assert engine.chat_json("s", "u", required=("translation",)) == \
        {"translation": "Xin chào các bạn."}


def test_chat_json_returns_none_for_missing_required_keys():
    engine = make_engine()
    engine.chat = lambda *a, **k: '{"corrected": "no translation key here"}'
    assert engine.chat_json("s", "u", required=("corrected", "translation")) is None


async def test_chat_degrades_gracefully_when_the_server_is_down():
    engine = make_engine()
    assert engine.chat("system", "user") == ""
    assert engine.chat_json("s", "u", required=("translation",)) is None
    assert engine.stats.failures >= 2


async def test_start_fails_loudly_with_an_instruction():
    """Stage start must surface the fix, not an empty subtitle mid-lecture."""
    translator = build("ollama_translator", name="vi", host=DEAD_HOST, timeout=1.0)
    with pytest.raises(RuntimeError) as exc:
        await translator.start()
    message = str(exc.value)
    assert "ollama serve" in message and "ollama pull" in message


async def test_fused_start_fails_loudly_with_an_instruction():
    fused = build("fused_ollama", name="fix_translate", host=DEAD_HOST, timeout=1.0)
    with pytest.raises(RuntimeError) as exc:
        await fused.start()
    message = str(exc.value)
    assert "ollama serve" in message and "ollama pull" in message


# -- live (skipped without a server) -------------------------------------------


def _server_up() -> bool:
    try:
        with socket.create_connection(("localhost", 11434), timeout=0.3):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _server_up(), reason="no Ollama server on localhost:11434")
async def test_live_translation_round_trip():
    translator = build("ollama_translator", name="vi", target="vi", timeout=30)
    await translator.start()
    produced = await translator.process(
        _frame("Gradient descent adjusts every weight to reduce the loss.")
    )
    assert isinstance(produced, TextFrame)
    assert produced.lang == "vi"
    assert produced.text.strip()


@pytest.mark.skipif(not _server_up(), reason="no Ollama server on localhost:11434")
async def test_live_correction_round_trip():
    corrector = build("ollama_corrector", name="fix", timeout=30)
    await corrector.start()
    out = await corrector.process(
        _frame("we use gradiant dissent to train the model")
    )
    assert "gradient descent" in out.text.lower()


@pytest.mark.skipif(not _server_up(), reason="no Ollama server on localhost:11434")
async def test_live_fused_round_trip():
    fused = build("fused_ollama", name="fix_translate", target="vi", timeout=30)
    await fused.start()
    produced = await fused.process(
        _frame("we use gradiant dissent to train the model")
    )
    assert isinstance(produced, dict)
    assert "gradient descent" in produced["corrected"].text.lower()
    assert produced["translated"].lang == "vi"
    assert produced["translated"].text.strip()
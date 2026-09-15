"""Groq translation backend tests.

The SDK is replaced with a tiny in-memory module so this suite never needs network
access or a real credential.
"""

from __future__ import annotations

import inspect
import sys
import types
from types import SimpleNamespace

import pytest

from lmls.cli.__main__ import _resolve
from lmls.core.registry import build, option_parameters, resolve
from lmls.core.types import Lineage, TextFrame
from lmls.llm.cloud_engine import DEFAULT_GROQ_MODEL, _CACHE
from lmls.translate.groq import GroqLlmTranslator


@pytest.fixture(autouse=True)
def clear_cloud_engine_cache():
    _CACHE.clear()
    yield
    _CACHE.clear()


def install_fake_groq(monkeypatch, content: str = '{"translation":"Xin chào lớp học"}'):
    clients = []
    requests = []

    class Completions:
        def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                usage=SimpleNamespace(completion_tokens=7),
            )

    class Groq:
        def __init__(self, **kwargs):
            clients.append(kwargs)
            self.chat = SimpleNamespace(completions=Completions())

    module = types.ModuleType("groq")
    module.Groq = Groq
    monkeypatch.setitem(sys.modules, "groq", module)
    return clients, requests


def test_registry_reads_only_named_groq_secret(tmp_path):
    key_file = tmp_path / "test.env"
    key_file.write_text("UNRELATED=leave-me-alone\nGROQ=test-only-value\n")

    translator = build(
        "groq_translator",
        api_key_file=str(key_file),
        api_key_name="GROQ",
    )

    assert translator._api_key == "test-only-value"
    assert translator.provider == "groq"
    assert translator.model == DEFAULT_GROQ_MODEL


def test_secret_is_not_a_public_or_diagnostic_option():
    translator = GroqLlmTranslator(api_key="test-only-value")
    options = option_parameters(resolve("groq_translator"))

    assert "api_key" not in options
    assert options["api_key_file"].default is inspect.Parameter.empty
    assert options["api_key_name"].default == "GROQ"
    assert "api_key" not in translator.describe()


def test_registry_rejects_missing_or_wrong_named_secret(tmp_path):
    with pytest.raises(FileNotFoundError, match="secret file not found"):
        build("groq_translator", api_key_file=str(tmp_path / "missing.env"))

    key_file = tmp_path / "test.env"
    key_file.write_text("SOMETHING_ELSE=value\n")
    with pytest.raises(ValueError, match="no nonempty 'GROQ' value"):
        build("groq_translator", api_key_file=str(key_file))


def test_pipeline_cli_adds_only_secret_file_references():
    config = _resolve(
        config=None,
        chain="wav,segment,asr,translate",
        overrides={"asr": "mock_transcriber", "translate": "groq_translator"},
        target="vi",
        source=None,
        device=False,
    )

    options = config.node("translate").options
    assert options["api_key_file"] == ".env"
    assert options["api_key_name"] == "GROQ"
    assert "api_key" not in options


@pytest.mark.asyncio
async def test_groq_translates_a_final_frame(monkeypatch):
    clients, requests = install_fake_groq(monkeypatch)
    translator = GroqLlmTranslator(
        api_key="test-only-value",
        target="vi",
        max_tokens=96,
        timeout=3.5,
    )

    await translator.start()
    assert translator._api_key is None
    produced = await translator.process(
            "text",
        TextFrame(text="Hello class", lineage=Lineage.new("segment-1"))
    )

    assert set(produced or {}) == {"text_out"}
    translated = produced["text_out"]
    assert translated.text == "Xin chào lớp học"
    assert translated.lang == "vi"
    assert translated.segment_id == "segment-1"
    assert translated.meta["source_text"] == "Hello class"
    assert translated.meta["repair_mode"] is False

    assert clients == [{"api_key": "test-only-value", "timeout": 3.5}]
    request = requests[0]
    assert request["model"] == DEFAULT_GROQ_MODEL
    assert request["max_completion_tokens"] == 96
    assert request["reasoning_effort"] == "low"
    assert request["include_reasoning"] is False
    assert request["response_format"] == {"type": "json_object"}
    assert [message["role"] for message in request["messages"]] == [
        "system", "user"
    ]


@pytest.mark.asyncio
async def test_groq_skips_partial_frames_without_loading_sdk(monkeypatch):
    clients, requests = install_fake_groq(monkeypatch)
    translator = GroqLlmTranslator(api_key="test-only-value")

    produced = await translator.process(
            "text",
        TextFrame(
            text="Half a sentence",
            is_final=False,
            lineage=Lineage.new("segment-1"),
        )
    )

    assert produced is None
    assert clients == []
    assert requests == []


@pytest.mark.asyncio
async def test_groq_failure_keeps_english_as_the_honest_fallback(monkeypatch):
    class Completions:
        def create(self, **_):
            raise TimeoutError("provider timed out")

    class Groq:
        def __init__(self, **_):
            self.chat = SimpleNamespace(completions=Completions())

    module = types.ModuleType("groq")
    module.Groq = Groq
    monkeypatch.setitem(sys.modules, "groq", module)

    translator = GroqLlmTranslator(api_key="test-only-value")
    produced = await translator.process(
            "text",
        TextFrame(text="Keep me", lineage=Lineage.new("segment-1"))
    )

    assert produced is None
    assert translator._engine.stats.failures == 1

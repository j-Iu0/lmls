"""Deepgram adapter tests use a fake SDK transport; CI never sends lecture audio."""

from __future__ import annotations

import asyncio
import inspect
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import typer

from lmls.cli.__main__ import _resolve
from lmls.cli.bench import _retarget
from lmls.core.config import load_config
from lmls.core.registry import build, option_parameters, resolve
from lmls.core.types import FRAME_SAMPLES, SAMPLE_RATE, AudioFrame
from lmls.transcribe.deepgram import DeepgramTranscriber, pcm16le

class Events:
    OPEN = "open"
    MESSAGE = "message"
    ERROR = "error"
    CLOSE = "close"


class FakeConnection:
    def __init__(self):
        self.callbacks = {}
        self.media: list[bytes] = []
        self.keepalives = 0
        self.finalized = 0
        self.closed = 0
        self._closed = asyncio.Event()

    def on(self, event, callback):
        self.callbacks[event] = callback

    async def start_listening(self):
        self.callbacks[Events.OPEN](None)
        await self._closed.wait()

    async def send_media(self, data: bytes):
        self.media.append(data)
        duration = len(self.media) * FRAME_SAMPLES / SAMPLE_RATE
        if len(self.media) == 1:
            self.callbacks[Events.MESSAGE](_result(
                "hello", start=0.0, duration=duration, confidence=0.7
            ))
        else:
            self.callbacks[Events.MESSAGE](_result(
                "hello world", start=0.0, duration=duration, confidence=0.95,
                is_final=True, speech_final=True,
            ))

    async def send_keep_alive(self):
        self.keepalives += 1

    async def send_finalize(self):
        self.finalized += 1
        self.callbacks[Events.MESSAGE](_result("", from_finalize=True))

    async def send_close_stream(self):
        self.closed += 1
        self._closed.set()


class FakeManager:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_):
        return False


class FakeClient:
    def __init__(self, connection):
        self.connection = connection
        self.options = None
        self.listen = SimpleNamespace(
            v1=SimpleNamespace(connect=self.connect),
        )

    def connect(self, **options):
        self.options = options
        return FakeManager(self.connection)


def _result(
    transcript: str,
    *,
    start: float = 0.0,
    duration: float = 0.0,
    confidence: float = 0.0,
    is_final: bool = False,
    speech_final: bool = False,
    from_finalize: bool = False,
):
    return SimpleNamespace(
        channel=SimpleNamespace(alternatives=[SimpleNamespace(
            transcript=transcript, confidence=confidence,
        )]),
        start=start,
        duration=duration,
        is_final=is_final,
        speech_final=speech_final,
        from_finalize=from_finalize,
        metadata=SimpleNamespace(request_id="request-1"),
    )


async def _frames():
    for seq in range(2):
        yield AudioFrame(
            pcm=np.full(FRAME_SAMPLES, 0.25 * (seq + 1), dtype=np.float32),
            sample_rate=SAMPLE_RATE,
            seq=seq,
            t_capture=100.0 + seq * FRAME_SAMPLES / SAMPLE_RATE,
        )


def test_pcm_is_clipped_and_encoded_as_little_endian_linear16():
    pcm = np.array([-2.0, -1.0, 0.0, 0.5, 1.0, 2.0, np.nan], dtype=np.float32)
    encoded = np.frombuffer(pcm16le(pcm), dtype="<i2")
    assert encoded.tolist() == [-32767, -32767, 0, 16383, 32767, 32767, 0]


@pytest.mark.asyncio
async def test_stream_sends_every_audio_frame_once_and_emits_stable_revisions():
    connection = FakeConnection()
    client = FakeClient(connection)
    transcriber = DeepgramTranscriber(
        api_key="test-only-secret",
        keyterms=["gradient descent"],
        final_timeout_s=0.2,
    )
    transcriber.name = "asr"
    transcriber._client = client
    transcriber._event_type = Events

    output = [frame async for frame in transcriber.process_stream(_frames())]

    assert len(connection.media) == 2
    assert all(len(chunk) == FRAME_SAMPLES * 2 for chunk in connection.media)
    assert [frame.text for frame in output] == ["hello", "hello world"]
    assert [frame.is_final for frame in output] == [False, True]
    assert output[0].segment_id == output[1].segment_id
    assert output[1].lineage.t_audio_end == pytest.approx(100.04)
    assert output[1].meta == {
        "provider": "deepgram",
        "model": "nova-3",
        "confidence": 0.95,
        "request_id": "request-1",
        "provider_start_s": 0.0,
        "provider_end_s": 0.04,
    }
    assert client.options == {
        "model": "nova-3",
        "language": "en",
        "encoding": "linear16",
        "sample_rate": 16000,
        "channels": 1,
        "interim_results": True,
        "endpointing": 120,
        "smart_format": True,
        "mip_opt_out": True,
        "keyterm": ["gradient descent"],
    }
    assert connection.finalized == 1
    assert connection.closed >= 1


def test_stable_chunk_is_split_into_immediately_translatable_sentences():
    transcriber = DeepgramTranscriber(api_key="test-only-secret")
    partial = transcriber._result_frames(_result(
        "First sentence. Second", duration=1.0
    ))
    final = transcriber._result_frames(_result(
        "First sentence. Second sentence.",
        duration=2.0,
        is_final=True,
        speech_final=False,
    ))

    assert len(partial) == 1 and not partial[0].is_final
    assert [frame.text for frame in final] == [
        "First sentence.", "Second sentence."
    ]
    assert all(frame.is_final for frame in final)
    assert final[0].segment_id == partial[0].segment_id
    assert final[1].segment_id != final[0].segment_id


def test_stable_chunk_has_a_word_count_fallback_without_punctuation():
    transcriber = DeepgramTranscriber(
        api_key="test-only-secret",
        split_sentences=False,
        max_final_words=5,
    )
    pieces = transcriber._split_final_text(
        "one two three four five six seven eight nine ten eleven twelve"
    )

    assert pieces == [
        "one two three four five",
        "six seven eight nine ten",
        "eleven twelve",
    ]


def test_registry_reads_named_key_file_and_passes_key_to_module(tmp_path):
    key_file = tmp_path / "test.env"
    key_file.write_text("DEEPGRAM=test-only-secret\n")

    transcriber = build(
        "deepgram",
        api_key_file=str(key_file),
        api_key_name="DEEPGRAM",
    )

    assert transcriber._api_key == "test-only-secret"
    assert transcriber.describe()["backend"] == "deepgram"


def test_registry_rejects_missing_or_malformed_key_files(tmp_path):
    with pytest.raises(FileNotFoundError, match="secret file not found"):
        build("deepgram", api_key_file=str(tmp_path / "missing.env"))

    key_file = tmp_path / "test.env"
    key_file.write_text("SOMETHING_ELSE=value\n")
    with pytest.raises(ValueError, match="no nonempty 'DEEPGRAM' value"):
        build("deepgram", api_key_file=str(key_file))


def test_credentials_are_not_a_public_or_diagnostic_option():
    transcriber = DeepgramTranscriber(api_key="test-only-secret")
    options = option_parameters(resolve("deepgram"))
    assert "api_key" not in options
    assert options["api_key_file"].default is inspect.Parameter.empty
    assert options["api_key_name"].default == "DEEPGRAM"
    assert "api_key" not in transcriber.describe()
    assert "api_key" not in transcriber._connect_options()


def test_more_than_one_hundred_keyterms_is_rejected():
    with pytest.raises(ValueError, match="at most 100"):
        DeepgramTranscriber(
            api_key="test-only-secret",
            keyterms=[f"term-{i}" for i in range(101)],
        )


def test_in_place_whisper_override_explains_the_topology_change():
    with pytest.raises(typer.BadParameter, match="use config/deepgram.toml"):
        _resolve(
            config=Path("config/default.toml"),
            chain=None,
            overrides={"transcribe": "deepgram"},
            target=None,
            source=None,
            device=False,
        )


def test_deepgram_benchmark_rejects_faster_than_realtime_replay():
    with pytest.raises(ValueError, match="must be benchmarked in realtime"):
        _retarget(
            load_config("config/deepgram.toml"),
            Path("assets/lecture.wav"),
            realtime=False,
        )


def test_deepgram_preset_keeps_only_a_secret_file_reference():
    config = load_config("config/deepgram.toml")
    options = config.node("asr").options
    assert options["api_key_file"] == ".env"
    assert options["api_key_name"] == "DEEPGRAM"
    assert "api_key" not in options


def test_file_path_uses_one_prerecorded_request(monkeypatch):
    calls = []
    auth = []

    class Media:
        def transcribe_file(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(results=SimpleNamespace(utterances=[
                SimpleNamespace(
                    transcript="hello world", start=0.1, end=0.8,
                    confidence=0.96, language="en",
                )
            ]))

    class Client:
        def __init__(self, **kwargs):
            auth.append(kwargs)
            self.listen = SimpleNamespace(v1=SimpleNamespace(media=Media()))

    module = types.ModuleType("deepgram")
    module.DeepgramClient = Client
    monkeypatch.setitem(sys.modules, "deepgram", module)
    pcm = np.zeros(SAMPLE_RATE, dtype=np.float32)
    frames = DeepgramTranscriber(api_key="test-only-secret").transcribe_array(
        pcm, SAMPLE_RATE
    )

    assert len(calls) == 1
    assert calls[0]["request"].startswith(b"RIFF")
    assert "encoding" not in calls[0] and "sample_rate" not in calls[0]
    assert calls[0]["utterances"] is True
    assert len(frames) == 1 and frames[0].text == "hello world"
    assert frames[0].meta["provider_start_s"] == 0.1
    assert auth == [{"api_key": "test-only-secret"}]
    assert "test-only-secret" not in repr(calls[0])

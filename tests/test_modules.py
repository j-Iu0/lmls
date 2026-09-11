"""Per-module tests. Each module is exercised on its own, which is the point of the
decomposition: none of these needs another module present.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from lmls.core.audioutil import align, load_wav, mix_at_snr, rms, snr_db
from lmls.core.codec import event_from_dict, event_to_dict
from lmls.core.registry import (
    MissingDependency,
    UnknownImplementation,
    available,
    build,
    resolve,
)
from lmls.core.types import (
    FRAME_SAMPLES,
    SAMPLE_RATE,
    AudioFrame,
    Lineage,
    TextFrame,
    Utterance,
)
from lmls.segment import make_segmenter

pytestmark = pytest.mark.asyncio

LECTURE = "assets/lecture.wav"
NOISE = "assets/classroom_noise.wav"


def frames_from(pcm: np.ndarray):
    for i in range(0, len(pcm) - FRAME_SAMPLES, FRAME_SAMPLES):
        yield AudioFrame(
            pcm[i : i + FRAME_SAMPLES].copy(),
            SAMPLE_RATE,
            i // FRAME_SAMPLES,
            i / SAMPLE_RATE,
        )


def _utterance(pcm_or_frames, id: str = "u1", is_final: bool = True) -> Utterance:
    if isinstance(pcm_or_frames, np.ndarray):
        pcm = pcm_or_frames
    else:
        pcm = np.concatenate([f.pcm for f in pcm_or_frames])
    return Utterance(id=id, pcm=pcm.astype(np.float32), t_start=0.0,
                     t_end=pcm.shape[0] / SAMPLE_RATE, is_final=is_final)


# -- core types --------------------------------------------------------------


def test_lineage_is_immutable_and_latency_recording_returns_a_copy():
    lineage = Lineage.new("u1")
    lineage = Lineage(segment_id="u1", t_audio_end=100.0)
    updated = lineage.record_latency("asr", 0.25)

    assert updated.segment_id == "u1"
    assert updated.t_audio_end == 100.0
    assert updated.stage_latency_ms == {"asr": 250.0}
    assert lineage.stage_latency_ms == {}, "record_latency must not mutate"
    assert updated is not lineage


def test_text_frame_carries_provenance_through_convenience_accessors():
    frame = TextFrame(
        text="hello", lineage=Lineage(segment_id="u1", revision=3, t_audio_end=100.0)
    )
    assert frame.segment_id == "u1"
    assert frame.revision == 3
    assert frame.end_to_end_ms > 0  # measured from t_audio_end to now


def test_frames_survive_a_jsonl_round_trip():
    frame = TextFrame(
        text="xin chào",
        lang="vi",
        lineage=Lineage(segment_id="u9", revision=2, t_audio_end=1234.5,
                        stage_latency_ms={"fused_llm": 1100.0}),
        meta={"source_text": "hello"},
    )
    restored = event_from_dict(json.loads(json.dumps(event_to_dict(frame))))
    assert restored.text == "xin chào"
    assert restored.lang == "vi"
    assert restored.revision == 2
    assert restored.segment_id == "u9"
    assert restored.lineage.stage_latency_ms == {"fused_llm": 1100.0}
    assert restored.meta["source_text"] == "hello"


# -- registry ----------------------------------------------------------------


def test_unknown_implementation_names_the_alternatives():
    with pytest.raises(UnknownImplementation, match="available:"):
        resolve("wishper")


def test_missing_dependency_is_an_instruction_not_a_traceback():
    try:
        build("deepfilternet")
    except MissingDependency as exc:
        assert "pip install" in str(exc)
    except ImportError as exc:  # raised from the adapter's own guard
        assert "deepfilternet" in str(exc).lower()
    else:
        pytest.skip("DeepFilterNet is installed here")


def test_the_mock_path_needs_no_optional_dependencies():
    """config/mock.toml must be constructible on a machine with no models at all."""
    for impl, options in [
        ("wav", {"path": LECTURE}),
        ("passthrough_denoiser", {}),
        ("energy", {}),
        ("mock_transcriber", {}),
        ("rules", {}),
        ("mock_translator", {}),
        ("jsonl", {}),
    ]:
        assert build(impl, name=impl, **options) is not None


def test_every_registered_name_resolves_to_an_importable_path():
    """Catches a typo in the registry table, which would otherwise only show up when
    someone selects that implementation."""
    import importlib

    from lmls.core.registry import REGISTRY

    for impl, path in REGISTRY.items():
        module_path, _, class_name = path.partition(":")
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            continue  # optional dependency absent; that is legitimate
        assert hasattr(module, class_name), f"{impl}: {path}"


def test_port_declarations_are_well_formed():
    """The graph routes by these dicts; a malformed declaration breaks everything."""
    for impl in available():
        try:
            cls = resolve(impl)
        except Exception:  # optional dependency missing at import time
            continue
        assert isinstance(cls.inputs, dict) and isinstance(cls.outputs, dict), impl
        assert all(isinstance(v, type) for v in cls.inputs.values()), impl
        assert all(isinstance(v, type) for v in cls.outputs.values()), impl


# -- input -------------------------------------------------------------------


async def test_wav_replay_produces_canonical_frames():
    source = build("wav", name="wav", path=LECTURE, realtime=False)
    await source.start()
    frames = []
    async for frame in source.run():
        frames.append(frame)
        if len(frames) >= 50:
            break
    assert all(f.sample_rate == SAMPLE_RATE for f in frames)
    assert all(f.pcm.dtype == np.float32 for f in frames)
    assert all(len(f.pcm) == FRAME_SAMPLES for f in frames)
    assert [f.seq for f in frames] == list(range(50))


async def test_ffmpeg_source_normalises_to_the_same_format_as_the_microphone():
    """The media-format requirement: whatever goes in, the frames are identical."""
    source = build("ffmpeg", name="ff", url=LECTURE, realtime=False)
    await source.start()
    try:
        frames = []
        async for frame in source.run():
            frames.append(frame)
            if len(frames) >= 20:
                break
    finally:
        await source.stop()
    assert len(frames) == 20
    assert all(len(f.pcm) == FRAME_SAMPLES and f.sample_rate == SAMPLE_RATE
               for f in frames)
    assert float(np.max(np.abs(np.concatenate([f.pcm for f in frames])))) > 0.0


# -- denoise -----------------------------------------------------------------


@pytest.mark.parametrize(
    "impl", ["passthrough_denoiser", "highpass_gate", "spectral"]
)
def test_denoisers_preserve_frame_shape_and_format(impl):
    """This is what makes the stage removable: nothing downstream can tell it ran."""
    denoiser = build(impl, name=impl)
    frame = AudioFrame(
        np.random.default_rng(0).normal(0, 0.05, FRAME_SAMPLES).astype(np.float32),
        SAMPLE_RATE, 0,
    )
    out = denoiser.process(frame)
    assert out.pcm.shape == frame.pcm.shape
    assert out.pcm.dtype == np.float32
    assert out.sample_rate == frame.sample_rate
    assert out.seq == frame.seq


def test_passthrough_is_bit_exact():
    denoiser = build("passthrough_denoiser", name="p")
    pcm = np.random.default_rng(1).normal(0, 0.1, FRAME_SAMPLES).astype(np.float32)
    assert np.array_equal(denoiser.process(AudioFrame(pcm, SAMPLE_RATE)).pcm, pcm)


def test_spectral_gate_reconstructs_clean_speech_almost_unchanged():
    """A denoiser that mangles clean audio costs word error rate on the good days.

    Checked after removing the declared one-hop delay -- the overlap-add uses sqrt-Hann
    on both analysis and synthesis so that a unit gain reconstructs the input.
    """
    clean = load_wav(LECTURE)[: SAMPLE_RATE * 8]
    denoiser = build("spectral", name="s")
    out = np.concatenate([denoiser.process(f).pcm for f in frames_from(clean)])
    ref, aligned, lag = align(clean, out, max_lag_ms=100)
    assert lag <= 400, "delay exceeded the declared 20 ms hop"
    assert snr_db(ref, aligned - ref) > 3.0


def test_spectral_gate_reduces_added_noise():
    clean = load_wav(LECTURE)[: SAMPLE_RATE * 12]
    noisy = mix_at_snr(clean, load_wav(NOISE)[: SAMPLE_RATE * 12], 5.0)
    denoiser = build("spectral", name="s")
    out = np.concatenate([denoiser.process(f).pcm for f in frames_from(noisy)])

    _, noisy_aligned, _ = align(clean, noisy, max_lag_ms=100)
    ref, cleaned, _ = align(clean, out, max_lag_ms=100)
    before = snr_db(ref[: len(noisy_aligned)], noisy_aligned[: len(ref)] - ref[: len(noisy_aligned)])
    after = snr_db(ref, cleaned - ref)
    assert after > before, f"SNR got worse: {before:.1f} -> {after:.1f} dB"


def test_mix_at_snr_hits_the_requested_ratio():
    """The noisy-condition fixtures are only comparable if the SNR is actually the
    stated one."""
    rng = np.random.default_rng(2)
    speech = rng.normal(0, 0.1, SAMPLE_RATE).astype(np.float32)
    noise = rng.normal(0, 0.3, SAMPLE_RATE).astype(np.float32)
    for target in (0.0, 5.0, 15.0):
        mixed = mix_at_snr(speech, noise, target)
        measured = 20 * np.log10(rms(speech) / max(rms(mixed - speech), 1e-12))
        assert abs(measured - target) < 1.0


# -- segmentation ------------------------------------------------------------


def test_segmenter_splits_the_lecture_into_sentence_utterances():
    """Regression guard for the bug that made every utterance hit the length cap.

    The fixture has ten sentences separated by ~240 ms gaps; with too high a silence
    threshold the segmenter never fires and returns a handful of 8 s blocks cut
    mid-word, which is what makes Whisper hallucinate.
    """
    pcm = load_wav(LECTURE)
    segmenter = make_segmenter("energy")
    finals = [
        u for f in frames_from(pcm) for u in segmenter.push(f) if u.is_final
    ] + [u for u in segmenter.close() if u.is_final]

    assert 8 <= len(finals) <= 13, f"expected ~10 sentences, got {len(finals)}"
    assert all(u.duration < 6.5 for u in finals)
    assert all(u.duration > 0.4 for u in finals)
    assert len({u.id for u in finals}) == len(finals), "segment ids must be unique"


def test_segmenter_emits_partials_before_the_final():
    pcm = load_wav(LECTURE)
    segmenter = make_segmenter("energy")
    seen: list[tuple[str, bool]] = []
    for frame in frames_from(pcm):
        for u in segmenter.push(frame):
            seen.append((u.id, u.is_final))
        if len([s for s in seen if s[1]]) >= 2:
            break
    first_id = seen[0][0]
    partials = [s for s in seen if s[0] == first_id and not s[1]]
    assert partials, "no partial emitted; the fast display tier would never fire"
    assert seen[len(partials)][0] == first_id and seen[len(partials)][1] is True


def test_silence_produces_no_utterances():
    """A quiet room must not generate subtitles."""
    segmenter = make_segmenter("energy")
    silence = np.zeros(SAMPLE_RATE * 5, dtype=np.float32)
    produced = [u for f in frames_from(silence) for u in segmenter.push(f)]
    produced += list(segmenter.close())
    assert produced == []


def test_a_short_blip_is_ignored():
    """A cough should not become a hallucinated sentence."""
    rng = np.random.default_rng(3)
    audio = np.zeros(SAMPLE_RATE * 3, dtype=np.float32)
    audio[SAMPLE_RATE : SAMPLE_RATE + 2000] = rng.normal(0, 0.4, 2000)  # 125 ms
    segmenter = make_segmenter("energy")
    finals = [u for f in frames_from(audio) for u in segmenter.push(f) if u.is_final]
    finals += [u for u in segmenter.close() if u.is_final]
    assert finals == []


# -- segmentation as a pipeline node ------------------------------------------


async def test_energy_segmenter_module_wraps_the_inner_segmenter():
    """The pipeline node: AudioFrame in, zero or more Utterances out, drain() at EOS."""
    from lmls.core.registry import resolve

    node_cls = resolve("energy")
    assert node_cls.inputs == {"audio": AudioFrame}
    assert node_cls.outputs == {"utterance": Utterance}

    pcm = load_wav(LECTURE)[: SAMPLE_RATE * 6]
    node = node_cls()
    node.name = "vad"
    produced: list[Utterance] = []
    for frame in frames_from(pcm):
        produced.extend(node.process(frame))  # sync process; runner executor-wraps it

    drained = node.drain()  # the graph runner calls this at end of stream
    assert isinstance(drained, list)
    all_utterances = produced + drained
    finals = [u for u in all_utterances if u.is_final]
    assert finals, "a segmenter node must yield utterances from speech"
    assert all(u.pcm.dtype == np.float32 for u in all_utterances)


async def test_segmenter_module_drain_matches_inner_close():
    from lmls.core.registry import resolve

    pcm = load_wav(LECTURE)[: SAMPLE_RATE * 4]
    node = resolve("energy")()
    for frame in frames_from(pcm):
        node.process(frame)
    drained = node.drain()
    assert isinstance(drained, list)
    # drain() must fully flush the inner buffer: a second close finds nothing.
    assert list(node._inner.close()) == [], "drain must consume the pending utterance"


def test_silero_defaults_to_the_torch_runtime(monkeypatch):
    """The ML requirements already install torch, so the default must not need ONNX."""
    from lmls.segment.silero import _SileroSegmenterImpl

    requested: list[bool] = []
    fake_model = object()
    monkeypatch.setitem(sys.modules, "silero_vad", SimpleNamespace(
        load_silero_vad=lambda *, onnx: requested.append(onnx) or fake_model,
    ))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())

    segmenter = _SileroSegmenterImpl()

    assert requested == [False]
    assert segmenter._model is fake_model
    assert segmenter.describe()["runtime"] == "torch"


def test_silero_explains_an_explicit_missing_onnx_runtime(monkeypatch):
    from lmls.segment.silero import _SileroSegmenterImpl

    def missing_onnx(*, onnx: bool):
        assert onnx is True
        raise ModuleNotFoundError("No module named 'onnxruntime'", name="onnxruntime")

    monkeypatch.setitem(sys.modules, "silero_vad", SimpleNamespace(load_silero_vad=missing_onnx))
    with pytest.raises(ImportError, match="Set onnx=false to use PyTorch"):
        _SileroSegmenterImpl(onnx=True)


# -- correction --------------------------------------------------------------


async def test_rule_corrector_fixes_glossary_terms():
    corrector = build("rules", name="rules")
    frame = TextFrame(
        text="grade ee ent dissent uses back propagation on the data set",
        lineage=Lineage(segment_id="u1"),
    )
    out = await corrector.process(frame)
    lowered = out.text.lower()  # the corrector also capitalises the first word
    assert "gradient descent" in lowered
    assert "backpropagation" in lowered
    assert "dataset" in lowered
    assert out.meta["corrected"] is True
    assert out.lineage is frame.lineage, "the lineage must be carried unchanged"


async def test_rule_corrector_leaves_correct_text_alone():
    corrector = build("rules", name="rules")
    out = await corrector.process(
        TextFrame(text="This sentence is already fine.", lineage=Lineage.new("u1"))
    )
    assert out.text == "This sentence is already fine."
    assert out.meta["corrected"] is False


async def test_passthrough_corrector_only_relabels():
    corrector = build("passthrough_corrector", name="p")
    frame = TextFrame(text="unchanged", lineage=Lineage.new("u1"))
    out = await corrector.process(frame)
    assert out.text == "unchanged"
    assert out.lineage is frame.lineage


async def test_llm_corrector_keeps_context_internally():
    """The context lives in the module, not in a method parameter. The guard that
    maintains it across calls is exercised on the ollama adapter (see test_ollama)."""
    corrector = build("mlx_llm_corrector", name="c", context_lines=3)
    assert corrector._context.maxlen == 3


def test_similarity_guard_rejects_a_hallucinated_rewrite():
    from lmls.correct.mlx_llm import similarity

    original = "gradient descent then adjusts every weight to make the loss smaller"
    assert similarity(original, "Gradient descent then adjusts every weight to make "
                                "the loss smaller.") > 0.7
    assert similarity(original, "The lecture will resume after a short break.") < 0.4


# -- translation -------------------------------------------------------------


async def test_translator_in_repair_mode_returns_both_ports():
    """The behaviour that makes the correction stage optional."""
    translator = build("mock_translator", name="vi", target="vi", delay_ms=0,
                       repair_mode=True)
    produced = await translator.process(
        TextFrame(text="hello everyone", lineage=Lineage.new("u1"))
    )
    assert isinstance(produced, dict)
    assert set(produced) == {"corrected", "text_out"}
    assert produced["corrected"].lang == "en"
    assert produced["text_out"].lang == "vi"


async def test_translator_in_faithful_mode_returns_a_single_frame():
    translator = build("mock_translator", name="vi", target="vi", delay_ms=0)
    produced = await translator.process(
        TextFrame(text="hello everyone", lineage=Lineage.new("u1"))
    )
    assert isinstance(produced, TextFrame)
    assert produced.lang == "vi"
    assert produced.meta["repair_mode"] is False


async def test_partials_are_not_translated():
    """Translating half a sentence produces churn on screen for no information gain."""
    translator = build("mock_translator", name="vi", target="vi", delay_ms=0)
    produced = await translator.process(
        TextFrame(text="Hello", is_final=False, lineage=Lineage.new("u1"))
    )
    assert produced is None


# -- transcription -----------------------------------------------------------


async def test_mock_transcriber_turns_an_utterance_into_frames():
    """The graph path: one utterance in, zero or more TextFrames out. Segmentation
    happened upstream; the transcriber is a pure decoder."""
    transcriber = build("mock_transcriber", name="mock", delay_ms=0)
    assert type(transcriber).inputs == {"audio": Utterance}
    assert type(transcriber).outputs == {"text": TextFrame}

    segmenter = make_segmenter("energy")
    pcm = load_wav(LECTURE)[: SAMPLE_RATE * 6]
    utterance = next(
        u for f in frames_from(pcm) for u in segmenter.push(f) if u.is_final
    )
    frames = await transcriber.process(utterance)

    assert len(frames) == 1
    frame = frames[0]
    assert frame.is_final and frame.lang == "en"
    assert frame.segment_id == utterance.id, "provenance must carry the segment id"
    assert frame.lineage.t_audio_end == utterance.t_end, (
        "end-to-end latency is anchored at the end of the audio"
    )
    assert frame.revision == 0, "revision is assigned by the bus, never by the module"


async def test_transcribe_array_is_the_offline_one_shot_path():
    """Kept for the CLI and bench; not used by the graph."""
    transcriber = build("mock_transcriber", name="mock", delay_ms=0)
    pcm = load_wav(LECTURE)[: SAMPLE_RATE * 4]
    frames = transcriber.transcribe_array(pcm, SAMPLE_RATE)
    assert len(frames) == 1
    assert isinstance(frames[0], TextFrame)
    assert frames[0].lineage.t_audio_end is not None


# -- sinks -------------------------------------------------------------------


async def test_jsonl_sink_records_every_revision(tmp_path):
    """A log that kept only final state could not show that a correction happened."""
    path = tmp_path / "session.jsonl"
    sink = build("jsonl", name="log", path=path)
    await sink.start()
    base = TextFrame(text="the sell", lineage=Lineage(segment_id="u1",
                                                      t_audio_end=1.0))
    await sink.process(base)
    await sink.process(
        TextFrame(text="the cell", lineage=Lineage(segment_id="u1", revision=1,
                                                   t_audio_end=1.0))
    )
    await sink.process(
        TextFrame(text="tế bào", lang="vi",
                  lineage=Lineage(segment_id="u1", revision=2, t_audio_end=1.0))
    )
    await sink.stop()

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["revision"] for r in rows] == [0, 1, 2]
    assert all(r["segment_id"] == "u1" for r in rows)
    assert rows[-1]["lang"] == "vi"
    assert rows[0]["t_audio_end"] == 1.0


async def test_pretty_sink_replaces_a_line_rather_than_appending():
    """The incremental-display requirement, checked on the sink's own state.

    The English line and the translations are told apart by ``lang``; the display
    contract (replace on segment_id + revision) is unchanged.
    """
    import io

    sink = build("stdout_pretty", name="screen",
                 stream=io.StringIO(), colour=False)
    base = Lineage(segment_id="u1", t_audio_end=1.0)
    await sink.process(TextFrame(text="the sell", lineage=base))
    await sink.process(
        TextFrame(text="the cell", lineage=Lineage(segment_id="u1", revision=1,
                                                   t_audio_end=1.0),
                  meta={"corrected": True})
    )
    await sink.process(
        TextFrame(text="tế bào", lang="vi",
                  lineage=Lineage(segment_id="u1", revision=2, t_audio_end=1.0))
    )

    assert len(sink._blocks) == 1, "a correction created a second block"
    block = sink._blocks[0]
    assert block.english == "the cell"
    assert block.corrected is True
    assert block.translations == {"vi": "tế bào"}
    await sink.stop()


async def test_pretty_sink_ignores_a_stale_revision():
    """With per-subscriber queues two topics can deliver slightly out of order."""
    import io

    sink = build("stdout_pretty", name="screen",
                 stream=io.StringIO(), colour=False)
    await sink.process(
        TextFrame(text="corrected text",
                  lineage=Lineage(segment_id="u1", revision=1))
    )
    await sink.process(
        TextFrame(text="raw text", lineage=Lineage(segment_id="u1", revision=0))
    )  # the older revision arrives late
    assert sink._blocks[0].english == "corrected text"
    await sink.stop()


async def test_collect_sink_keys_latest_by_segment_and_language():
    sink = build("collect", name="c")
    base = Lineage(segment_id="u1", t_audio_end=1.0)
    await sink.process(TextFrame(text="the sell", lineage=base))
    await sink.process(
        TextFrame(text="the cell", lineage=Lineage(segment_id="u1", revision=1,
                                                   t_audio_end=1.0))
    )
    await sink.process(
        TextFrame(text="tế bào", lang="vi",
                  lineage=Lineage(segment_id="u1", revision=2, t_audio_end=1.0))
    )
    assert sink.text("en") == "the cell"
    assert sink.text("vi") == "tế bào"
    assert len(sink.events) == 3, "every revision is kept, in arrival order"

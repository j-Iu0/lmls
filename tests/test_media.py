"""Clock gating, capture mapping, and owned ffmpeg lifecycle."""

import asyncio
import math
import shutil
import time
import wave

import numpy as np
import pytest

from lmls.core.interfaces import Module
from lmls.core.registry import build, resolve
from lmls.core.types import FRAME_SAMPLES, SAMPLE_RATE, AudioFrame
from lmls.input.media import (
    ControlledMediaSource,
    _MAX_TIMESTAMPS,
    _RECENT_FRAMES,
)

FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE


@pytest.fixture
def wav_file(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")

    def write(seconds=1.0):
        # Deterministic, non-periodic PCM makes a fractional seek unambiguous.
        pcm = np.random.default_rng(42).integers(
            -12000, 12000, int(seconds * SAMPLE_RATE), dtype=np.int16,
        )
        path = tmp_path / "synthetic.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)
            out.writeframes(pcm.astype("<i2").tobytes())
        return str(path), pcm.astype(np.float32) / 32768

    return write


async def assert_blocked(task):
    done, _ = await asyncio.wait([task], timeout=0.05)
    assert not done, "a frame escaped the playback gate"


def test_registry_and_constructor():
    source = build("media", name="player", url="movie.mp4")
    assert resolve("media") is ControlledMediaSource
    assert isinstance(source, Module)
    assert source.inputs == {}
    assert source.outputs == {"audio": AudioFrame}
    assert source.position == 0
    assert source.describe() == {
        "stage": "ControlledMediaSource", "name": "player", "url": "movie.mp4",
        "decoded_s": 0, "playback_s": 0, "waiting": True, "level_dbfs": -120,
    }
    with pytest.raises(TypeError):
        ControlledMediaSource()
    with pytest.raises(ValueError, match="url"):
        ControlledMediaSource("")


@pytest.mark.parametrize("value", [-1, math.nan, math.inf, -math.inf])
def test_invalid_clocks(value):
    for option in ("start_seconds", "lookahead_seconds"):
        with pytest.raises(ValueError, match=option):
            ControlledMediaSource("movie.mp4", **{option: value})
    source = ControlledMediaSource("movie.mp4")
    with pytest.raises(ValueError, match="position"):
        source.advance(value)
    assert source.describe()["waiting"]
    assert source.describe()["playback_s"] == 0


def test_backward_jitter_does_not_ratchet_the_clock():
    source = ControlledMediaSource("movie.mp4", start_seconds=10)
    source.advance(11)
    source.advance(10.75)
    assert source.describe()["playback_s"] == 11
    with pytest.raises(ValueError, match="new media source"):
        source.advance(10.74)
    source.advance(11)


@pytest.mark.asyncio
async def test_missing_ffmpeg_is_an_exception(tmp_path):
    source = ControlledMediaSource("movie.mp4", ffmpeg=str(tmp_path / "no-ffmpeg"))
    with pytest.raises(RuntimeError, match="not found on PATH"):
        await anext(source.run())
    await source.stop()


@pytest.mark.asyncio
async def test_initial_gate_advance_and_frame_end_budget(wav_file):
    path, _ = wav_file()
    source = ControlledMediaSource(path, lookahead_seconds=0.05)
    stream = source.run()
    pending = asyncio.create_task(anext(stream))
    try:
        await assert_blocked(pending)
        assert source.position == 0
        source.advance(0)
        first = await asyncio.wait_for(pending, 3)
        second = await asyncio.wait_for(anext(stream), 3)
        assert [first.seq, second.seq] == [0, 1]
        assert source.position == pytest.approx(0.04)
        assert source.describe()["waiting"]
        pending = asyncio.create_task(anext(stream))
        await assert_blocked(pending)
        source.advance(0.01)
        third = await asyncio.wait_for(pending, 3)
        assert source.position == pytest.approx(0.06)
        assert first.t_capture < second.t_capture < third.t_capture
        assert -20 < source.describe()["level_dbfs"] < -5
    finally:
        await source.stop()
        await stream.aclose()


@pytest.mark.asyncio
async def test_zero_lookahead_and_advance_before_start(wav_file):
    path, _ = wav_file()
    source = ControlledMediaSource(path, lookahead_seconds=0)
    source.advance(0.019)
    stream = source.run()
    pending = asyncio.create_task(anext(stream))
    try:
        await assert_blocked(pending)
        source.advance(0.02)
        await asyncio.wait_for(pending, 3)
        assert source.position == 0.02
    finally:
        await source.stop()
        await stream.aclose()


@pytest.mark.asyncio
async def test_accurate_offset_capture_mapping_and_eof(wav_file):
    path, pcm = wav_file()
    offset = 0.135
    source = ControlledMediaSource(path, start_seconds=offset, lookahead_seconds=0)
    source.advance(2)
    await source.start()
    proc = source._decoder._proc
    stderr_task = source._decoder._stderr_task
    exit_task = source._decoder._exit_task
    command = source._decoder._build_command()
    assert "-re" not in command
    assert command.index("-ss") < command.index("-i")
    assert command[command.index("-ss") + 1] == str(offset)
    assert "-accurate_seek" in command
    before = time.time()

    async def collect():
        return [frame async for frame in source.run()]

    frames = await asyncio.wait_for(collect(), 5)
    after = time.time()
    decoded = np.concatenate([frame.pcm for frame in frames])
    expected = pcm[int(offset * SAMPLE_RATE):]
    np.testing.assert_array_equal(decoded[:len(expected)], expected)
    np.testing.assert_array_equal(decoded[len(expected):], 0)
    assert len(frames) == math.ceil(len(expected) / FRAME_SAMPLES)
    assert all(frame.sample_rate == SAMPLE_RATE for frame in frames)
    assert [frame.seq for frame in frames] == list(range(len(frames)))
    assert before <= frames[0].t_capture < frames[-1].t_capture <= after
    assert all(a.t_capture < b.t_capture for a, b in zip(frames, frames[1:]))
    for i, frame in enumerate(frames):
        assert source.media_time(frame.t_capture) == pytest.approx(offset + i * 0.02)
        end = frame.t_capture + frame.duration
        assert source.media_time(end - frame.duration) + frame.duration == pytest.approx(
            offset + (i + 1) * 0.02,
        )
    assert source.position == pytest.approx(offset + len(frames) * 0.02)
    assert proc.returncode == 0
    assert stderr_task.done() and exit_task.done()
    assert source._decoder._proc is None
    assert not source.describe()["waiting"]
    # The source is single-use, including after normal EOF.
    assert [frame async for frame in source.run()] == []
    await source.stop()


@pytest.mark.asyncio
async def test_eof_at_exact_budget_does_not_need_another_advance(wav_file):
    path, _ = wav_file(0.1)
    source = ControlledMediaSource(path, lookahead_seconds=0)
    source.advance(0.1)

    async def collect():
        return [frame async for frame in source.run()]

    frames = await asyncio.wait_for(collect(), 5)
    assert len(frames) == 5


@pytest.mark.asyncio
async def test_decode_failure_wakes_initial_gate(wav_file, tmp_path):
    wav_file()  # apply the same optional-ffmpeg skip
    source = ControlledMediaSource(str(tmp_path / "missing.wav"))
    with pytest.raises(RuntimeError, match="ffmpeg exited"):
        await asyncio.wait_for(anext(source.run()), 5)
    assert source._decoder._proc is None
    assert source._decoder._stderr_task.done()
    assert source._decoder._exit_task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_stop_or_cancel_while_gated_reaps_full_pipe(wav_file, cancel):
    path, _ = wav_file(30)
    source = ControlledMediaSource(path)
    await source.start()
    proc = source._decoder._proc
    stderr_task = source._decoder._stderr_task
    exit_task = source._decoder._exit_task
    stream = source.run()
    pending = asyncio.create_task(anext(stream))
    await assert_blocked(pending)
    if cancel:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 5)
    else:
        await asyncio.wait_for(source.stop(), 5)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(pending, 5)
    await source.stop()
    await stream.aclose()
    assert proc.returncode is not None
    assert source._decoder._proc is None
    assert stderr_task.done() and exit_task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_stop_cancels_inflight_read(wav_file, monkeypatch, cancel):
    path, _ = wav_file()
    source = ControlledMediaSource(path)
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def blocked_decoder():
        try:
            entered.set()
            await asyncio.Event().wait()
            yield  # pragma: no cover
        finally:
            closed.set()

    monkeypatch.setattr(source._decoder, "run", blocked_decoder)
    source.advance(0)
    stream = source.run()
    pending = asyncio.create_task(anext(stream))
    await asyncio.wait_for(entered.wait(), 3)
    if cancel:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 5)
    else:
        await asyncio.wait_for(source.stop(), 5)
        with pytest.raises(StopAsyncIteration):
            await pending
    assert closed.is_set()
    assert source._decoder._stderr_task.done()
    await stream.aclose()


@pytest.mark.asyncio
async def test_cancelling_stop_still_finishes_cleanup(wav_file, monkeypatch):
    path, _ = wav_file(30)
    source = ControlledMediaSource(path)
    await source.start()
    proc = source._decoder._proc
    entered = asyncio.Event()
    release = asyncio.Event()
    decoder_stop = source._decoder.stop

    async def delayed_stop():
        entered.set()
        await release.wait()
        await decoder_stop()

    monkeypatch.setattr(source._decoder, "stop", delayed_stop)
    stopping = asyncio.create_task(source.stop())
    await asyncio.wait_for(entered.wait(), 3)
    stopping.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stopping, 5)
    assert proc.returncode is not None
    assert source._decoder._stderr_task.done()
    assert source._decoder._exit_task.done()
    await source.stop()


@pytest.mark.asyncio
async def test_closing_generator_after_frame_reaps_decoder(wav_file):
    path, _ = wav_file(30)
    source = ControlledMediaSource(path)
    source.advance(0)
    stream = source.run()
    await asyncio.wait_for(anext(stream), 3)
    proc = source._decoder._proc
    await asyncio.wait_for(stream.aclose(), 5)
    assert proc.returncode is not None
    assert source._decoder._stderr_task.done()
    assert source._decoder._exit_task.done()


@pytest.mark.asyncio
async def test_stop_before_start_and_repeated_start(wav_file):
    path, _ = wav_file()
    stopped = ControlledMediaSource(path)
    await stopped.stop()
    await stopped.start()
    assert [frame async for frame in stopped.run()] == []
    assert stopped._decoder._proc is None
    source = ControlledMediaSource(path)
    await asyncio.gather(source.start(), source.start())
    proc = source._decoder._proc
    await source.start()
    assert source._decoder._proc is proc
    await asyncio.gather(source.stop(), source.stop())
    assert proc.returncode is not None


def test_mapping_interpolates_clamps_and_corrects_nonmonotonic_capture():
    source = ControlledMediaSource("movie.mp4", start_seconds=5)
    assert source.media_time(100) == 5
    source._record_capture(100, 5)
    source._record_capture(100.002, 5.02)
    assert source.media_time(100.001) == pytest.approx(5.01)
    assert source.media_time(99) == 5
    assert source.media_time(101) == 5.02
    equal = source._record_capture(100.002, 5.04)
    backward = source._record_capture(99, 5.06)
    assert 100.002 < equal < backward < 100.003
    assert source.media_time(equal) == 5.04
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="capture_time"):
            source.media_time(bad)


@pytest.mark.parametrize("capture_interval", [0.001, 0.02, 0.1])
def test_mapping_is_bounded_and_retains_120_seconds(capture_interval):
    source = ControlledMediaSource("movie.mp4")
    count = 4 * _MAX_TIMESTAMPS
    for i in range(count):
        source._record_capture(1000 + i * capture_interval, i * FRAME_SECONDS)
        assert len(source._capture_times) <= _MAX_TIMESTAMPS
    assert len(source._capture_times) == len(source._media_times)
    # Recent 120 seconds of media retain exact per-frame mappings, even in bursts.
    for i in range(count - _RECENT_FRAMES, count):
        assert source.media_time(1000 + i * capture_interval) == pytest.approx(i * 0.02)
    # The capture window retains an anchor at or before its 120-second boundary.
    last_capture = 1000 + (count - 1) * capture_interval
    assert source._capture_times[0] <= max(1000, last_capture - 120)
    midpoint = (source._capture_times[0] + last_capture) / 2
    expected = (midpoint - 1000) / capture_interval * FRAME_SECONDS
    assert source.media_time(midpoint) == pytest.approx(expected)

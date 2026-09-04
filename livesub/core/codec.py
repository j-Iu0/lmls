"""Serialisation for the command-line pipe form of the pipeline.

The same modules that talk over the in-process bus can be run as separate OS processes:

    livesub-input mic --raw | livesub-denoise spectral --raw | livesub-transcribe ...

Audio stages speak raw ``f32le`` mono PCM at 16 kHz on stdin/stdout -- headerless, so a
stage can start emitting before it knows how long the stream is, and directly playable
with ``ffplay -f f32le -ar 16000 -ch_layout mono -``.

Text stages speak JSON Lines, one ``TextFrame`` per line. Because every text topic
carries the same frame type, any text stage can be piped into any other -- including
piping ASR straight into the translator with no corrector in between.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any, AsyncIterator, BinaryIO, Iterator, TextIO

import numpy as np

from .types import FRAME_SAMPLES, SAMPLE_RATE, AudioFrame, Lineage, TextFrame

# -- audio -------------------------------------------------------------------


def frame_to_bytes(frame: AudioFrame) -> bytes:
    return frame.pcm.astype(np.float32, copy=False).tobytes()


def read_frames(stream: BinaryIO | None = None) -> Iterator[AudioFrame]:
    """Blocking read of fixed-size frames from a binary stream."""
    stream = stream or sys.stdin.buffer
    nbytes = FRAME_SAMPLES * 4
    seq = 0
    while True:
        buf = stream.read(nbytes)
        if not buf:
            return
        if len(buf) < nbytes:  # pad a short final frame
            buf = buf + b"\x00" * (nbytes - len(buf))
        yield AudioFrame(np.frombuffer(buf, dtype=np.float32).copy(), SAMPLE_RATE, seq)
        seq += 1


async def aread_frames(stream: BinaryIO | None = None) -> AsyncIterator[AudioFrame]:
    """Async wrapper so a stdin pipe can drive the same code path as a live device."""
    loop = asyncio.get_running_loop()
    stream = stream or sys.stdin.buffer
    nbytes = FRAME_SAMPLES * 4
    seq = 0
    while True:
        buf = await loop.run_in_executor(None, stream.read, nbytes)
        if not buf:
            return
        if len(buf) < nbytes:
            buf = buf + b"\x00" * (nbytes - len(buf))
        yield AudioFrame(np.frombuffer(buf, dtype=np.float32).copy(), SAMPLE_RATE, seq)
        seq += 1


def write_frame(frame: AudioFrame, stream: BinaryIO | None = None) -> None:
    stream = stream or sys.stdout.buffer
    stream.write(frame_to_bytes(frame))
    stream.flush()


# -- text --------------------------------------------------------------------


def event_to_dict(event: TextFrame) -> dict[str, Any]:
    return {
        "segment_id": event.segment_id,
        "revision": event.revision,
        "lang": event.lang,
        "text": event.text,
        "is_final": event.is_final,
        "t_audio_end": event.lineage.t_audio_end or 0.0,
        "t_emit": time.time(),
        "end_to_end_ms": event.end_to_end_ms,
        "stage_latency_ms": event.lineage.stage_latency_ms,
        "meta": event.meta,
    }


def event_from_dict(data: dict[str, Any]) -> TextFrame:
    lineage = Lineage(
        segment_id=data["segment_id"],
        revision=int(data.get("revision", 0)),
        t_audio_end=float(data.get("t_audio_end", 0.0)) or None,
        stage_latency_ms=dict(data.get("stage_latency_ms", {})),
    )
    return TextFrame(
        text=data.get("text", ""),
        lang=data.get("lang", "en"),
        is_final=bool(data.get("is_final", True)),
        lineage=lineage,
        meta=dict(data.get("meta", {})),
    )


def write_event(event: TextFrame, stream: TextIO | None = None) -> None:
    stream = stream or sys.stdout
    stream.write(json.dumps(event_to_dict(event), ensure_ascii=False) + "\n")
    stream.flush()


def read_events(stream: TextIO | None = None) -> Iterator[TextFrame]:
    stream = stream or sys.stdin
    for line in stream:
        line = line.strip()
        if line:
            yield event_from_dict(json.loads(line))


async def aread_events(stream: TextIO | None = None) -> AsyncIterator[TextFrame]:
    loop = asyncio.get_running_loop()
    stream = stream or sys.stdin
    while True:
        line = await loop.run_in_executor(None, stream.readline)
        if not line:
            return
        line = line.strip()
        if line:
            yield event_from_dict(json.loads(line))

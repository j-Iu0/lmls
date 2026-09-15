"""Continuous cloud transcription through Deepgram's Listen v1 WebSocket.

Unlike Whisper, Deepgram is already a streaming recogniser with its own VAD and
endpointing.  This adapter therefore consumes canonical 20 ms :class:`AudioFrame`s
directly.  Putting the local utterance segmenter in front would discard timing context,
add latency, and duplicate Deepgram's endpointing.

The provider connection is full duplex: audio must continue upstream while transcript
events arrive independently downstream.  ``process_stream`` is the graph's streaming
transform contract for exactly that shape.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import re
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np

from ..core.interfaces import Module
from ..core.startup import StartupPhase
from ..core.types import SAMPLE_RATE, AudioFrame, Lineage, TextFrame

log = logging.getLogger("lmls.transcribe.deepgram")

_INPUT_DONE = object()


@dataclass(slots=True)
class _ConnectionFailure:
    error: BaseException


@dataclass(slots=True)
class _AudioClock:
    start_sample: int
    end_sample: int
    t_capture: float
    sample_rate: int


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read an SDK model or a plain mapping (the latter keeps tests dependency-free)."""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def pcm16le(pcm: np.ndarray) -> bytes:
    """Convert canonical float PCM to the raw linear16 format Deepgram expects."""
    clean = np.nan_to_num(pcm.astype(np.float32, copy=False), nan=0.0, posinf=1.0,
                          neginf=-1.0)
    return (np.clip(clean, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class DeepgramTranscriber(Module):
    """Stream raw audio to Deepgram Nova and emit interim/final text.

    Args:
        model: Deepgram speech-to-text model. ``nova-3`` is the general-purpose
            streaming model recommended for new transcription applications.
        language: BCP-47 language hint, or ``multi`` for Nova-3 multilingual.
        endpointing_ms: silence Deepgram uses to mark a natural speech endpoint.
        smart_format: format dates, numbers, punctuation, and similar entities.
        keyterms: up to 100 plain Nova-3 terms or phrases to bias recognition toward.
        mip_opt_out: opt raw audio out of Deepgram's Model Improvement Program.
        finalize_on_is_final: translate each stable provider chunk without waiting for
            the speaker to stop long enough for ``speech_final``.
        split_sentences: split a stable chunk at sentence-ending punctuation.
        max_final_words: fallback cap when a stable chunk has no sentence boundary.
        keepalive_s: send a control keepalive if the input stream itself is idle.
        connect_timeout_s: maximum time to establish the provider WebSocket.
        final_timeout_s: after local input ends, wait this long for Finalize results.

    ``api_key`` is injected by the registry from the configured key file.  This module
    never reads environment variables or configuration files itself.
    """

    inputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}
    outputs: ClassVar[dict[str, type]] = {"text": TextFrame}
    secret_file_options: ClassVar = {
        "api_key": ("api_key_file", "api_key_name", "DEEPGRAM")
    }

    def __init__(
        self,
        api_key: str,
        model: str = "nova-3",
        language: str = "en",
        endpointing_ms: int = 120,
        smart_format: bool = True,
        keyterms: list[str] | None = None,
        mip_opt_out: bool = True,
        finalize_on_is_final: bool = True,
        split_sentences: bool = True,
        max_final_words: int = 28,
        keepalive_s: float = 4.0,
        connect_timeout_s: float = 10.0,
        final_timeout_s: float = 5.0,
        **_: Any,
    ):
        super().__init__()
        if not api_key.strip():
            raise ValueError("Deepgram api_key must not be empty")
        if endpointing_ms < 0:
            raise ValueError("endpointing_ms must be non-negative")
        if keepalive_s <= 0 or connect_timeout_s <= 0 or final_timeout_s <= 0:
            raise ValueError("Deepgram timeout and keepalive values must be positive")
        if keyterms is not None and len(keyterms) > 100:
            raise ValueError("Deepgram Nova-3 accepts at most 100 keyterms")
        if max_final_words < 1:
            raise ValueError("max_final_words must be positive")

        self.model = model
        self.language = language
        self.endpointing_ms = endpointing_ms
        self.smart_format = smart_format
        self.keyterms = list(keyterms or [])
        self.mip_opt_out = mip_opt_out
        self.finalize_on_is_final = finalize_on_is_final
        self.split_sentences = split_sentences
        self.max_final_words = max_final_words
        self.keepalive_s = keepalive_s
        self.connect_timeout_s = connect_timeout_s
        self.final_timeout_s = final_timeout_s
        self._api_key = api_key

        self._client: Any = None
        self._event_type: Any = None
        self._connection: Any = None
        self._last_send = 0.0
        self._sent_samples = 0
        self._clock: deque[_AudioClock] = deque()
        self._segments = 0
        self._failures = 0

        self._active_id: str | None = None
        self._committed: list[str] = []
        self._committed_ranges: set[tuple[float, float]] = set()
        self._last_visible = ""
        self._active_audio_end: float | None = None
        self._last_confidence: float | None = None
        self._request_id: str | None = None

    async def start(self) -> None:
        if self._client is not None:
            return
        self._report_startup(StartupPhase.IN_PROGRESS, "initialising Deepgram client")
        try:
            from deepgram import AsyncDeepgramClient
            from deepgram.core.events import EventType

            self._client = AsyncDeepgramClient(api_key=self._api_key)
            self._event_type = EventType
        except ImportError as exc:
            message = (
                "Deepgram SDK is not installed; "
                "pip install -r requirements-deepgram.txt"
            )
            self._report_startup(StartupPhase.FAILED, message)
            raise ImportError(message) from exc
        except Exception as exc:
            self._report_startup(StartupPhase.FAILED, str(exc))
            raise
        self._report_startup(StartupPhase.READY)

    async def stop(self) -> None:
        # The stream runner normally owns connection shutdown.  This is also safe when
        # startup failed or Graph cleanup calls stop after cancellation.
        connection, self._connection = self._connection, None
        if connection is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await connection.send_close_stream()

    def _connect_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "model": self.model,
            "language": self.language,
            "encoding": "linear16",
            "sample_rate": SAMPLE_RATE,
            "channels": 1,
            "interim_results": True,
            "endpointing": self.endpointing_ms,
            "smart_format": self.smart_format,
            "mip_opt_out": self.mip_opt_out,
        }
        if self.keyterms:
            options["keyterm"] = self.keyterms
        return options

    @asynccontextmanager
    async def _open_connection(self, events: asyncio.Queue[Any]):
        assert self._client is not None and self._event_type is not None
        opened = asyncio.Event()
        loop = asyncio.get_running_loop()

        def enqueue(value: Any) -> None:
            loop.call_soon_threadsafe(events.put_nowait, value)

        manager = self._client.listen.v1.connect(**self._connect_options())
        async with manager as connection:
            connection.on(self._event_type.OPEN, lambda *_: loop.call_soon_threadsafe(opened.set))
            connection.on(self._event_type.MESSAGE, lambda message: enqueue(message))
            connection.on(
                self._event_type.ERROR,
                lambda error: enqueue(_ConnectionFailure(
                    error if isinstance(error, BaseException) else RuntimeError(str(error))
                )),
            )
            connection.on(
                self._event_type.CLOSE,
                lambda *_: log.debug("Deepgram WebSocket closed"),
            )
            listener = asyncio.create_task(
                connection.start_listening(), name=f"{self.name}:deepgram-listen"
            )
            try:
                await asyncio.wait_for(opened.wait(), timeout=self.connect_timeout_s)
                self._connection = connection
                self._last_send = time.monotonic()
                yield connection
            finally:
                self._connection = None
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await connection.send_close_stream()
                if not listener.done():
                    listener.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await listener

    async def process_stream(
        self, input_stream: AsyncIterator[tuple[str, AudioFrame]]
    ) -> AsyncIterator[TextFrame]:
        if self._client is None:
            await self.start()

        events: asyncio.Queue[Any] = asyncio.Queue()
        async with self._open_connection(events) as connection:
            sender = asyncio.create_task(
                self._send_audio(connection, input_stream, events),
                name=f"{self.name}:deepgram-send",
            )
            keepalive = asyncio.create_task(
                self._keepalive(connection, events),
                name=f"{self.name}:deepgram-keepalive",
            )
            input_done = False
            final_deadline = 0.0
            try:
                while True:
                    timeout = None
                    if input_done:
                        timeout = max(0.0, final_deadline - time.monotonic())
                        if timeout == 0:
                            final = self._flush_active()
                            if final is not None:
                                yield final
                            return
                    try:
                        item = await asyncio.wait_for(events.get(), timeout=timeout)
                    except asyncio.TimeoutError:
                        final = self._flush_active()
                        if final is not None:
                            yield final
                        return

                    if item is _INPUT_DONE:
                        sender.result()
                        input_done = True
                        await connection.send_finalize()
                        self._last_send = time.monotonic()
                        final_deadline = time.monotonic() + self.final_timeout_s
                        continue
                    if isinstance(item, _ConnectionFailure):
                        self._failures += 1
                        raise RuntimeError(
                            f"Deepgram streaming failed: {item.error}"
                        ) from item.error

                    for frame in self._result_frames(item):
                        yield frame
                    if input_done and bool(_field(item, "from_finalize", False)):
                        final = self._flush_active()
                        if final is not None:
                            yield final
                        return
            finally:
                for task in (sender, keepalive):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(sender, keepalive, return_exceptions=True)

    async def _send_audio(
        self,
        connection: Any,
        frames: AsyncIterator[tuple[str, AudioFrame]],
        events: asyncio.Queue[Any],
    ) -> None:
        try:
            async for _, frame in frames:
                if frame.sample_rate != SAMPLE_RATE:
                    raise ValueError(
                        f"Deepgram expects canonical {SAMPLE_RATE} Hz audio, got "
                        f"{frame.sample_rate} Hz"
                    )
                start = self._sent_samples
                end = start + len(frame.pcm)
                await connection.send_media(pcm16le(frame.pcm))
                self._clock.append(_AudioClock(
                    start_sample=start,
                    end_sample=end,
                    t_capture=frame.t_capture,
                    sample_rate=frame.sample_rate,
                ))
                self._sent_samples = end
                self._last_send = time.monotonic()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await events.put(_ConnectionFailure(exc))
        else:
            await events.put(_INPUT_DONE)

    async def _keepalive(self, connection: Any, events: asyncio.Queue[Any]) -> None:
        try:
            while True:
                await asyncio.sleep(self.keepalive_s)
                if time.monotonic() - self._last_send >= self.keepalive_s:
                    await connection.send_keep_alive()
                    self._last_send = time.monotonic()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await events.put(_ConnectionFailure(exc))

    def _result_frames(self, message: Any) -> list[TextFrame]:
        channel = _field(message, "channel")
        alternatives = _field(channel, "alternatives", []) if channel is not None else []
        alternative = alternatives[0] if alternatives else None
        transcript = str(_field(alternative, "transcript", "") or "").strip()
        is_chunk_final = bool(_field(message, "is_final", False))
        speech_final = bool(_field(message, "speech_final", False))
        from_finalize = bool(_field(message, "from_finalize", False))
        if not transcript and not ((speech_final or from_finalize) and self._active_id):
            return []

        start = float(_field(message, "start", 0.0) or 0.0)
        duration = float(_field(message, "duration", 0.0) or 0.0)
        end = start + duration
        if transcript and self._active_id is None:
            self._active_id = self._new_segment_id()

        confidence = _field(alternative, "confidence")
        if confidence is not None:
            self._last_confidence = float(confidence)
        metadata = _field(message, "metadata")
        request_id = _field(metadata, "request_id") if metadata is not None else None
        if request_id:
            self._request_id = str(request_id)

        # A provider-final chunk is stable even when ``speech_final`` is false because
        # the lecturer has not paused.  Finalising it now lets correction/translation
        # begin without concatenating several stable sentences into one long request.
        if self.finalize_on_is_final and is_chunk_final and transcript:
            return self._final_chunk_frames(
                transcript, alternative, start=start, duration=duration
            )

        if is_chunk_final and transcript:
            key = (round(start, 6), round(duration, 6))
            if key not in self._committed_ranges:
                self._committed_ranges.add(key)
                self._committed.append(transcript)
            interim = ""
        else:
            interim = transcript

        if duration:
            self._active_audio_end = self._capture_time(end)
        visible = " ".join(part for part in [*self._committed, interim] if part).strip()
        final = speech_final or from_finalize
        if not visible or (visible == self._last_visible and not final):
            return []
        self._last_visible = visible
        frame = self._make_frame(visible, final)
        if final:
            self._reset_segment(end)
        return [frame]

    def _final_chunk_frames(
        self,
        transcript: str,
        alternative: Any,
        *,
        start: float,
        duration: float,
    ) -> list[TextFrame]:
        pieces = self._split_final_text(transcript)
        if not pieces:
            return []

        provider_words = list(_field(alternative, "words", []) or [])
        total_words = max(1, sum(len(piece.split()) for piece in pieces))
        consumed_words = 0
        first_id = self._active_id or self._new_segment_id()
        frames: list[TextFrame] = []
        for index, piece in enumerate(pieces):
            consumed_words += len(piece.split())
            if provider_words:
                provider_word = provider_words[
                    min(consumed_words - 1, len(provider_words) - 1)
                ]
                piece_end = float(
                    _field(provider_word, "end", start + duration) or 0.0
                )
            else:
                piece_end = start + duration * consumed_words / total_words
            frames.append(self._make_frame(
                piece,
                True,
                segment_id=first_id if index == 0 else self._new_segment_id(),
                audio_end=self._capture_time(piece_end),
                meta_extra={
                    "provider_start_s": start,
                    "provider_end_s": piece_end,
                },
            ))

        self._reset_segment(start + duration)
        return frames

    def _split_final_text(self, text: str) -> list[str]:
        sentences = (
            re.split(r"(?<=[.!?])\s+", text.strip())
            if self.split_sentences
            else [text.strip()]
        )
        pieces: list[str] = []
        for sentence in sentences:
            words = sentence.split()
            while len(words) > self.max_final_words:
                cut = self.max_final_words
                # Prefer a nearby clause boundary to a mechanical word-count cut.
                for candidate in range(
                    self.max_final_words, self.max_final_words // 2, -1
                ):
                    if words[candidate - 1].endswith((",", ";", ":")):
                        cut = candidate
                        break
                pieces.append(" ".join(words[:cut]))
                words = words[cut:]
            if words:
                pieces.append(" ".join(words))
        return pieces

    def _new_segment_id(self) -> str:
        self._segments += 1
        return f"dg-{uuid.uuid4().hex[:12]}"

    def _capture_time(self, stream_seconds: float) -> float:
        sample = max(0, int(round(stream_seconds * SAMPLE_RATE)))
        for item in self._clock:
            if item.start_sample <= sample <= item.end_sample:
                offset = (sample - item.start_sample) / item.sample_rate
                return item.t_capture + offset
        if self._clock:
            last = self._clock[-1]
            return last.t_capture + (last.end_sample - last.start_sample) / last.sample_rate
        return time.time()

    def _make_frame(
        self,
        text: str,
        final: bool,
        *,
        segment_id: str | None = None,
        audio_end: float | None = None,
        meta_extra: dict[str, Any] | None = None,
    ) -> TextFrame:
        segment_id = segment_id or self._active_id
        assert segment_id is not None
        return TextFrame(
            text=text,
            lang=self.language if self.language != "multi" else "und",
            is_final=final,
            lineage=Lineage(
                segment_id=segment_id,
                t_audio_end=audio_end or self._active_audio_end or time.time(),
            ),
            meta={
                "provider": "deepgram",
                "model": self.model,
                "confidence": self._last_confidence,
                "request_id": self._request_id,
                **(meta_extra or {}),
            },
        )

    def _flush_active(self) -> TextFrame | None:
        if self._active_id is None or not self._last_visible:
            return None
        frame = self._make_frame(self._last_visible, True)
        self._reset_segment(None)
        return frame

    def transcribe_array(self, pcm: np.ndarray, sample_rate: int) -> list[TextFrame]:
        """Transcribe an already-loaded file once through the prerecorded API.

        The live graph uses ``process_stream``.  A file is different: replaying it
        through a realtime WebSocket would be slower and more fragile than one normal
        media request, and local segmentation would once again duplicate the provider.
        """
        try:
            from deepgram import DeepgramClient
        except ImportError as exc:
            raise ImportError(
                "Deepgram SDK is not installed; pip install -r requirements-deepgram.txt"
            ) from exc

        from ..core.audioutil import resample

        pcm = resample(pcm, sample_rate, SAMPLE_RATE)
        audio_available = time.time()
        client = DeepgramClient(api_key=self._api_key)
        options = self._connect_options()
        options.pop("interim_results", None)
        options.pop("endpointing", None)
        options.pop("encoding", None)
        options.pop("sample_rate", None)
        options.pop("channels", None)
        options["utterances"] = True

        # The generated v7 prerecorded method does not expose raw-audio sample-rate
        # parameters.  A standard in-memory PCM WAV carries that information in its
        # header and is accepted without a temporary file.
        import soundfile as sf

        encoded = io.BytesIO()
        sf.write(encoded, pcm, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        response = client.listen.v1.media.transcribe_file(
            request=encoded.getvalue(), **options
        )
        response = _field(response, "data", response)
        results = _field(response, "results")
        utterances = _field(results, "utterances", []) if results is not None else []
        frames: list[TextFrame] = []
        for item in utterances or []:
            text = str(_field(item, "transcript", "") or "").strip()
            if not text:
                continue
            start_s = float(_field(item, "start", 0.0) or 0.0)
            end_s = float(_field(item, "end", start_s) or start_s)
            frames.append(TextFrame(
                text=text,
                lang=str(_field(item, "language", self.language) or self.language),
                lineage=Lineage(
                    segment_id=f"dg-{uuid.uuid4().hex[:12]}",
                    t_audio_end=audio_available,
                ),
                meta={
                    "provider": "deepgram",
                    "model": self.model,
                    "confidence": _field(item, "confidence"),
                    "provider_start_s": start_s,
                    "provider_end_s": end_s,
                },
            ))
        if frames:
            return frames

        channels = _field(results, "channels", []) if results is not None else []
        channel = channels[0] if channels else None
        alternatives = _field(channel, "alternatives", []) if channel is not None else []
        alternative = alternatives[0] if alternatives else None
        text = str(_field(alternative, "transcript", "") or "").strip()
        if not text:
            return []
        return [TextFrame(
            text=text,
            lang=self.language if self.language != "multi" else "und",
            lineage=Lineage(
                segment_id=f"dg-{uuid.uuid4().hex[:12]}",
                t_audio_end=audio_available,
            ),
            meta={
                "provider": "deepgram",
                "model": self.model,
                "confidence": _field(alternative, "confidence"),
            },
        )]

    def _reset_segment(self, end_seconds: float | None) -> None:
        if end_seconds is not None:
            cutoff = max(0, int(end_seconds * SAMPLE_RATE) - SAMPLE_RATE)
            while self._clock and self._clock[0].end_sample < cutoff:
                self._clock.popleft()
        self._active_id = None
        self._committed.clear()
        self._committed_ranges.clear()
        self._last_visible = ""
        self._active_audio_end = None
        self._last_confidence = None

    def describe(self) -> dict[str, Any]:
        return {
            "module": type(self).__name__,
            "name": self.name,
            "backend": "deepgram",
            "model": self.model,
            "language": self.language,
            "connected": self._connection is not None,
            "segments": self._segments,
            "audio_s_sent": round(self._sent_samples / SAMPLE_RATE, 2),
            "failures": self._failures,
        }

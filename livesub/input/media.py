"""Decode media only as far as an external playback clock permits."""

from __future__ import annotations

import asyncio
import math
from bisect import bisect_left
from contextlib import suppress
from typing import Any, AsyncIterator, ClassVar

from ..core.audioutil import db, rms
from ..core.interfaces import Module
from ..core.types import FRAME_SAMPLES, SAMPLE_RATE, AudioFrame
from .ffmpeg_source import FfmpegSource

_FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE
_HISTORY_SECONDS = 120.0
_RECENT_FRAMES = math.ceil(_HISTORY_SECONDS / _FRAME_SECONDS) + 1
_MAX_TIMESTAMPS = 2 * _RECENT_FRAMES + 2


class _ManagedFfmpegSource(FfmpegSource):
    """Reuse decoding, but retain and join all subprocess-related tasks."""

    def __init__(self, changed: asyncio.Event, **kwargs: Any):
        super().__init__(**kwargs)
        self._changed = changed
        self._stderr_task: asyncio.Task | None = None
        self._exit_task: asyncio.Task | None = None

    async def start(self) -> None:
        # FfmpegSource.start() discards its stderr task handle. Own that small
        # lifecycle boundary here while using its command and frame decoder.
        self._proc = await asyncio.create_subprocess_exec(
            *self._build_command(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._exit_task = asyncio.create_task(self._watch_exit())

    async def _watch_exit(self) -> None:
        assert self._proc is not None
        await self._proc.wait()
        self._changed.set()  # EOF/errors must also wake a paused consumer.

    async def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.terminate()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
        # Drain stdout too: wait() alone can deadlock on a full, paused pipe.
        try:
            await asyncio.wait_for(proc.communicate(), timeout=2)
        except asyncio.TimeoutError:  # pragma: no cover - uncooperative process
            with suppress(ProcessLookupError):
                proc.kill()
            await proc.communicate()
        if self._exit_task is not None:
            await self._exit_task
        self._proc = None


class ControlledMediaSource(Module):
    """A single-use media decoder driven by absolute media playback seconds.

    Call ``advance()`` on the event-loop thread to enable decoding. Only complete
    canonical frames ending at or before playback + lookahead are admitted.
    ``position`` is the end of emitted audio, including a padded final frame.
    See ``docs/media-transport.md`` for clock conversion and lifecycle details.
    """

    inputs: ClassVar[dict[str, type]] = {}
    outputs: ClassVar[dict[str, type]] = {"audio": AudioFrame}

    def __init__(
        self,
        url: str,
        start_seconds: float = 0,
        lookahead_seconds: float = 0.5,
        **kwargs: Any,
    ):
        super().__init__()
        if not url:
            raise ValueError("media source needs 'url'")
        self.url = url
        self.start_seconds = self._seconds(start_seconds, "start_seconds")
        self.lookahead_seconds = self._seconds(lookahead_seconds, "lookahead_seconds")
        self._playback = self.start_seconds
        self._frames = 0
        self._advanced = False
        self._stopped = False
        self._running = False
        self._changed = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._read_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._stream: AsyncIterator[AudioFrame] | None = None
        self._level_dbfs = -120.0
        self._capture_times: list[float] = []
        self._media_times: list[float] = []
        options = dict(kwargs)
        extra_args = list(options.pop("extra_args", None) or [])
        # Input seeking with transcoding uses accurate_seek; make it explicit
        # after caller options so start_seconds controls the actual offset.
        options.update(
            url=url, realtime=False,
            extra_args=extra_args + ["-ss", str(self.start_seconds), "-accurate_seek"],
        )
        self._decoder = _ManagedFfmpegSource(self._changed, **options)

    @staticmethod
    def _seconds(value: float, name: str) -> float:
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative")
        return value

    @property
    def position(self) -> float:
        """Absolute media position at the end of decoded/emitted frames."""
        return self.start_seconds + self._frames * _FRAME_SECONDS

    def advance(self, position: float) -> None:
        """Supply playback seconds; tolerate up to 250 ms of clock jitter.

        Small regressions are clamped to the previous high-water mark. Larger
        regressions require a new source, as does any actual seek/restart.
        """
        position = self._seconds(position, "position")
        if position < self._playback - 0.25:
            raise ValueError("backward seek requires a new media source instance")
        self._playback = max(self._playback, position)
        self._advanced = True
        self._changed.set()

    def _permitted(self) -> bool:
        end = self.start_seconds + (self._frames + 1) * _FRAME_SECONDS
        return self._advanced and end <= self._playback + self.lookahead_seconds + 1e-9

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if not self._stopped and self._decoder._proc is None:
                await self._decoder.start()

    async def stop(self) -> None:
        """Wake the gate, cancel any pending read, and reap ffmpeg. Idempotent."""
        self._stopped = True
        self._changed.set()
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._close_decoder())
        try:
            await asyncio.shield(self._cleanup_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._cleanup_task)
            raise

    async def _close_decoder(self) -> None:
        async with self._lifecycle_lock:
            if self._read_task is not None:
                self._read_task.cancel()
                await asyncio.gather(self._read_task, return_exceptions=True)
            if self._stream is not None:
                await self._stream.aclose()
            await self._decoder.stop()

    async def run(self) -> AsyncIterator[AudioFrame]:
        if self._running:
            raise RuntimeError("media source supports only one run consumer")
        self._running = True
        try:
            await self.start()
            if self._stopped:
                return
            self._stream = self._decoder.run()
            while not self._stopped:
                self._changed.clear()
                proc = self._decoder._proc
                assert proc is not None and proc.stdout is not None
                # Observing an empty EOF pipe consumes no frame or clock budget.
                if not self._permitted() and not proc.stdout.at_eof():
                    await self._changed.wait()
                    continue
                self._read_task = asyncio.create_task(anext(self._stream))
                try:
                    frame = await self._read_task
                except StopAsyncIteration:
                    if self._decoder._stderr_task is not None:
                        await self._decoder._stderr_task
                    if proc.returncode:
                        raise RuntimeError(
                            f"ffmpeg exited {proc.returncode} reading {self.url!r}: "
                            + "\n".join(self._decoder._stderr_tail)
                        )
                    return
                except asyncio.CancelledError:
                    if self._stopped:
                        return
                    raise
                finally:
                    self._read_task = None
                if self._stopped:
                    return
                # FfmpegSource stamps time.time() immediately after reading PCM.
                # Never fabricate a 20 ms wall-clock interval for a fast decode.
                frame.t_capture = self._record_capture(frame.t_capture, self.position)
                self._frames += 1
                self._level_dbfs = db(rms(frame.pcm))
                yield frame
        finally:
            await self.stop()
            self._running = False

    def _record_capture(self, capture: float, offset: float) -> float:
        if self._capture_times:
            capture = max(capture, math.nextafter(self._capture_times[-1], math.inf))
        self._capture_times.append(capture)
        self._media_times.append(offset)
        # Keep both 120 s of capture history (plus its preceding anchor) and
        # exact frame entries for at least 120 s of decoded audio.
        discard = min(
            max(0, bisect_left(self._capture_times, capture - _HISTORY_SECONDS) - 1),
            max(0, len(self._capture_times) - _RECENT_FRAMES),
        )
        if discard:
            del self._capture_times[:discard]
            del self._media_times[:discard]
        if len(self._capture_times) > _MAX_TIMESTAMPS:
            # Arbitrarily fast decoding cannot retain every wall-window sample
            # in bounded space. Thin older anchors, keeping the recent audio exact.
            split = len(self._capture_times) - _RECENT_FRAMES
            self._capture_times[:split] = self._capture_times[:split:2]
            self._media_times[:split] = self._media_times[:split:2]
        return capture

    def media_time(self, capture_time: float) -> float:
        """Interpolate capture timestamps to frame-start media offsets.

        Outside retained history, use the nearest endpoint. Before the first
        frame, return start_seconds. For an utterance end, the caller should use
        ``media_time(t_end - frame_duration) + frame_duration``.
        """
        capture_time = float(capture_time)
        if not math.isfinite(capture_time):
            raise ValueError("capture_time must be finite")
        if not self._capture_times:
            return self.start_seconds
        right = bisect_left(self._capture_times, capture_time)
        if right == 0:
            return self._media_times[0]
        if right == len(self._capture_times):
            return self._media_times[-1]
        left = right - 1
        fraction = (capture_time - self._capture_times[left]) / (
            self._capture_times[right] - self._capture_times[left]
        )
        return self._media_times[left] + fraction * (
            self._media_times[right] - self._media_times[left]
        )

    def describe(self) -> dict[str, Any]:
        return {
            "stage": "ControlledMediaSource",
            "name": self.name,
            "url": self.url,
            "decoded_s": self.position,
            "playback_s": self._playback,
            "waiting": not self._stopped and not self._permitted(),
            "level_dbfs": self._level_dbfs,
        }

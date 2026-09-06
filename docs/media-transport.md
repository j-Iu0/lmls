# Controlled media transport

`livesub.input.media.ControlledMediaSource` is a general `Module` source,
registered as `media`. It has no input ports and emits `AudioFrame` on its
`audio` output. It accepts any seekable file or URL that the underlying
`FfmpegSource` can decode. It does not control a player, UI, websocket protocol,
or session restarts.

## Construction and playback clock

```python
source = ControlledMediaSource(
    url="lecture.mp4",       # required
    start_seconds=30.135,    # absolute media offset; default 0
    lookahead_seconds=0.5,  # maximum lead over playback; default 0.5
)
await source.start()
source.advance(30.135)
```

Both numeric constructor options must be finite and non-negative. Extra keyword
arguments are forwarded to `FfmpegSource`, including `ffmpeg` and `extra_args`.
The controlled source forces `realtime=False`. It appends input arguments
`-ss <start_seconds> -accurate_seek` after caller-supplied extra arguments.
FFmpeg's accurate input seek discards decoded material before the requested
offset while transcoding to canonical 16 kHz, mono, float32 PCM. Seek accuracy
still depends on the input supporting seeking; offsets ultimately resolve to
audio samples. Device capture is not a seekable media transport.

`advance(position: float)` is synchronous. Call it from the source's event-loop
thread whenever the external player reports its absolute media position in
seconds. Code on another thread should schedule it with
`loop.call_soon_threadsafe(source.advance, position)`.

No frames are read from the decoder iterator or emitted before the first
`advance()`, even with nonzero lookahead. Thereafter, a frame is admitted only
when its **end** is at or before `position + lookahead_seconds` (with a 1 ns
floating-point tolerance). Each canonical frame lasts 20 ms. Thus
`advance(0)` with 50 ms lookahead permits two frames, ending at 40 ms; the frame
ending at 60 ms waits for a later update. Zero lookahead is supported and requires
the playback clock to reach the next frame's end. The gate waits on an
`asyncio.Event`; it does not poll or pace itself with sleeps.

Playback updates are high-water marks. NaN, infinity, and negative values raise
`ValueError` without updating the clock. Backward movement of up to 250 ms is
accepted as jitter and clamped to the previous high-water mark. Larger backward
movement raises `ValueError` explaining that seeking requires a new source.
Successive small backward reports cannot accumulate into a backward seek.
Forward jumps permit decoding all intervening frames; they do not skip audio.
For an actual seek in either direction, the owner stops this instance and
constructs another with the desired `start_seconds`.

The subprocess may decode ahead into its bounded OS and asyncio pipe buffers.
The clock controls reads from its frame iterator and publication, rather than
FFmpeg's internal codec buffers. When playback pauses, already permitted
lookahead may finish decoding and then the gate remains asleep.

## Position, capture timestamps, and subtitle time

`source.position` is the absolute media position at the end of emitted frames,
not the external player's position. It starts at `start_seconds` and advances
by 20 ms per emitted frame. The final partial frame is zero-padded by
`FfmpegSource`, so the reported end can exceed the actual input end by less than
20 ms. Frame sequence numbers begin at zero for each new instance.

`AudioFrame.t_capture` comes from `FfmpegSource` calling `time.time()` immediately
after reading decoded PCM. It represents when audio entered the pipeline, so
latency calculations use actual decode time even when catching up quickly.
Capture timestamps are made strictly increasing: equal timestamps or backward
wall-clock adjustments are raised to the next representable float after the
previous timestamp. No artificial 20 ms wall-clock spacing is introduced.
A backward system clock adjustment necessarily limits wall-clock latency
accuracy until the clock catches up.

`source.media_time(capture_time) -> float` maps a capture timestamp to the
corresponding **frame-start media offset**. It uses binary search over an
ordered timestamp index, linear interpolation between neighboring entries,
and the nearest endpoint outside retained history. Before any frame is emitted,
it returns `start_seconds`. Non-finite lookup timestamps raise `ValueError`.
Interpolation is a best estimate for timestamps that are not exact frame
captures, including across playback pauses.

The index retains exact per-frame mappings for at least the latest 120 seconds
of decoded audio (6,001 entries). It also retains coverage of the latest 120
seconds of capture time and an anchor preceding that window. The hard limit is
12,004 entries. If a large fast-decoding burst exceeds that limit, older anchors
are thinned while the recent 6,001 frames remain exact; interpolation handles
those older timestamps. Entries older than both retention windows are removed.
History remains available after EOF or `stop()` for late subtitle results.

An `Utterance.t_end` conventionally equals the last frame's capture timestamp
**plus that frame's duration**. During fast decoding, adding 20 ms to a capture
timestamp can land among much later decoded frames. The server/transport should
remove that duration before lookup, then add it in media time:

```python
from livesub.core.types import FRAME_SAMPLES, SAMPLE_RATE

frame_duration = FRAME_SAMPLES / SAMPLE_RATE
media_end = source.media_time(utterance.t_end - frame_duration) + frame_duration
# The same conversion applies to TextFrame.lineage.t_audio_end.
```

Subtract one **frame** duration, not the utterance's total duration. This
conversion belongs to the owner that knows whether its input is a frame-start
capture or an utterance end; `media_time()` does not guess. Producers that
synthesize timestamps from sample counts instead of preserving capture
timestamps need their own conversion; the source index cannot reconstruct
provenance that was lost downstream. Existing frame and utterance contracts,
segmenters, and server code are unchanged by this source.

## Lifecycle and status

`run()` starts the decoder automatically if needed. Explicit `start()` is also
supported and idempotent, including concurrent calls. Use one `run()` consumer.
An exhausted source is single-use; calling `run()` after stop/EOF emits nothing.

Normal EOF closes the iterator and subprocess. An empty EOF pipe bypasses the
gate because observing it consumes no frame budget. A subprocess-exit watcher
wakes a gated consumer, including when EOF falls exactly on the playback limit
or FFmpeg fails before producing audio. Buffered audio still requires playback
budget before it can be consumed. Missing FFmpeg raises a `RuntimeError` with
the inherited installation hint. Nonzero decoder exits raise `RuntimeError`
and include available stderr diagnostics.

`stop()` is idempotent and safe before start, during a gate wait, during a read,
and after EOF. It wakes the gate, cancels and joins an in-flight frame read,
closes the decoder iterator, terminates FFmpeg, and drains its pipes. If FFmpeg
does not exit within two seconds, it is killed and reaped. The private lifecycle
wrapper retains and joins both the stderr reader and exit watcher; it reuses
`FfmpegSource` command construction and audio decoding without modifying that
shared class. Cleanup is shielded against cancellation of the caller.

Cancelling an active `run()` iteration also performs cleanup. If a consumer
stops while the generator is suspended at a yielded frame, it should explicitly
close the generator or call `stop()` in its own `finally` block, as with other
resource-owning async iterators.

`describe()` exposes the source stage/name/URL and:

| Field | Meaning |
| --- | --- |
| `decoded_s` | Absolute media end position, equal to `position`. |
| `playback_s` | Last accepted playback high-water mark. |
| `waiting` | True while another frame needs a clock update; false after stop/EOF. |
| `level_dbfs` | RMS level of the latest frame; initially -120 dBFS. Silence uses the shared helper's -240 dBFS floor. |

`waiting` describes the clock gate, not subprocess I/O readiness or downstream
backpressure. The level holds its last value while paused or stopped.

## Verification and changed files

`tests/test_media.py` generates deterministic WAV fixtures and uses actual
FFmpeg to check initial gating, exact frame-end budgets, advance-before-start,
zero lookahead, fractional seek PCM accuracy, tail padding, capture timestamps,
EOF at the playback boundary, invalid inputs, and stop/cancellation cleanup.
FFmpeg-dependent cases skip when the executable is unavailable. Pure unit tests
cover registry resolution, clock validation, jitter, timestamp interpolation,
monotonic corrections, and bounded retention under several decoding speeds.

The implementation is contained in `livesub/input/media.py`. The only registry
change is the `media` registration line in `livesub/core/registry.py`. This
document and `tests/test_media.py` complete the change; UI ownership and session
restart policy remain with the caller.

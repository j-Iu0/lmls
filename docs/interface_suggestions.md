# Interface Architecture Suggestions

Recorded from design review against `module_spec.md`.

---

## 1. Normalize `correct()` / `translate()` return type

Both ABCs return `TextEvent | Sequence[TextEvent]`. Every consumer must branch on the type at every call site. Prefer a uniform return:

```python
async def correct(self, event: TextEvent, context: list[str]) -> list[TextEvent]:
    ...
```

Single-event implementations return `[event]`. The fused stage already returns two — this removes the special case without breaking the fused pattern.

---

## 2. `Stage.name` is a class variable, not an instance variable

```python
class Stage(ABC):
    name: str = "unnamed"  # shared across all instances until overwritten
```

Two instances of the same class share `name` until the registry assigns it. Better:

```python
def __init__(self) -> None:
    self.name: str = "unnamed"
```

Or use `__init_subclass__` / `dataclass` inheritance. The current approach works via instance `__dict__` assignment, but it's implicit and will confuse subclasses that call `super().__init__()`.

---

## 3. `Denoiser.process()` is synchronous but can be CPU-heavy

`DeepFilterNetDenoiser` runs a neural net inside a sync `process()` called in the async loop. This blocks the event loop for the frame's duration. The ABC should provide an async path or an executor helper:

```python
async def process_async(self, frame: AudioFrame) -> AudioFrame:
    """Default: runs process() in the default executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, self.process, frame)
```

The graph would call `process_async()` uniformly; heavy implementations can override it.

---

## 4. `with_stage_latency()` mutates in-place on a `slots=True` dataclass

`TextEvent` is treated as mostly immutable (all derivation goes through `derive()` / `replace()`). But `with_stage_latency()` mutates `stage_latency_ms` directly. Since `derive()` shallow-copies the dict, a latency written before `derive()` is copied to the child correctly — but a latency written *after* `derive()` on the parent silently leaks into the already-derived child's dict (they share the same object until the next `derive()`).

Suggestion: make `with_stage_latency()` return a new event via `derive()` with the updated dict, or at minimum document the mutation contract explicitly in the ABC.

---

## 5. `t_audio_end = 0.0` sentinel is ambiguous

```python
t_audio_end: float = 0.0
```

`end_to_end_ms` guards against it with `if not self.t_audio_end`, which treats epoch 0 as "unset" — fragile near midnight on 1 Jan 1970, and silently wrong if a source accidentally leaves it at zero. Use:

```python
t_audio_end: float | None = None
```

and guard with `if self.t_audio_end is None`.

---

## 6. `Segmenter` injection into `Transcriber` is invisible at the ABC level

Transcribers use segmenters internally, but the choice of segmenter (`energy` vs `silero`) and its config (`silence_ms`, `partial_every_ms`, …) is buried inside each implementation with no standard constructor parameter. Adding a `segmenter` option to the `Transcriber` ABC contract (even just as a documented convention) would make it configurable from TOML without reading each implementation's source:

```toml
[[node]]
module = "transcribe"
impl   = "faster_whisper"
segmenter = "silero"
silence_ms = 300
```

---

## 7. `SubtitleSink.flush()` overlaps with `Stage.stop()`

`Stage.stop()` is the lifecycle hook for releasing resources. `flush()` is "write buffered output before the pipeline exits." These are different semantics, but callers must remember to call `flush()` in addition to `stop()`. Consider merging: either call `flush()` from the default `stop()` implementation in `SubtitleSink`, or remove `flush()` and rely on `stop()` for both.

---

## 8. `context: list[str]` is untyped

Both `correct()` and `translate()` receive `context: list[str]` — recent finalised lines. There's no indication of language, timestamps, or how many items to expect. A lightweight `ContextWindow` type would clarify the contract and make it easier for implementations to use timing information (e.g. for detecting topic switches):

```python
@dataclass
class ContextLine:
    text: str
    lang: str
    t_emit: float
```

This is a minor ergonomic improvement, not a breaking change.

---

## Priority Summary

| # | Issue | Impact | Breaking? |
|---|-------|--------|-----------|
| 1 | Uniform return type | High — simplifies every consumer | Yes, minor |
| 2 | `name` instance var | Medium — subtle bugs in subclasses | Yes, trivial |
| 3 | Async denoiser path | High — blocks event loop today | Yes, minor |
| 4 | Latency mutation aliasing | Medium — silent aliasing bug | No |
| 5 | `t_audio_end` sentinel | Low — theoretical correctness | Yes, trivial |
| 6 | Segmenter injection | Medium — improves configurability | No |
| 7 | flush/stop overlap | Low — ergonomic | No |
| 8 | Typed context | Low — ergonomic | No |

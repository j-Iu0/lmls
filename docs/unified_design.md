# Unified Interface Design

High-level overview of the proposed redesign. Captures all decisions made in the design review.

---

## 1. Motivation

The current design embeds pipeline semantics into the data type (`TextKind = "raw" | "corrected" | "translated"`), forcing the bus and graph to inspect event content for routing decisions. The six ABCs prescribe fixed roles, making novel compositions (double correction, speaker injection, progressive quality) require new ABCs or workarounds like `FusedLlmStage`.

The redesign makes the pipeline semantics-blind. Modules declare typed ports; the config wires them by name; the pipeline routes by type, not by content. Modules remain the only things that know what their data means.

---

## 2. Core Types

Three sibling payload types. All live in `livesub.core.types`.

### 2.1 `AudioFrame`
Unchanged. 16 kHz, mono, float32, 320 samples, fixed size. Published on real-time audio streams. Bus policy: drop-oldest.

### 2.2 `Utterance`
Promoted from internal helper to first-class bus type. Variable-length audio representing a complete (or in-progress) speech region, produced by a Segmenter node.

```python
@dataclass
class Utterance:
    id:       str          # stable across partials and final
    pcm:      np.ndarray   # float32, arbitrary length
    t_start:  float
    t_end:    float
    is_final: bool
```

Bus policy: blocking by default (an utterance is a discrete speech event; silent loss is unacceptable). Supports live/catchup ring buffer mode — see §5.

### 2.3 `TextFrame`

Replaces `TextEvent`. `TextKind` is removed. A `TextFrame` carries text content, its provenance (`Lineage`), and finality — nothing more. The pipeline has no opinion on whether the text is raw, corrected, or translated; that is the module's concern.

```python
@dataclass
class TextFrame:
    text:     str
    lang:     str          # BCP-47
    is_final: bool
    lineage:  Lineage
    meta:     dict[str, Any]
```

### 2.4 `Lineage`

Extracted from `TextFrame`. Carries provenance across the pipeline. **Managed by the bus, not by modules.** When a module publishes a `TextFrame` to a topic, the bus assigns the next revision for that `(segment_id, topic)` pair before delivery.

```python
@dataclass
class Lineage:
    segment_id:       str            # stable across all revisions of one utterance
    revision:         int            # assigned by bus at publish time
    t_audio_end:      float | None   # wall clock of last audio sample; None until set
    stage_latency_ms: dict[str, float]
```

Because the bus manages revision, modules are **lineage-transparent** — they carry the incoming `Lineage` forward without modification. A passthrough relay, a logger, or any module that does not meaningfully transform content simply attaches the same `Lineage` to its output. No `derive()` call required; no risk of accidentally forking the revision chain.

For modules that produce text from audio (Transcribers), the **Transcriber** creates a fresh `Lineage` with a new `segment_id` sourced from `Utterance.id`. The bus only stamps the `revision` on that lineage at publish time.

**Sink display contract** (unchanged in substance): a sink replaces the displayed line for a `(segment_id, topic)` pair when a higher revision arrives, and discards lower revisions. `topic` replaces `kind` as the discriminator — the sink knows what it is displaying from the topic it subscribed to, not from a field inside the event.

---

## 3. Unified Module ABC

The six separate ABCs (`AudioSource`, `Denoiser`, `Transcriber`, `Corrector`, `Translator`, `SubtitleSink`) are replaced by a single `Module` base class. Role is expressed through port declarations, not class hierarchy.

```python
class Module(ABC):
    # Declared at class level by each implementation.
    inputs:  ClassVar[dict[str, type]] = {}
    outputs: ClassVar[dict[str, type]] = {}

    name: str   # instance variable, set by registry at construction time

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def describe(self) -> dict[str, Any]: ...
```

Every module declares its named ports and their payload types:

```python
class EnergySegmenter(Module):
    inputs  = {"audio": AudioFrame}
    outputs = {"utterance": Utterance}

class FasterWhisperTranscriber(Module):
    inputs  = {"audio": Utterance}
    outputs = {"text": TextFrame}

class OllamaLlmCorrector(Module):
    inputs  = {"text_in": TextFrame}
    outputs = {"text_out": TextFrame}

class FusedLlmStage(Module):
    inputs  = {"text_in": TextFrame}
    outputs = {"corrected": TextFrame, "translated": TextFrame}

class MicSource(Module):
    inputs  = {}
    outputs = {"audio": AudioFrame}

class PrettyStdoutSink(Module):
    inputs  = {"text": TextFrame}
    outputs = {}
```

### 3.1 Process method

Modules define their own typed process method. The pipeline runner dispatches by inspecting the port count and types at startup — no runtime type switching on every call. The conventions are:

| Shape | Method signature |
|-------|-----------------|
| Source (no inputs) | `async def run(self) -> AsyncIterator[T]` |
| Transform (one in, one out) | `async def process(self, frame: T) -> U` |
| Multi-output | `async def process(self, frame: T) -> dict[str, U]` |
| Sink (no outputs) | `async def process(self, frame: T) -> None` |

The `dict` return for multi-output is keyed by output port name, matching the `outputs` declaration.

### 3.2 Sync vs async

Modules may define `process` as either `async def` or plain `def`. The pipeline detects which via `inspect.iscoroutinefunction` at startup and wraps sync methods in `loop.run_in_executor` automatically. Module authors write natural Python; the event loop is never blocked.

### 3.3 Internal state

Modules that need conversational context (e.g., LLM correctors needing recent finalized lines) maintain it as internal state, updated in `process()`. Context is **not** a method parameter. The module decides what to remember, how much to keep, and when to clear it (typically in `stop()`).

---

## 4. Configuration

### 4.1 Node fields

Unchanged from current spec except `in`/`out` now connect named ports:

```toml
[[node]]
name = "asr"
impl = "faster_whisper"
[node.in]
audio = "utterance.speech"
[node.out]
text = "text.raw"
model = "small.en"
```

When a module has exactly one input and one output port, shorthand is allowed:

```toml
[[node]]
name = "asr"
impl = "faster_whisper"
in   = "utterance.speech"
out  = "text.raw"
```

Multi-output nodes always use the table form:

```toml
[[node]]
name = "fix"
impl = "fused_llm"
in   = "text.raw"
[node.out]
corrected  = "text.corrected"
translated = "text.out"
target     = "vi"
```

### 4.2 Language configuration

Language-specific modules hardcode their target in config. Configurable modules accept `target` as a constructor kwarg. Both are plain TOML fields passed to `__init__` — no special handling at the interface level.

```toml
# language-specific
target = "vi"

# auto-detect or multilingual
target = "auto"
```

### 4.3 Type validation

The validator resolves the Python type for each topic by tracing it to the first publisher's declared output port type. It then checks every subscriber's input port type against that. No prefix conventions. A mismatch is a `GraphError` at startup before any data flows.

```
topic "utterance.speech"
  publisher:  segmenter.outputs["utterance"] = Utterance  ✓  (establishes type)
  subscriber: asr.inputs["audio"]            = Utterance  ✓
  subscriber: diarizer.inputs["audio"]       = Utterance  ✓
  subscriber: corrector.inputs["text_in"]    = TextFrame  ✗  GraphError
```

---

## 5. Bus: Ring Buffer Policy

Two existing policies are unchanged:

| Policy | Behaviour | Default for |
|--------|-----------|-------------|
| `drop-oldest` | Queue is fixed size; oldest item dropped when full | `AudioFrame` topics |
| `blocking` | Publisher awaits when subscriber queue is full | `TextFrame`, `Utterance` topics |

A third policy is added:

| Policy | Behaviour | Default for |
|--------|-----------|-------------|
| `ring-buffer` | Shared buffer, per-subscriber read positions; no back-pressure on publisher | opt-in |

Under the ring-buffer policy, subscribers declare a **mode** on subscription:

- `live` — read position always jumps to the head; old unread items are skipped. Always real-time, lossy.
- `catchup` — read position advances at the subscriber's own pace; can fall arbitrarily behind, catches up when capacity is available. Never loses items.

### 5.1 Live + catchup configuration

```toml
[[node]]
name  = "asr_live"
impl  = "faster_whisper"
in    = "utterance.speech"
mode  = "live"
model = "small.en"
out   = "text.raw"

[[node]]
name  = "asr_quality"
impl  = "faster_whisper"
in    = "utterance.speech"
mode  = "catchup"
model = "large.en"
out   = "text.raw"
```

Both nodes publish to `text.raw`. The live node emits low-latency low-revision output. The quality node catches up and emits higher-revision output for the same `segment_id`s. The bus-managed revision and the sink display contract handle the progressive replacement automatically.

### 5.2 Skip-if-finalized

A catchup subscriber can declare `skip_if_finalized` pointing to an output topic. The bus tracks which `segment_id`s have received a final event on that topic and skips those positions in the ring buffer for this subscriber. Useful when live and quality nodes use the same model — avoids redundant processing.

```toml
[[node]]
name               = "asr_quality"
mode               = "catchup"
skip_if_finalized  = "text.raw"
```

Defaults to off. Not appropriate when the quality node uses a better model than the live node.

### 5.3 Opt-out

Ring-buffer policy is opt-in. A topic with only `blocking` or `live` subscribers behaves exactly as today. `mode` defaults to `blocking` for `Utterance` and `TextFrame` topics, and `live` (drop-oldest) for `AudioFrame` topics, preserving current behaviour unless explicitly changed.

---

## 6. Segmenter as Pipeline Node

`Segmenter` is promoted from internal transcriber helper to a registered pipeline node. It takes `AudioFrame` and emits `Utterance`.

This separates two concerns that are currently conflated inside `Transcriber`:
- **When does a sentence end?** — the Segmenter's job
- **What was said?** — the Transcriber's job

The transcriber simplifies to a pure `Utterance → TextFrame` transform, using `transcribe_array()` which all backends already support.

Segmenter implementations (`energy`, `silero`, and new additions like `pyannote`, `webrtcvad`) are registered in the registry like any other module and configured from TOML.

```toml
[[node]]
name = "vad"
impl = "silero"
in   = "audio.clean"
out  = "utterance.speech"
silence_ms = 220

[[node]]
name = "asr"
impl = "faster_whisper"
in   = "utterance.speech"
out  = "text.raw"
```

Partial utterances (`is_final=False`) are supported. A segmenter that emits partials enables the transcriber to produce low-latency partial `TextFrame`s, preserving the current responsiveness of the display.

---

## 7. Minor Fixes

### 7.1 `Stage.name` — instance variable

Currently a class variable, shared across instances until overwritten. Changed to an instance variable initialised in `Module.__init__`:

```python
def __init__(self) -> None:
    self.name: str = "unnamed"
```

### 7.2 `SubtitleSink.flush()` — merged into `stop()`

`flush()` (write buffered output) and `stop()` (release resources) are called in the same sequence at pipeline shutdown, with no case where one is needed without the other. `flush()` is removed as a separate method; its responsibility moves into `stop()`. Existing sink implementations move their flush logic into `stop()`.

---

## 8. What Does Not Change

- Registry `build()` / `resolve()` / `available()` API — **`build()` and `resolve()` drop the `kind` parameter** (flat registry; impl name alone identifies the class); `available()` returns all impls as a flat list
- TOML config structure — `[[node]]`, `in`, `out`, `enabled`, extra kwargs to `__init__`
- `chain_config()` shorthand Python API
- `validate()` and `mermaid()` graph utilities — updated internally for port-based types
- `CollectSink` scripting API
- LLM engine singletons (`MlxEngine`, `OllamaEngine`, `CloudEngine`) and prompts module
- `AudioFrame` constants (`SAMPLE_RATE`, `FRAME_MS`, `FRAME_SAMPLES`)
- End-to-end latency measurement anchor (`t_audio_end` on `Lineage`)

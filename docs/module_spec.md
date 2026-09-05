# livesub Module Specification

This document is the reference for anyone **writing a new module** or **reusing an existing module** outside the default pipeline. It covers the contracts every module must satisfy, the data types that flow between them, the bus that connects them, and the configuration system that wires them together.

---

## Table of Contents

1. [Concepts and Vocabulary](#1-concepts-and-vocabulary)
2. [Core Data Types](#2-core-data-types)
3. [The Bus](#3-the-bus)
4. [Module Lifecycle](#4-module-lifecycle)
5. [Module Conventions](#5-module-conventions)
6. [Implementations Reference](#6-implementations-reference)
7. [Fused Stages](#7-fused-stages)
8. [LLM Engines (Internal Helper)](#8-llm-engines-internal-helper)
9. [Registry and Dependency Loading](#9-registry-and-dependency-loading)
10. [Configuration System](#10-configuration-system)
11. [Writing a New Module](#11-writing-a-new-module)
12. [Reusing a Module Outside the Pipeline](#12-reusing-a-module-outside-the-pipeline)

---

## 1. Concepts and Vocabulary

The pipeline is a directed graph of **nodes**. Each node wraps one **module** (an instance of `Module`) and communicates with other nodes exclusively through named **topics** on a shared **Bus**. No module imports or calls another module directly.

```
AudioSource ──audio.*──► Denoiser ──audio.*──► Segmenter ──utterance.*──► Transcriber ──text.*──► Corrector ──text.*──► Translator ──text.*──► Sink(s)
```

All stages are optional and independently composable. The corrector and segmenter can be removed; the translator can be wired directly to raw ASR output.

Key properties:

- **Role is expressed by port declarations, not class hierarchy.** A module that takes `AudioFrame` and emits `Utterance` is a segmenter by behaviour, not by class name.
- **The pipeline is semantics-blind.** It routes by Python type. No module kind field exists. A topic carries whatever type its publishing module's output port declares.
- **Stages are optional.** Removing a node and re-pointing the next node's topic requires no code change.
- **Fan-out is free.** Multiple nodes can subscribe to the same topic independently.
- **Modules are isolated.** A module only imports from `livesub.core`.

---

## 2. Core Data Types

All types live in `livesub.core.types` and are re-exported from `livesub.core`.

### 2.1 Audio Constants

```python
SAMPLE_RATE   = 16_000   # Hz — canonical sample rate everywhere in the system
FRAME_MS      = 20       # ms per AudioFrame
FRAME_SAMPLES = 320      # samples per AudioFrame (SAMPLE_RATE * FRAME_MS // 1000)
```

**The canonical audio format is: 16 kHz, mono, float32 in `[-1.0, 1.0]`.** Every source must produce this format. Every denoiser must consume and return this format.

### 2.2 `AudioFrame`

```python
@dataclass(slots=True)
class AudioFrame:
    pcm:         np.ndarray   # float32, shape (FRAME_SAMPLES,) == (320,)
    sample_rate: int          # always SAMPLE_RATE; carried for introspection
    seq:         int          # monotonically increasing frame counter from the source
    t_capture:   float        # time.time() at the moment audio entered the process
```

- `t_capture` is the anchor for all end-to-end latency measurements. Sources must stamp it as early as possible.
- `pcm.dtype` is always `float32`; `__post_init__` casts automatically.
- `frame.duration` → `len(pcm) / sample_rate` (float seconds).

Published on `audio.*` topics. Default bus mode: **live** (drop-oldest).

### 2.3 `Utterance`

```python
@dataclass(slots=True)
class Utterance:
    id:       str          # stable across partials and final for one speech region
    pcm:      np.ndarray   # float32, arbitrary length (the full speech region so far)
    t_start:  float        # wall clock of first sample
    t_end:    float        # wall clock of last sample
    is_final: bool         # False for an in-progress partial; True when speech ends
```

Produced by segmenter nodes and published on `utterance.*` topics. Consumed by transcriber nodes.

- `is_final=False` partials allow transcribers to emit low-latency partial text while speech is still in progress. The same `id` is reused across all partials and the final for one utterance.
- Published on `utterance.*` topics. Default bus mode: **blocking** (an utterance is a discrete speech event; silent loss is unacceptable).

### 2.4 `Lineage`

```python
@dataclass
class Lineage:
    segment_id:       str                  # stable across all revisions of one subtitle line
    revision:         int = 0              # assigned by the bus; modules must not set this
    t_audio_end:      float | None = None  # wall clock of last audio sample
    stage_latency_ms: dict[str, float] = field(default_factory=dict)
```

Carries provenance through the text stages. **Managed by the bus, not by modules.**

- `revision` is stamped by the bus each time a `TextFrame` is published to a topic, keyed by `(segment_id, topic)`. All subscribers of a call to `bus.publish()` receive the same revision-stamped frame.
- `t_audio_end` is set once by the Transcriber when it builds a `TextFrame` from an `Utterance`. Every downstream module copies the `Lineage` unchanged (or calls `record_latency`, which preserves all fields).
- Modules must never set or increment `revision`. They simply carry the incoming `Lineage` forward.

**Key methods:**
```python
lineage = Lineage.new(segment_id="abc123")         # create fresh lineage
lineage = lineage.record_latency("corrector", 0.12) # returns a new Lineage (immutable)
```

### 2.5 `TextFrame`

```python
@dataclass(slots=True)
class TextFrame:
    text:     str
    lang:     str     = "en"   # BCP-47, e.g. "en", "vi", "zh"
    is_final: bool    = True
    lineage:  Lineage = field(default_factory=Lineage.new)
    meta:     dict[str, Any] = field(default_factory=dict)
```

The only message type on text topics. All stages of processing (raw ASR, corrected, translated) use this same struct. Which stage produced a frame is determined by the **topic it was published to**, not by a field inside the frame.

**Key properties:**
```python
frame.segment_id      # → lineage.segment_id (convenience accessor)
frame.revision        # → lineage.revision   (assigned by bus)
frame.end_to_end_ms   # → ms from last audio sample to now
```

**Display contract (`segment_id` + `revision`):** A sink replaces the displayed line for a `segment_id` when a higher `revision` arrives and discards events with a lower revision than the current one. The topic name (from config) tells the sink what the text represents; it does not need to inspect the frame for a kind field.

### 2.6 Topic Naming Conventions

Topic names are arbitrary strings. The bus assigns no meaning to names; all behaviour follows from the payload type registered against the topic at wiring time. The conventions below are used in the built-in configs for human clarity only.

| Conventional prefix | Payload type | Default bus mode |
|---------------------|-------------|-----------------|
| `audio.*` | `AudioFrame` | live (drop-oldest) |
| `utterance.*` | `Utterance` | blocking |
| `text.*` | `TextFrame` | blocking |

---

## 3. The Bus

`livesub.core.bus.Bus` — topic-based fan-out pub/sub.

### 3.1 Behaviour

- **Every subscriber gets its own queue.** Publishing to a topic with two subscribers delivers independently to both.
- Publishing to a topic with no subscribers is a no-op (legal and cheap).

### 3.2 Subscription Modes

Three modes govern how a subscriber receives items:

| Mode | Behaviour | Default for |
|------|-----------|-------------|
| `"live"` | Fixed-size queue; oldest item dropped when full. Always delivers the latest. | `AudioFrame` topics |
| `"blocking"` | Publisher awaits when queue is full; back-pressures upstream. Nothing is lost. | `Utterance`, `TextFrame` topics |
| `"catchup"` | Shared ring buffer; publisher never blocks. Subscriber reads at its own pace from its current position, catching up when capacity allows. Items overwritten before a slow reader arrives are gone (bounded by ring size, default 512). | opt-in only |

The default mode for a topic is determined by its registered payload type. `AudioFrame` → `"live"`. Everything else → `"blocking"`. Nodes can override with the `mode` config key.

### 3.3 Revision Stamping

Every `TextFrame` published via `bus.publish(topic, frame)` is automatically stamped with the next revision for `(segment_id, topic)` before delivery. All subscribers receive the same revision-stamped frame. Modules must not set `revision` themselves.

### 3.4 Finalisation Tracking

The bus tracks which `(topic, segment_id)` pairs have received a `TextFrame` with `is_final=True`. This powers `skip_if_finalized` on catchup nodes (§10).

### 3.5 API

```python
# Subscribe (called once at startup, before events flow)
sub = bus.subscribe(topic, subscriber="my_node")
sub = bus.subscribe(topic, subscriber="quality_asr", mode="catchup",
                    skip_if_finalized="text.raw")

# Declare intent to publish (allows multiple publishers on one topic)
bus.register_publisher(topic)

# Declare the payload type for a topic (done by the graph; hand-built buses should call this)
bus.register_topic_type(topic, Utterance)

# Publish (awaitable; stamps revision for TextFrame automatically)
await bus.publish(topic, payload)

# Signal end-of-stream for this topic
await bus.close(topic)

# Iterate a subscription
async for item in sub:
    ...

# Introspection
bus.report()         # -> dict with per-topic, per-subscriber traffic stats
bus.total_dropped()  # -> int
bus.topic_type(topic) # -> type | None
```

### 3.6 Queue Sizes

| Mode | Default depth | ~Buffer |
|------|--------------|---------|
| `"live"` | 256 items | ~5 s at 20 ms/frame |
| `"blocking"` | 64 items | many minutes in practice |
| `"catchup"` | 512 items (ring) | configurable |

---

## 4. Module Lifecycle

Every module inherits from `Module`, which provides lifecycle hooks and port declarations:

```python
class Module(ABC):
    inputs:  ClassVar[dict[str, type]] = {}   # {port_name: payload_type}
    outputs: ClassVar[dict[str, type]] = {}   # {port_name: payload_type}

    def __init__(self) -> None:
        self.name: str = "unnamed"  # set by registry at construction

    async def start(self) -> None: ...   # default: no-op
    async def stop(self) -> None: ...    # default: no-op
    def drain(self) -> list[Any]: ...    # default: []

    def describe(self) -> dict[str, Any]:
        return {"module": type(self).__name__, "name": self.name,
                "inputs": ..., "outputs": ...}
```

**Declaration order is load-bearing.** dicts preserve insertion order in Python, and the
config loader relies on it: list-form wiring (`in = ["a", "b"]`) maps topics onto ports
*positionally, in declaration order* (§10.2). Insertion order, not alphabetical order —
write ports in the order data flows through the module.

- `start()` is called before any data flows. Load models, open audio devices, bind sockets.
- `stop()` is called after all data is processed. Release resources, close files, flush buffers. **Sink implementations that previously had a `flush()` method put that logic here.**
- `drain()` is called by the graph runner after the input stream is exhausted, before closing the output topic. Return any buffered payloads still to publish. Segmenters override this to emit the in-progress utterance at end-of-file.
- `describe()` appears in `livesub graph` output and bench reports. Override to expose relevant configuration.

---

## 5. Module Conventions

The graph runner dispatches based on the module's port shape.

### 5.1 Source (`inputs == {}`)

No input subscriptions. Must implement:

```python
async def run(self) -> AsyncIterator[T]:
    """Yield payloads for the output port until the source is exhausted or stop() is called."""
```

### 5.2 Transform (`inputs != {}` and `outputs != {}`)

Reads from subscriptions and writes to topics. Must implement `process`. May be sync or async — the runner detects which and wraps sync methods in `run_in_executor` automatically:

```python
# 1-to-1
def process(self, frame: T) -> U: ...
async def process(self, frame: T) -> U: ...

# 1-to-list (e.g., segmenter: one frame → zero or more utterances)
def process(self, frame: T) -> list[U]: ...

# 1-to-many named outputs (e.g., fused stage)
async def process(self, frame: T) -> dict[str, Any]: ...
# dict is keyed by output port name; the graph routes each value to the declared topic
```

Return value dispatch:

| Return value | Published as |
|---|---|
| `None` | nothing |
| single payload | to the module's single output topic |
| `list` | each item to the single output topic |
| `dict` | each value to the topic mapped for that port name |

### 5.3 Sink (`outputs == {}`)

No output publications. Must implement:

```python
async def process(self, frame: T) -> None: ...
```

Multiple sinks can run simultaneously and subscribe to any combination of topics. Writes to shared state (e.g., terminal output) are serialised by the runner's lock.

### 5.4 Sync vs async

If `process` is a plain `def`, the graph runner calls it via `loop.run_in_executor(None, module.process, frame)`. This keeps the event loop free even for CPU-heavy modules. Sources must always use `async def run` because they are async iterators.

### 5.5 Internal state

Modules that need context (e.g., LLM correctors needing recent finalised lines for discourse disambiguation) maintain it as instance state — a `deque` updated after each final event. Context is **never** a method parameter. The module decides what to remember and when to clear it (typically in `stop()`).

---

## 6. Implementations Reference

All registered implementations. `impl` is the key used in TOML configs.

```
impl                    class                        requirements
────────────────────────────────────────────────────────────────────────────
# Sources
mic                     MicSource                    requirements.txt
ffmpeg                  FfmpegSource                 ffmpeg binary
wav                     WavReplaySource              requirements.txt
stdin                   StdinPcmSource               requirements.txt

# Denoisers  (AudioFrame → AudioFrame)
passthrough_denoiser    PassthroughDenoiser          requirements.txt
highpass_gate           HighpassGateDenoiser         requirements.txt
spectral                SpectralDenoiser             requirements.txt
noisereduce             NoiseReduceDenoiser          requirements.txt (noisereduce)
deepfilternet           DeepFilterNetDenoiser        pip install deepfilternet

# Segmenters  (AudioFrame → Utterance)
energy                  EnergySegmenter              requirements.txt
silero                  SileroSegmenter              requirements.txt (silero-vad / onnx)

# Transcribers  (Utterance → TextFrame)
mock_transcriber        MockTranscriber              requirements.txt
mlx_whisper             MlxWhisperTranscriber        requirements-mlx.txt
faster_whisper          FasterWhisperTranscriber     requirements-cpu.txt

# Correctors  (TextFrame → TextFrame)
passthrough_corrector   PassthroughCorrector         requirements.txt
rules                   RuleCorrector                requirements.txt
mlx_llm_corrector       MlxLlmCorrector              requirements-mlx.txt
ollama_corrector        OllamaLlmCorrector           requirements-cpu.txt
cloud_llm_corrector     CloudLlmCorrector            requirements-cloud.txt

# Translators  (TextFrame → TextFrame)
mock_translator         MockTranslator               requirements.txt
mlx_llm_translator      MlxLlmTranslator             requirements-mlx.txt
ollama_translator       OllamaLlmTranslator          requirements-cpu.txt
cloud_llm_translator    CloudLlmTranslator           requirements-cloud.txt

# Fused (TextFrame → {corrected: TextFrame, translated: TextFrame})
fused_llm               FusedLlmStage                requirements-mlx.txt
fused_ollama            OllamaFusedStage             requirements-cpu.txt

# Sinks
collect                 CollectSink                  requirements.txt
stdout_pretty           PrettyStdoutSink             requirements.txt
jsonl                   JsonlSink                    requirements.txt
websocket_server        WebSocketSink                requirements.txt
```

### Port declarations for each kind

| Kind | `inputs` | `outputs` |
|------|----------|-----------|
| Source | `{}` | `{"audio": AudioFrame}` |
| Denoiser | `{"audio": AudioFrame}` | `{"audio": AudioFrame}` |
| Segmenter | `{"audio": AudioFrame}` | `{"utterance": Utterance}` |
| Transcriber | `{"audio": Utterance}` | `{"text": TextFrame}` |
| Corrector | `{"text_in": TextFrame}` | `{"text_out": TextFrame}` |
| Translator | `{"text_in": TextFrame}` | `{"text_out": TextFrame}` (+ `"corrected"` on repair-mode translators: mlx/cloud/mock) |
| FusedLlmStage | `{"text_in": TextFrame}` | `{"corrected": TextFrame, "translated": TextFrame}` |
| Sink | `{"text": TextFrame}` | `{}` |

### Constructor options (common)

| Impl | Option | Type | Default | Description |
|------|--------|------|---------|-------------|
| `mic` | `device` | str/int | `None` | Device name or index |
| `mic` | `sample_rate` | int | `16000` | Source device rate; resampled if ≠ 16k |
| `ffmpeg` | `path` | str | — | File path, URL, or device |
| `ffmpeg` | `realtime` | bool | `True` | Pace at wall-clock speed |
| `wav` | `path` | str | — | WAV file path |
| `wav` | `realtime` | bool | `False` | Drift-free sleep between frames |
| `wav` | `loop` | bool | `False` | Restart when exhausted |
| `energy` | `silence_ms` | float | `220` | Trailing silence to end utterance |
| `energy` | `min_speech_ms` | float | `400` | Minimum speech to start utterance |
| `energy` | `max_utterance_ms` | float | `6000` | Hard cap; splits at quiet point |
| `energy` | `partial_every_ms` | float | `900` | Emit partial this often |
| `faster_whisper` | `model` | str | `"small.en"` | Model size |
| `faster_whisper` | `device` | str | `"auto"` | `"cpu"`, `"cuda"`, `"auto"` |
| `faster_whisper` | `compute_type` | str | `"int8"` | Quantisation type |
| `mlx_whisper` | `model` | str | `"small.en"` | mlx-whisper model name |
| `ollama_corrector` | `model` | str | — | Ollama model name |
| `ollama_corrector` | `host` | str | `"http://localhost:11434"` | Server URL |
| `ollama_corrector` | `threshold` | float | `0.4` | Min Jaccard similarity |
| `ollama_translator` | `target` | str | — | BCP-47 target language (required) |
| `fused_ollama` | `target` | str | — | BCP-47 target language (required) |
| `fused_ollama` | `model` | str | — | Ollama model name |
| `fused_ollama` | `host` | str | `"http://localhost:11434"` | Server URL |
| `stdout_pretty` | `keep` | int | `4` | Recent blocks shown |
| `jsonl` | `path` | str | stdout | Output file |
| `websocket_server` | `host` | str | `"localhost"` | Bind address |
| `websocket_server` | `port` | int | `8765` | Bind port |
| `websocket_server` | `replay` | int | `20` | Events replayed to late-joining clients |

---

## 7. Fused Stages

`livesub.fused.llm_correct_translate.FusedLlmStage` (MLX, `fused_llm`) and
`livesub.fused.ollama_llm.OllamaFusedStage` (Ollama, `fused_ollama`) each make a single
LLM call and return both a corrected English frame and a translated frame — halving
latency vs two separate stages.

**Port declaration:**
```python
inputs  = {"text_in": TextFrame}
outputs = {"corrected": TextFrame, "translated": TextFrame}
```

**`process()` return:**
```python
async def process(self, frame: TextFrame) -> dict[str, TextFrame]:
    return {"corrected": ..., "translated": ...}
```

**How to wire it:**
```toml
[[node]]
name   = "fix"
impl   = "fused_llm"
in     = "text.raw"
[node.out]
corrected  = "text.corrected"
translated = "text.out"
model      = "mlx-community/Qwen2.5-7B-Instruct-4bit"
target     = "vi"
```

---

## 8. LLM Engines (Internal Helper)

All LLM-based correctors and translators share engine singletons from `livesub.llm`. Engines are not pipeline stages.

### 8.1 Shared Interface

```python
async def chat_json(
    self,
    system: str,
    user: str,
    required_keys: list[str],
) -> dict | None:
    """Send a chat prompt and parse the JSON response.
    Returns the parsed dict, or None if parsing fails or the server is down."""
```

### 8.2 Available Engines

| Class | Backend | Singleton factory | Extra deps |
|-------|---------|-------------------|-----------|
| `MlxEngine` | mlx-lm (Metal) | `get_engine(model)` | `requirements-mlx.txt` |
| `OllamaEngine` | Ollama server | `get_ollama_engine(model, host)` | `requirements-cpu.txt` |
| `CloudEngine` | Anthropic/OpenAI | `get_cloud_engine(provider, model)` | `requirements-cloud.txt` |

### 8.3 Prompts

`livesub.llm.prompts` provides:

| Constant/function | Used by |
|---|---|
| `CORRECT_SYSTEM` / `correct_user(text, ctx)` | correctors |
| `TRANSLATE_SYSTEM` / `translate_user(text, ctx, target)` | translators |
| `REPAIR_TRANSLATE_SYSTEM` / `fused_user(text, ctx, target)` | fused stages; repair-mode translators (mlx/cloud) |
| `FUSED_SYSTEM` / `fused_user(text, ctx, target)` | fused stage |
| `language_name(code)` | any |

---

## 9. Registry and Dependency Loading

`livesub.core.registry` maps `impl_name` → class, resolved lazily.

```python
from livesub.core.registry import build, resolve, available

# Instantiate a stage (preferred)
stage = build("spectral", name="nr", chunk_ms=40)

# Just get the class
cls = resolve("faster_whisper")

# List all registered impl names
names = available()  # -> ["cloud_llm_corrector", "energy", "faster_whisper", ...]
```

- `build()` constructs the instance and sets `.name`.
- `resolve()` raises `UnknownImplementation` if the impl name is not in the registry.
- `resolve()` raises `MissingDependency` with an actionable install hint if the module cannot be imported.

**To register a new implementation** (see §11), add one entry to `REGISTRY` in `livesub/core/registry.py`.

---

## 10. Configuration System

The config is a TOML file containing `[[node]]` tables. No Python code change is needed to rewire the graph.

### 10.1 Node fields

| Key | Required | Description |
|-----|----------|-------------|
| `name` | no | Human label; defaults to `{impl}{index}` |
| `impl` | yes | Registry impl name (e.g. `faster_whisper`, `energy`) |
| `in` | depends | Topic(s) to subscribe to (see §10.2) |
| `out` | depends | Topic(s) to publish to (see §10.2) |
| `mode` | no | Subscription mode: `"default"`, `"live"`, `"blocking"`, `"catchup"` |
| `skip_if_finalized` | no | Topic to watch; skip utterances already finalised there (catchup only) |
| `enabled` | no | `false` to disable without removing the node; default `true` |
| *(other)* | — | Passed directly to the implementation's `__init__` as kwargs |

There is no `module` key. The impl name alone identifies the class and its port declarations.

### 10.2 Wiring `in` and `out`

Both `in` and `out` accept three forms:

**Single string** — wired to the module's sole declared port (error if it declares none or several):
```toml
in  = "utterance.speech"
out = "text.raw"
```

**List** — topics mapped **positionally** onto the declared ports, in the order the
module declared them (see §4). Reordering the list re-wires the node; there is no name
matching in this form. When the module has exactly one input port, any number of topics
creates a fan-in (all topics subscribe to that one port):
```toml
in = ["text.raw", "text.corrected", "text.out"]   # fan-in: all to one port
```
For `out` the list must contain exactly one topic per declared output port — outputs
never fan-out implicitly. A `"_"` entry skips that port: it is left unwired and
publishes nothing, and no bus topic is created for it. No node may subscribe to a topic
that only a skipped port produced — validation rejects it as unpublished:
```toml
out = ["_", "text.out"]   # skip the first declared port; wire the second
```

**Table** — explicit `port_name = "topic"` mapping, validated against the module's declarations:
```toml
[node.in]
text_in = "text.raw"

[node.out]
corrected  = "text.corrected"
translated = "text.out"
```

Port names are scoped to the module. Topic names carry no intrinsic meaning; all routing follows from the payload type registered by the producing module's output port.

### 10.3 Live + catchup nodes

```toml
[[node]]
name  = "asr_live"
impl  = "faster_whisper"
in    = "utterance.speech"
mode  = "live"
model = "small.en"
out   = "text.raw"

[[node]]
name               = "asr_quality"
impl               = "faster_whisper"
in                 = "utterance.speech"
mode               = "catchup"
model              = "large.en"
skip_if_finalized  = "text.raw"
out                = "text.raw"
```

Both publish to `text.raw`. The live node emits quickly at lower quality. The quality node catches up when capacity allows and emits higher-revision frames for the same `segment_id`s. The sink display contract (replace on higher revision) handles progressive replacement automatically. `skip_if_finalized` prevents the quality node from re-transcribing utterances the live node already finalised.

### 10.4 Example complete config

```toml
[[node]]
name = "mic"
impl = "mic"
out  = "audio.raw"

[[node]]
name = "vad"
impl = "energy"
in   = "audio.raw"
out  = "utterance.speech"

[[node]]
name  = "asr"
impl  = "faster_whisper"
in    = "utterance.speech"
out   = "text.raw"
model = "small.en"

[[node]]
name  = "fix"
impl  = "ollama_corrector"
in    = "text.raw"
out   = "text.corrected"
model = "qwen3.5:4b"

[[node]]
name   = "vi"
impl   = "ollama_translator"
in     = "text.corrected"
out    = { text_out = "text.out" }
target = "vi"
model  = "qwen3.5:4b"

[[node]]
name = "screen"
impl = "stdout_pretty"
in   = ["text.raw", "text.corrected", "text.out"]
```

### 10.5 `chain_config` shorthand (Python API)

```python
from livesub.core.config import chain_config

cfg = chain_config(
    stages=["mic", "segment", "asr", "correct", "translate"],
    overrides={"asr": "mlx_whisper"},
    sinks=["stdout_pretty", "jsonl"],
)
```

Omitting `"segment"` and `"correct"` (e.g. `["mic", "asr", "translate"]`) wires the
translator directly to `text.raw` — pass a fused implementation for it
(`--translate fused_ollama`) so ASR errors are repaired in the same call.

Known chain stage names: `mic`, `ffmpeg`, `wav`, `segment`, `denoise`, `asr`, `correct`, `translate`.

### 10.6 Validation and diagram

```python
from livesub.core.graph import validate, mermaid
validate(cfg)       # raises GraphError on: duplicate names, missing topics, type mismatches, cycles
print(mermaid(cfg)) # Mermaid flowchart LR string
```

**Validation rules:**
1. No duplicate node names.
2. At least one source node (a module with `inputs == {}`).
3. Every declared output port in config must match the module's `outputs` declaration.
4. Every topic read by a subscriber must be published by someone.
5. A topic's payload type (from its publisher's output port) must match the subscriber's input port type. **No prefix conventions** — a topic named `audio.foo` carrying `Utterance` is valid.
6. No cycles.

---

## 11. Writing a New Module

### Step 1 — Subclass `Module` and declare ports

```python
# livesub/denoise/my_denoiser.py
from livesub.core import Module, AudioFrame

class MyDenoiser(Module):
    inputs  = {"audio": AudioFrame}
    outputs = {"audio": AudioFrame}

    def __init__(self, strength: float = 0.5):
        super().__init__()
        self.strength = strength

    async def start(self) -> None:
        pass  # load model, open device, etc.

    async def stop(self) -> None:
        pass  # release resources

    def process(self, frame: AudioFrame) -> AudioFrame:
        cleaned = _my_algorithm(frame.pcm, self.strength)
        return AudioFrame(cleaned, frame.sample_rate, frame.seq, frame.t_capture)

    def describe(self) -> dict:
        return {**super().describe(), "strength": self.strength}
```

### Step 2 — Register it

Add one line to `REGISTRY` in `livesub/core/registry.py`:

```python
REGISTRY: dict[str, str] = {
    ...
    "my_denoiser": "livesub.denoise.my_denoiser:MyDenoiser",
}
```

Add an install hint if optional extras are needed:

```python
_INSTALL_HINTS = {
    ...
    "my_denoiser": "requirements-extra.txt",
}
```

### Step 3 — Use it in a config

```toml
[[node]]
name     = "nr"
impl     = "my_denoiser"
in       = "audio.raw"
out      = "audio.clean"
strength = 0.8
```

### Checklist by module shape

| Shape | `inputs` | `outputs` | Required method | Key contracts |
|-------|----------|-----------|-----------------|---------------|
| Source | `{}` | `{port: T}` | `async def run() -> AsyncIterator[T]` | Stamp `t_capture` early on `AudioFrame`; call `stop()` cleanly |
| Denoiser | `{"audio": AudioFrame}` | `{"audio": AudioFrame}` | `def process(frame) -> AudioFrame` | Preserve `seq`, `t_capture`; same `len(pcm)` |
| Segmenter | `{"audio": AudioFrame}` | `{"utterance": Utterance}` | `def process(frame) -> list[Utterance]` | Override `drain()` to flush end-of-file; use stable `id` across partials |
| Transcriber | `{"audio": Utterance}` | `{"text": TextFrame}` | `async def process(utt) -> list[TextFrame]` | Create `Lineage.new(segment_id=utt.id)`, set `t_audio_end=utt.t_end` |
| Corrector | `{"text_in": TextFrame}` | `{"text_out": TextFrame}` | `async def process(frame) -> TextFrame` | Carry `lineage` forward; use similarity guard for LLM rewrites; return input on failure |
| Translator | `{"text_in": TextFrame}` | `{"text_out": TextFrame}` | `async def process(frame) -> TextFrame \| dict` | Dual output: use a fused stage (`fused_llm` / `fused_ollama`), or `repair_mode` on the mlx/cloud/mock translators |
| Sink | `{"text": TextFrame}` | `{}` | `async def process(frame) -> None` | Implement `segment_id + revision` replace contract; flush in `stop()` |

**Note:** Modules must **never** set `frame.lineage.revision`. Carry the incoming `Lineage` into output frames unchanged, or call `lineage.record_latency(name, seconds)` which returns a new `Lineage` with the same `revision`.

---

## 12. Reusing a Module Outside the Pipeline

Modules are plain Python objects. You can instantiate and call them without a bus or graph.

### Denoiser — offline file processing

```python
import asyncio
import numpy as np
from livesub.core.registry import build
from livesub.core.audioutil import load_wav, save_wav

async def denoise_file(in_path: str, out_path: str) -> None:
    denoiser = build("spectral")
    await denoiser.start()
    pcm, sr = load_wav(in_path)
    cleaned = denoiser.process_array(pcm, sr)
    save_wav(out_path, cleaned, sr)
    await denoiser.stop()

asyncio.run(denoise_file("noisy.wav", "clean.wav"))
```

### Transcriber — one-shot transcription

```python
from livesub.core.registry import build

async def transcribe_file(path: str) -> list[str]:
    import soundfile as sf
    pcm, sr = sf.read(path, dtype="float32", always_2d=False)
    asr = build("faster_whisper", name="asr", model="small.en")
    await asr.start()
    events = asr.transcribe_array(pcm, sr)   # utility method; uses internal segmenter
    await asr.stop()
    return [e.text for e in events if e.is_final]
```

### Running a minimal pipeline in code

```python
import asyncio
from livesub.core.config import chain_config
from livesub.core.graph import run_config

async def main():
    cfg = chain_config(
        stages=["wav", "segment", "asr", "translate"],
        overrides={"asr": "mock_transcriber", "translate": "mock_translator"},
        sinks=["collect"],
    )
    cfg.node("wav").options["path"] = "assets/sample.wav"
    await run_config(cfg, timeout=30)

asyncio.run(main())
```

### `CollectSink` — programmatic result collection

```python
sink.events            # list[TextFrame] — all events in arrival order
sink.latest            # dict[(segment_id, topic)] -> TextFrame — current best per segment
sink.finals(lang)      # list[TextFrame] filtered to final events of a given lang
sink.text()            # str — all final events joined by newline
```

---

*Last updated: reflects the unified interface design (Module ABC, TextFrame, Lineage, Utterance as bus type, port-based wiring, ring buffer catchup mode).*

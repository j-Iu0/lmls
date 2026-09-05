# Pipeline Module Specification

This document specifies what a **module** is in this framework: the contract every
module must satisfy, how it receives input and produces output, what the pipeline
expects from it across its lifetime, what it may assume about stream processing, and
what it is forbidden to do. It ends with worked examples and the registration/wiring
reference needed to put a module into service.

The framework is a general-purpose stream-processing engine. The audio/captioning
pipeline shipped with the package is one application of it, not the framework itself.
The built-in data types (§7) belong to that application; a module author is free to
define their own payload types for anything else.

The built-in implementations (sources, denoisers, transcribers, sinks, …) are listed
in the README. They are instances of the contract below, not part of it.

---

## Table of Contents

1. [Pipeline Structure](#1-pipeline-structure)
2. [Module Interface: Ports and Roles](#2-module-interface-ports-and-roles)
3. [Module Lifecycle](#3-module-lifecycle)
4. [Stream Processing Fundamentals](#4-stream-processing-fundamentals)
5. [Module Constraints](#5-module-constraints)
6. [The Bus (Background)](#6-the-bus-background)
7. [Built-in Data Types](#7-built-in-data-types)
8. [Registry and Wiring](#8-registry-and-wiring)
9. [Worked Examples](#9-worked-examples)

---

## 1. Pipeline Structure

A pipeline is a **directed graph of nodes**. Each node wraps exactly one module — an
instance of `livesub.core.Module` — and communicates with other nodes exclusively
through named **topics** on a shared **Bus** (§6).

```
Source ──topic──► Transform ──topic──► Transform ──topic──► Sink(s)
```

The shape of the graph is arbitrary: linear chains, fan-out to parallel consumers,
fan-in merging several topics into one module, optional stages removed and re-pointed
in configuration without code changes.

Key properties:

- **Role is expressed by port declarations, not class hierarchy.** A module that
  takes `T` and emits `U` is a transform by behaviour, not by class name. There is no
  per-role base class (§2).
- **Routing is semantics-blind.** The graph routes by Python type. A topic carries
  whatever type its publishing module's output port declares; the topic name carries
  no meaning to the framework.
- **Modules are isolated.** No module imports or calls another module *as part of the
  pipeline*. A module may import other modules as libraries (§5); the pipeline itself
  never does. The graph instantiates every module through the registry (§8), never by
  direct import.
- **Modules never see the bus.** The graph runner performs all subscriptions and all
  publishes. A module only implements `run()`/`process()` and returns values; the
  runner delivers them (§2). Modules are not given a `Bus` reference.
- **Stages are optional.** Removing a node and re-pointing the next node's input is a
  config change, not a code change (§8.3).
- **Fan-out is free.** Multiple nodes can subscribe to the same topic; each gets an
  independent queue (§6).

What the framework does **not** do: it imposes no domain. A payload can be an audio
frame, a stock tick, a network packet, a log line, or any Python object. The
built-in captioning pipeline (audio → speech regions → text → refined text →
display) is one configuration of this machinery.

---

## 2. Module Interface: Ports and Roles

Every module subclasses `livesub.core.Module` and declares two class variables:

```python
class Module(ABC):
    inputs:  ClassVar[dict[str, type]] = {}   # {port_name: payload_type}
    outputs: ClassVar[dict[str, type]] = {}   # {port_name: payload_type}
```

A **port** is a named attachment point. At wiring time, each declared input port is
bound to a topic to subscribe to, and each declared output port to a topic to publish
to. The payload type declared on a port is a contract: the graph validates that a
topic's type matches every port connected to it (§8.6).

**Declaration order is load-bearing.** Dicts preserve insertion order, and the config
loader relies on it: list-form wiring maps topics onto ports *positionally, in
declaration order* (§8.3). Declare ports in the order data flows through the module.

### 2.1 The three shapes

The graph runner dispatches on port shape. There is no other role signal — no base
class to pick, no kind field to set.

| Shape | `inputs` | `outputs` | Required method |
|---|---|---|---|
| **Source** | `{}` | one port `{port: T}` | `async def run(self) -> AsyncIterator[T]` |
| **Transform** | non-empty | non-empty | `process(self, ...)` (variants below) |
| **Sink** | non-empty | `{}` | `process(self, frame) -> None` |

Notes:

- A source has exactly **one** output port. The runner publishes every yielded item
  to that port's topic.
- `__init__` must call `super().__init__()`; the registry sets `self.name` after
  construction (§8.1).

### 2.2 Sources

A source is an async iterator. It yields payloads as they become available:

```python
async def run(self) -> AsyncIterator[T]:
    """Yield payloads for the output port until exhausted or stopped."""
```

The runner iterates `run()` and publishes each item. When the iterator is exhausted —
or the graph shuts down and cancels the task — the runner closes the output topic.
Yield promptly; do not batch unless your application requires it.

### 2.3 Transforms and return-value dispatch

A transform reads payloads from its subscriptions and returns new payloads. `process`
may be sync or async. The return value determines publication:

| Return value | Published as |
|---|---|
| `None` | nothing |
| single payload | to the module's single output topic |
| `list` | each item to the single output topic |
| `dict` | each value to the topic mapped for that port name |

The four signatures:

```python
# 1-to-1
def process(self, frame: T) -> U: ...
async def process(self, frame: T) -> U: ...

# 1-to-list (buffer-and-emit; e.g. accumulate small items into larger ones)
def process(self, frame: T) -> list[U]: ...

# 1-to-many named outputs (module declares several output ports)
async def process(self, frame: T) -> dict[str, Any]:
    # dict keys are output *port names*; the runner routes each value
    # to the topic bound to that port
```

A multi-port module **must** return a dict. A bare payload from a multi-port module
would be published to the first declared output topic — do not rely on that.

### 2.4 Sync vs async

`process` may be a plain `def` or an `async def`. The runner detects which and calls
sync methods via `loop.run_in_executor`, keeping the event loop free for CPU-heavy
modules (§5, constraint 2). Sources must always be `async def run`, because they are
async iterators. Sinks may be sync or async.

### 2.5 Payload types

The framework requires only that the type a port declares matches the type the
connected topic carries. The built-in types (§7) exist for the audio/text pipeline;
for other domains, define your own dataclasses and declare them on ports. Any Python
type works. There is no registration step for payload types and no base class for
them.

---

## 3. Module Lifecycle

The runner drives every module through the same sequence:

| Order | Hook | Signature | Purpose |
|---|---|---|---|
| 1 | Construction | `__init__(self, **options)` | Store configuration. Do **not** open resources here — wiring may still fail and the node may be discarded. |
| 2 | `start()` | `async def start(self) -> None` | Called once before any data flows. Load models, open files/sockets/devices, allocate buffers. |
| 3 | Data flow | `run()` / `process()` | Called repeatedly with payloads (§2). |
| 4 | `drain()` | `def drain(self) -> list[Any]` | Transforms only: called once after all inputs are exhausted, before the runner closes the module's output topics. Return any buffered payloads still to publish. |
| 5 | `stop()` | `async def stop(self) -> None` | Called once during shutdown (only if the node was started). Release everything acquired in `start()`, flush output buffers. |

Defaults: `start` and `stop` are no-ops; `drain` returns `[]`.

Contract details:

- **`stop()` is the flush point.** Sinks that buffer output (or previously had a
  `flush()` method) put that logic here. `stop()` is called even when shutdown was
  triggered by cancellation or timeout — but only for nodes whose `start()` ran.
- **`drain()` is the end-of-stream flush for transforms.** A module that accumulates
  items internally (e.g. holds a partial window waiting for a boundary that never
  comes) must override `drain()` to emit what it is holding. After `drain()` results
  are published, the runner closes the module's output topics.
- **Shutdown is cooperative cancellation.** The runner cancels each node's task and
  then calls `stop()`. Modules must let `asyncio.CancelledError` propagate (§5,
  constraint 3) and must not assume `process()` completes the item it was working on.
- `describe(self) -> dict` returns a dict shown in graph diagrams and bench reports.
  The base implementation reports module class, name, and port declarations. Override
  to expose configuration worth seeing in diagnostics.

---

## 4. Stream Processing Fundamentals

For readers new to live/streaming data. These are the mental-model facts that explain
why the interface looks the way it does.

### 4.1 What a stream is

A stream is an **unbounded sequence of discrete items** that arrives over time. Unlike
a file, there is no end you can see from the start, and you cannot seek backward. A
module sees item 1, handles it, then sees item 2. It never gets "the whole input" and
must not need it.

### 4.2 The finest unit of processing

The finest unit is **one item on one topic**. Each call to `process()` handles exactly
one item, and each `yield` from a source produces exactly one item. What "one item"
means is chosen by the producing module, not by the framework: in the audio pipeline
the unit is a fixed 20 ms frame; a log tailer might emit one line; a network source
might emit one packet. Downstream modules see whatever granularity upstream chose.
If your module needs larger or smaller units, it re-chunks internally (buffer several
items, or split one item into several outputs).

### 4.3 Partial and final results

A common streaming pattern is **progressive refinement**: publish a preliminary result
early (a *partial*), then publish improved versions, ending with a *final*. The
framework has no opinion on how this is signalled — it is a convention of the payload
type. In the built-in types, `Utterance.is_final` and `TextFrame.is_final` mark it,
and the bus assigns each published `TextFrame` a monotonically increasing `revision`
per segment (§6.3, §7.4). Consumers that render results should implement the
replace-on-higher-revision contract; producers of progressive results should reuse a
stable identifier across partials and the final so consumers can correlate them.

Under fan-out, multiple producers may emit revisions for the same segment
interleaved. Assume nothing about global ordering beyond: a single subscriber sees its
topic's items in publish order.

### 4.4 Backpressure and loss

When a consumer is slower than its producer, one of two things must happen, and the
topic's subscription mode decides which (§6.2):

- **blocking** — the publisher waits until the consumer catches up. Nothing is lost;
  the delay propagates upstream. Used for items where silent loss is unacceptable.
- **live** — the oldest queued item is dropped. The consumer always sees the latest
  data. Used for high-rate streams where staleness is worse than loss.

A module author does not choose the mode — the graph wiring does (§8.3) — but must
design for both: a `process()` may be called less often than items are produced
(items dropped), or the whole pipeline may slow down to its pace (backpressure).

### 4.5 State lives in the module

Context that spans items — a rolling window, a noise-floor estimate, recent results
for context — is **instance state**: a `deque`, a counter, a model handle on `self`.
It is never a method parameter; the runner passes only the payload. The module decides
what to remember, when to update it, and when to clear it (typically in `stop()`).
With fan-in, one `process()` call may arrive per subscription concurrently — state a
module reads/writes from async `process()` must account for that, or the module should
keep per-subscription state.

### 4.6 Buffering and end-of-stream

A module may hold items back (accumulate until a boundary, batch for efficiency).
Anything still held when the input stream ends must be returned from `drain()` (§3),
because the runner stops reading input and closes output topics immediately after.
Buffered data not returned in `drain()` is lost.

### 4.7 Errors

An exception escaping `process()` (or `run()`) is logged and re-raised by the runner;
it tears down that node and, with it, the whole run. A module that can recover — an
LLM call timing out, a malformed item — should handle the error itself and return a
neutral result (e.g. the input unchanged) rather than raise. Fail loudly at `start()`
(unreachable server, bad model path), not mid-stream.

---

## 5. Module Constraints

This section lists what a module is **forbidden** to do. Everything not listed is
implicitly allowed: modules may open sockets, run subprocesses, spawn threads, read
and write files, use asyncio primitives, and import any package — including other
modules in this package, as libraries.

1. **Never set `frame.lineage.revision`.**
   The bus stamps `revision` at publish time, keyed by `(segment_id, topic)`, and
   every subscriber receives the same value. Any value a module writes is overwritten
   on publish, so setting it accomplishes nothing and creates the false impression
   that a module can order or deduplicate revisions. Carry the incoming `Lineage`
   forward unchanged, or call `lineage.record_latency(name, seconds)`, which returns a
   new `Lineage` with the same `revision` (§7.4).

2. **Never block the event loop inside `async def process()`.**
   All modules run on one shared asyncio event loop; a blocked coroutine stalls every
   other module in the pipeline. CPU-bound or blocking-IO work in an async method must
   be delegated: `await asyncio.to_thread(...)`. A plain `def process` needs no care —
   the runner automatically calls it in an executor (§2.4).

3. **Never swallow `asyncio.CancelledError` without re-raising.**
   Shutdown works by cancelling each node's task; the runner then awaits the task and
   calls `stop()`. A module that catches `CancelledError` (e.g. in a broad
   `except Exception`) and does not re-raise prevents its own termination and hangs
   the teardown. Re-raise it, and clean up in `stop()`.

4. **Never read from stdin or write to stdout except in a module that owns that
   channel.**
   The pipeline has a Unix-pipe mode in which stdin carries the raw input stream into
   a source module and stdout carries the output stream out of a sink module. A second
   reader starves the owning source; a stray write from any other module corrupts the
   byte stream. In interactive use, stdout is claimed by display sinks. A module that
   prints debug output should use `logging` instead — stderr in normal runs, and it
   never interleaves with data channels.

A closing note on the bus: modules cannot subscribe or publish directly because they
are never given a `Bus` reference — the runner owns all bus interaction. If you find
yourself wanting bus access from inside a module, the intended answer is an extra
output port, not a bus handle.

---

## 6. The Bus (Background)

Module authors never call the bus; the graph runner does. This section is background:
what happens to a payload between one module's return and the next module's
`process()`.

### 6.1 Delivery model

- Every subscriber gets its **own queue**. Publishing to a topic with two subscribers
  delivers independently to both; a slow one does not delay the other.
- Publishing to a topic with **no subscribers is a no-op** — legal and cheap.
- `await bus.publish(topic, payload)` is awaited by the runner; in blocking mode it
  may wait for queue space (that wait *is* backpressure).

### 6.2 Subscription modes

| Mode | Behaviour | Queue | Default for |
|---|---|---|---|
| `"live"` | Fixed-size queue; **oldest item dropped** when full. Always delivers the latest. | 256 items | topics carrying `AudioFrame` |
| `"blocking"` | Publisher **awaits** when the queue is full; back-pressures upstream. Nothing is lost. | 64 items | everything else |
| `"catchup"` | Shared ring buffer; publisher never blocks. Subscriber reads at its own pace from its position, catching up when capacity allows. Items overwritten before a slow reader arrives are gone. | 512 (ring) | opt-in via config |

The default mode for a topic is determined by its registered payload type
(`AudioFrame` → `"live"`, anything else → `"blocking"`); wiring can override it per
node with the `mode` config key (§8.3).

### 6.3 Revision stamping and finalisation

For `TextFrame` payloads the bus does two things at publish time:

- **Revision stamping**: assigns the next revision for `(segment_id, topic)` and
  stamps it into the frame before delivery. All subscribers of that one `publish()`
  call receive the same revision. Modules never set `revision` (§5, constraint 1).
- **Finalisation tracking**: records which `(topic, segment_id)` pairs have received
  a frame with `is_final=True`. This powers the `skip_if_finalized` wiring option
  (§8.3), which lets a slow catch-up node skip items that a faster node already
  finalised.

---

## 7. Built-in Data Types

The framework ships one family of payload types for its audio/captioning application.
They live in `livesub.core.types` and are re-exported from `livesub.core`. They are
**one application, not the framework**: for other domains, define your own types
(§2.5) and ignore this section.

All routing in the built-in pipeline follows from the Python types below — never from
topic names. The conventional prefixes are for human readability only.

| Conventional prefix | Payload type | Default bus mode |
|---------------------|-------------|-----------------|
| `audio.*` | `AudioFrame` | live (drop-oldest) |
| `utterance.*` | `Utterance` | blocking |
| `text.*` | `TextFrame` | blocking |

### 7.1 Audio constants

```python
SAMPLE_RATE   = 16_000   # Hz — canonical sample rate everywhere in the system
FRAME_MS      = 20       # ms per AudioFrame
FRAME_SAMPLES = 320      # samples per AudioFrame (SAMPLE_RATE * FRAME_MS // 1000)
```

The canonical audio format is **16 kHz, mono, float32 in `[-1.0, 1.0]`**. Every
source must produce it; every audio-stage module must consume and return it.

### 7.2 `AudioFrame`

```python
@dataclass(slots=True)
class AudioFrame:
    pcm:         np.ndarray   # float32, shape (FRAME_SAMPLES,) == (320,)
    sample_rate: int          # always SAMPLE_RATE; carried for introspection
    seq:         int          # monotonically increasing frame counter from the source
    t_capture:   float        # time.time() at the moment audio entered the process
```

- `t_capture` is the anchor for all end-to-end latency measurements. Sources must
  stamp it as early as possible.
- `pcm.dtype` is always `float32`; `__post_init__` casts automatically.
- `frame.duration` → `len(pcm) / sample_rate` (float seconds).
- Denoisers must preserve `seq`, `t_capture`, and `len(pcm)`.

### 7.3 `Utterance`

```python
@dataclass(slots=True)
class Utterance:
    id:       str          # stable across partials and final for one speech region
    pcm:      np.ndarray   # float32, arbitrary length (the full speech region so far)
    t_start:  float        # wall clock of first sample
    t_end:    float        # wall clock of last sample
    is_final: bool         # False for an in-progress partial; True when speech ends
```

A speech region cut out of the frame stream by a segmenter. `is_final=False`
partials let transcribers emit low-latency partial text while speech is still in
progress; the same `id` is reused across all partials and the final of one region.

### 7.4 `Lineage` and `TextFrame`

```python
@dataclass
class Lineage:
    segment_id:       str                  # stable across all revisions of one line
    revision:         int = 0              # assigned by the bus; modules must not set this
    t_audio_end:      float | None = None  # wall clock of last audio sample
    stage_latency_ms: dict[str, float] = field(default_factory=dict)
```

```python
@dataclass(slots=True)
class TextFrame:
    text:     str
    lang:     str     = "en"   # BCP-47, e.g. "en", "vi", "zh"
    is_final: bool    = True
    lineage:  Lineage = field(default_factory=Lineage.new)
    meta:     dict[str, Any] = field(default_factory=dict)
```

`TextFrame` is the only message type on text topics; raw, corrected, and translated
stages all use it. Which stage produced a frame is determined by the **topic it was
published to**, not by any field inside the frame.

Lineage rules:

- The **bus** stamps `revision` per `(segment_id, topic)`; modules never set it (§5).
- `t_audio_end` is set once by the transcriber when it builds a `TextFrame` from an
  `Utterance`. Every downstream module copies the `Lineage` unchanged.
- The runner records each stage's processing latency into the `Lineage` of published
  `TextFrame`s automatically. A module may add its own entries with
  `lineage.record_latency(name, seconds)`, which returns a new immutable `Lineage`.

Convenience accessors on `TextFrame`: `frame.segment_id`, `frame.revision`,
`frame.end_to_end_ms`.

**Display contract (`segment_id` + `revision`):** a sink replaces the displayed line
for a `segment_id` when a higher `revision` arrives and discards events with a lower
revision than the one currently shown. Topic names (from config) tell the sink what
the text represents; the frame carries no kind field.

---

## 8. Registry and Wiring

### 8.1 Registry

`livesub.core.registry` maps `impl` names to classes, resolved lazily — importing
`livesub` loads none of the implementations until one is actually built.

```python
from livesub.core.registry import build, resolve, available

stage = build("spectral", name="nr", chunk_ms=40)  # instantiate; sets .name
cls   = resolve("faster_whisper")                  # just the class
names = available()                                # all registered impl names
```

To register an implementation, add one entry to `REGISTRY` in
`livesub/core/registry.py`:

```python
REGISTRY: dict[str, str] = {
    ...
    "my_denoiser": "livesub.denoise.my_denoiser:MyDenoiser",
}
```

Values are dotted paths `"package.module:ClassName"`; the class can live anywhere
importable, including outside this package. If an implementation needs optional
extras, add a hint so failures are actionable:

```python
_INSTALL_HINTS = {
    ...
    "my_denoiser": "requirements-extra.txt",
}
```

Error behaviour: `resolve()` raises `UnknownImplementation` for a name not in the
registry and `MissingDependency` (with the install hint) when the module's imports
fail. `build(impl, name=..., **kwargs)` constructs the instance, sets `.name`, and
forwards `**kwargs` to `__init__`.

### 8.2 Node config

Wiring is declared in TOML `[[node]]` tables; no Python code change is needed to
rewire the graph. There is no `module` key — the impl name alone identifies the class
and its port declarations.

| Key | Required | Description |
|-----|----------|-------------|
| `name` | no | Human label; defaults to `{impl}{index}` |
| `impl` | yes | Registry impl name |
| `in` | depends | Topic(s) to subscribe to (§8.3) |
| `out` | depends | Topic(s) to publish to (§8.3) |
| `mode` | no | Subscription mode: `"default"`, `"live"`, `"blocking"`, `"catchup"` |
| `skip_if_finalized` | no | Topic to watch; skip incoming `Utterance`s whose segment was already finalised there (catchup only) |
| `enabled` | no | `false` to disable without removing the node; default `true` |
| *(other)* | — | Passed directly to the implementation's `__init__` as kwargs |

### 8.3 Wiring `in` and `out`

Both `in` and `out` accept three forms.

**Single string** — bound to the module's sole declared port (error if the module
declares none or several):

```toml
in  = "utterance.speech"
out = "text.raw"
```

**List** — topics mapped **positionally** onto the declared ports, in the order the
module declared them (§2). Reordering the list re-wires the node; there is no name
matching in this form. When the module has exactly one input port, any number of
topics creates a **fan-in** (all topics subscribe to that one port):

```toml
in = ["text.raw", "text.corrected", "text.out"]   # fan-in: all to one port
```

For `out`, the list must contain exactly one topic per declared output port — outputs
never fan out implicitly. A `"_"` entry skips that port: it is left unwired, publishes
nothing, and no bus topic is created for it. A topic produced only by a skipped port
is unpublished; validation rejects any node subscribing to it:

```toml
out = ["_", "text.out"]   # skip the first declared port; wire the second
```

**Table** — explicit `port_name = "topic"` mapping, validated against the module's
declarations:

```toml
[node.in]
text_in = "text.raw"

[node.out]
corrected  = "text.corrected"
translated = "text.out"
```

Port names are scoped to the module. Topic names carry no intrinsic meaning; routing
follows from the payload type registered by the producing module's output port.

### 8.4 Live + catchup nodes

Two nodes may subscribe to the same input with different modes:

```toml
[[node]]
name  = "asr_live"
impl  = "faster_whisper"
in    = "utterance.speech"
mode  = "live"
out   = "text.raw"

[[node]]
name              = "asr_quality"
impl              = "faster_whisper"
in                = "utterance.speech"
mode              = "catchup"
skip_if_finalized = "text.raw"
out               = "text.raw"
```

The live node emits quickly at lower quality; the quality node catches up when
capacity allows and emits higher-revision frames for the same `segment_id`s. The
sink display contract (replace on higher revision, §7.4) handles progressive
replacement automatically, and `skip_if_finalized` prevents the quality node from
re-processing utterances the live node already finalised.

### 8.5 Programmatic wiring

`chain_config` builds a linear config from stage names in code:

```python
from livesub.core.config import chain_config

cfg = chain_config(
    stages=["wav", "segment", "asr", "correct", "translate"],
    overrides={"asr": "mlx_whisper"},
    sinks=["stdout_pretty", "jsonl"],
)
```

The known chain stage names (`mic`, `ffmpeg`, `wav`, `segment`, `denoise`, `asr`,
`correct`, `translate`) and their default impls belong to the built-in audio
application. Omitting a stage (e.g. `["mic", "asr", "translate"]`) wires neighbours
together directly. Sinks auto-subscribe to all text topics.

### 8.6 Validation and diagram

```python
from livesub.core.graph import validate, mermaid

validate(cfg)        # raises GraphError on invalid wiring
print(mermaid(cfg))  # Mermaid flowchart LR string
```

Validation rules:

1. No duplicate node names.
2. At least one source node (a module with `inputs == {}`).
3. Every declared output port in config must match the module's `outputs` declaration.
4. Every topic read by a subscriber must be published by someone.
5. A topic's payload type (from its publisher's output port) must match the
   subscriber's input port type. **No prefix conventions** — a topic named `audio.foo`
   carrying `Utterance` is valid.
6. No cycles.

---

## 9. Worked Examples

Minimal modules of each shape, using plain integers to show that nothing about the
framework is audio-specific.

### Step 1 — Subclass `Module` and declare ports

```python
# mypkg/counter.py
from collections.abc import AsyncIterator
from livesub.core import Module

class CounterSource(Module):
    """Source: emits 0, 1, 2, ... then stops."""
    outputs = {"n": int}

    def __init__(self, count: int = 10):
        super().__init__()
        self.count = count

    async def run(self) -> AsyncIterator[int]:
        for i in range(self.count):
            yield i

# mypkg/doubler.py
from livesub.core import Module

class Doubler(Module):
    """Transform (1:1): sync process; the runner offloads it to an executor."""
    inputs  = {"n": int}
    outputs = {"n": int}

    def process(self, n: int) -> int:
        return n * 2

# mypkg/printer.py
import sys
from livesub.core import Module

class PrinterSink(Module):
    """Sink: owns its stdout output channel (constraint 4, §5)."""
    inputs = {"n": int}

    def __init__(self, stream=sys.stdout):
        super().__init__()
        self.stream = stream

    def process(self, n: int) -> None:
        print(n, file=self.stream)
```

### Step 2 — Register

```python
# livesub/core/registry.py
REGISTRY: dict[str, str] = {
    ...
    "counter": "mypkg.counter:CounterSource",
    "doubler": "mypkg.doubler:Doubler",
    "printer": "mypkg.printer:PrinterSink",
}
```

### Step 3 — Wire it in config

```toml
[[node]]
name = "gen"
impl = "counter"
out  = "numbers"

[[node]]
name = "x2"
impl = "doubler"
in   = "numbers"
out  = "doubled"

[[node]]
name = "show"
impl = "printer"
in   = "doubled"
```

The `numbers` and `doubled` topics carry `int`, so both default to `"blocking"`
mode (only `AudioFrame` topics default to `"live"`).

### Checklist by shape

| Shape | `inputs` | `outputs` | Required method | Key contracts |
|-------|----------|-----------|-----------------|---------------|
| Source | `{}` | `{port: T}` | `async def run() -> AsyncIterator[T]` | One output port; yield promptly; stamp timing/provenance fields (e.g. `t_capture`) as early as possible; shutdown cancels the task — let `CancelledError` propagate |
| Transform (1:1) | one port | one port | `process(T) -> U` | Return `None` to publish nothing; handle recoverable errors internally and return the input rather than raise (§4.7) |
| Transform (1:N) | one port | one port | `process(T) -> list[U]` | Override `drain()` to flush buffered items at end-of-stream (§4.6); reuse stable ids across partials if the payload type supports revisioning |
| Transform (multi-out) | one port | several ports | `process(T) -> dict[str, Any]` | Keys are declared output port names; a bare (non-dict) return goes to the first declared output topic — do not rely on that |
| Sink | one port | `{}` | `process(T) -> None` | Owns its output channel (§5.4); per-segment replace-on-higher-revision display for `TextFrame` topics; flush buffered output in `stop()` |

**Never** set `frame.lineage.revision` — carry the incoming `Lineage` into output
frames unchanged, or call `lineage.record_latency(name, seconds)`, which returns a
new `Lineage` with the same `revision` (§5, constraint 1).

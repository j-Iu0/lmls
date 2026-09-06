# General graph observability and lifecycle changes

The web runtime needs to observe any configured graph without replacing module
methods, subscribing a second consumer to every topic, or importing web code into
the core. These changes live in `core/graph.py`, `core/metrics.py`, and the bus
delivery/introspection paths. They introduce no web dependency.

## Observer contract

`Graph(config, on_event=callback)` accepts an optional **synchronous** callback.
Graph invokes it on its running asyncio loop, including when `process()` executes
in a worker thread. Calls are inline: observers should be quick and should treat
payloads as read-only. Exceptions, including an observer-raised `CancelledError`,
are logged and do not fail or cancel the graph. The graph retains no event history.

Each notification is a dictionary with a `kind`:

| Kind | Other fields | Meaning |
| --- | --- | --- |
| `node_state` | `node`, `state`, `message` | A lifecycle transition. States emitted are `starting`, `ready`, `running`, `completed`, and `failed`. |
| `output` | `node`, `topic`, `payload` | A successful graph publication. `payload` is the actual typed Python object delivered by the bus, including its stamped revision. |
| `processing` | `node`, `elapsed_ms` | One invocation of a transform or sink's `process()` has ended, including failure or cancellation. |

Output events cover sources, transforms, and drained transform output. A list or
multiple output ports can produce multiple output events for one processing event.
Returning `None` still counts as processing. Sources have output events but no
`process()` counts. Sink lock waits, input queue waits, executor queue waits,
publication/backpressure, and observer callback time are excluded from service
time. Async `process()` timing includes awaits inside that method. `drain()` is
not a `process()` invocation and adds no processing count.

The output event follows successful bus delivery; a cancelled partial fan-out can
have reached some subscribers without producing an output event. Callbacks are
notifications, not a transaction log. `Bus.publish()` now returns the delivered
object so the graph observes the same revision as subscribers, without retaining
a last-payload cache or stamping a second revision. Existing publishers can
continue ignoring its return value.

The existing `on_startup` callback remains attached at graph construction, so
driver warm-up reports still work. It retains its existing `StartupEvent` API
and possible worker-thread invocation; it is separate from `on_event`.

## Snapshot contract

Call `graph.snapshot()` on the graph's loop while it runs, or before/after the run.
It returns detached JSON-compatible data with these top-level keys:

```json
{
  "started_at": 0.0,
  "nodes": {
    "example": {
      "state": "pending",
      "message": "",
      "in_flight": 0,
      "processing": {
        "count": 0,
        "total_ms": 0.0,
        "mean_ms": 0.0,
        "last_ms": 0.0
      }
    }
  },
  "metrics": {"counts": {}, "stages": {}, "end_to_end": {}},
  "bus": {}
}
```

`pending` is the initial snapshot state; construction does not invoke observers
outside a loop. Cancellation uses terminal `completed` with a cancellation
message. Failures retain `failed` and an error message. Closing a prepared graph
without running it completes the stages that were started. Never-started nodes
remain `pending`.

`nodes[name].in_flight` counts active processing and publication operations, including
executor dispatch and publication blocked by backpressure. It excludes waiting
for input, source decoding, startup, and EOF delivery. Fan-in can make it greater
than one. Counters are updated on the loop and reset in `finally` on success,
failure, or cancellation; a cancelled synchronous operation remains counted while
its worker finishes. Inline processing/output observers still see the operation
as active. There is no event-loop scheduling gap between processing completion
and its publications at the existing call sites.

Once upstream production is paused, a session can detect descendant quiescence
by checking that their `in_flight` counts and relevant bus queue depths are all
zero. This also handles a transcriber returning `[]` or translator returning
`None`, which emit no output notification. This is an observation of current
work, not a guarantee that an unpaused source will produce no more work.

For sources, `nodes[name].diagnostics` contains a detached copy of `describe()`
when it returns a JSON-compatible dictionary. Raising, cyclic, non-finite, or
otherwise unserializable results are omitted. `describe()` must be a cheap,
synchronous diagnostic method; the graph cannot preempt blocking Python code.
Snapshots do not serialize or retain media payloads.

`bus` is the existing `Bus.report()` structure, with an added per-subscriber
`depth` field alongside `received`, `dropped`, and `max_depth`. Depth excludes
end-of-stream markers. Catch-up depth includes prefetched unread items and
available ring backlog. Ring overwrites are now counted as drops even before a
reader resumes. Abort shutdown also counts queued payloads it discards.

## Cleanup and startup rationale

Previously startup occurred outside `run()`'s cleanup boundary, so a failed model
load or cancelled source initialization could strand resources already opened.
Both direct `start()` and `run()` now clean up partial startup. All transforms and
sinks finish startup before any source starts. Their relative configuration order
is retained. Stages are recorded before awaiting startup and stopped once in
reverse startup order, including the stage whose startup failed.

`aclose()` joins a shared cleanup task, making repeated/concurrent close calls
idempotent and shielding resource cleanup from repeated caller cancellation.
Closing during startup cancels and joins the current startup operation. Stop
errors are reported without skipping remaining stages. Driver `start()` followed
by `run()` remains supported; a closed graph is single-use because its bus streams
have terminated. Construct a new graph for another run.

Transform and sink fan-in pumps are explicitly cancelled and joined when a sibling
fails or their runner is cancelled. Plain `gather()` does not cancel siblings on
an exception. Source async generators are explicitly closed. Running synchronous
workers are joined before stopping their stage: Python cannot forcibly interrupt
a worker thread, so shutdown must wait for that processing call to return.

Normal completion preserves queued output and graceful topic EOF. Abort cleanup
discards pending queues and closes readers without waiting for consumers that
have already stopped. This avoids a deadlock when inserting EOF into a full
blocking queue. A stage's own `TimeoutError` propagates as a failure; only the
graph's configured run deadline is handled as a normal timed stop. The run
deadline continues to start after startup.

## Optional segment namespaces

Separate sources or segmenters commonly start their local IDs at `u0001`. Without
namespaces, joining those branches can conflate unrelated segments in revision
counters, finalization tracking, and subtitle display state. Web sessions can opt
in with `Graph(config, namespace_segments=True)`. The default is `False`, preserving
existing CLI IDs and behavior. Enabling it is the session caller's responsibility;
the core does not import or detect web code.

An originating ID becomes the compact JSON encoding of `[node.config.name, local_id]`,
for example `["segment_a","u0001"]`. JSON encoding avoids delimiter ambiguity and
handles arbitrary node names and IDs. It is deterministic, so repeated partial,
final, and drain emissions from the same origin keep the same ID without an
ever-growing mapping table. Dataclass copies preserve the original payloads and
all other fields; normal bus revision stamping still occurs afterward.

The generic payload-boundary rules are:

- Direct `Utterance` sources namespace `Utterance.id`.
- Direct `TextFrame` sources namespace `Lineage.segment_id`, preserving the other
  lineage and frame fields.
- Transform `Utterance` output is a new origin when the input is not an
  `Utterance`, or when its output ID differs from its input ID. This covers
  audio segmentation and a transform explicitly creating a new segment.
- An `Utterance` transform preserving its input ID preserves the namespace too;
  pass-through does not add another prefix. Downstream text retains its inherited
  lineage ID.
- Drain output has no input payload, so an `Utterance` drained by a segmentation
  node is namespaced with that node and its local ID. Draining modules must emit
  their local IDs; the graph does not guess provenance from an ID's string syntax.

The rules depend on payload types and the actual input/output IDs, not module
implementation names, topic naming, or web-specific classes.

## Metrics and memory rationale

`record_event()` formerly copied every inherited lineage timing into stage
metrics. Combined with direct `record_stage()` calls, one operation was counted
again for each output, revision, and downstream stage. Stage accounting now comes
only from actual `process()` invocations. Frame lineage remains available for
provenance. Text emission counts and end-to-end samples include text sources.

`Metrics(max_samples=2048)` keeps at most 2,048 recent samples per stage and per
language end-to-end series. Lifetime counters, stage totals/means, and end-to-end
maxima survive sample eviction. The existing summary keys and table columns are
unchanged: `n` is lifetime count, `mean`/`max` are lifetime aggregates, and
`p50`/`p95` describe the retained recent window. Emission counts count publications,
not unique segments or processing operations.

Graph telemetry retains one state/message per configured node (messages capped at
2,048 characters), scalar aggregates, and bounded timing samples. It does not
retain event histories, output payloads, or snapshots. Metrics storage scales with
configured node names and distinct language labels, rather than frame count per
series. Existing bus revision/finalization indexes retain their existing segment
semantics; this change does not evict them or claim to bound all module memory.
Observers that forward events must bound their own queues or histories.

## Validation

`tests/test_observability.py` exercises ordered lifecycle notifications, loop-thread
callbacks, original startup reporting, exact stamped payload identity, real
processing counts, callback failures, partial startup failure/cancellation,
concurrent close during startup, repeated cancellation during stop, fan-in sibling
cleanup, full-queue abort, synchronous worker ownership and queue-excluded timing,
drain/`None` counts, deadline versus service timeouts, stop failures, detached safe
snapshots, ring drops/depth, and bounded samples with lifetime aggregates.

Quiescence tests cover concurrent synchronous/async processing, `[]`/`None`
results, errors/cancellation, publication backpressure, and idle source/input
waits. Namespace tests cover two segmenter branches with colliding local IDs,
stable partial/final/drain IDs, unchanged pass-through IDs, changed-ID origins,
direct utterance/text sources, payload field preservation, and opt-out behavior.

Existing startup, bus, graph wiring, and module tests provide regression coverage
for warm-up, delivery, fan-out, revision stamping, and module integration.

## Default output ports

`Module.default_output` optionally names the destination of a bare result from a
multi-output module. The mock, MLX, and cloud translators declare `text_out`:
faithful translation already returns a bare `TextFrame` in these adapters, while
repair mode returns named outputs. The editor exposes both ports, revealing that
the old runner selected the alphabetically first **topic** for a bare result.
Connecting or renaming an optional correction topic could therefore redirect a
translation to that socket. The declaration fixes routing without changing the
standalone adapters' return types. An unconnected default output drops that
result, just like an unconnected named dictionary output. Other multi-output
modules must return named outputs when multiple topics are wired, or explicitly
declare a default. Single-output modules keep their existing behavior.

# Studio backend integration

The React editor uses the existing loopback API. Run/Stop/Seek, streaming upload,
ranged media, config validation and TOML/JSON serialization retain their existing
contracts. There is no second processing engine or Deno production server. Playback clock
commands use the existing HTTP acknowledgement endpoint, with one request in
flight and at most one pending update per source. Snapshots use WebSocket.

## Python changes and justification

- `Module.option_sources` declares constructor/config classes a wrapper forwards
  keyword arguments to. `registry.option_parameters` exposes their actual names,
  annotations and defaults without constructing a module. Explicit constructor
  arguments win. Energy and Silero declare their segment timing and detector
  sources. This closes a real introspection gap caused by `**kwargs` and is useful
  to CLI tools, documentation generators, validation tooling and API clients.
  It contains no React components, browser hints, sliders or presentation ranges.
- The web catalog consumes that shared registry introspection. The frontend owns
  labels, primary/advanced grouping and optional slider presentation hints. No
  processing option is renamed or translated into an invented demo setting.
- The web host serves the generated SPA index and hashed assets. Setuptools
  includes that directory when a wheel is built after `deno task build`. Missing
  builds return an explicit setup message. The existing loopback, origin, media
  scope and CSP protections are unchanged.

No changes were needed to inference, clocks, queue behavior, epoch handling,
metrics collection, or subtitle production for this integration.

## Mapping and constraints

Canvas edges map declared source/target ports to bus topics. Single-port fan-in,
multi-output modules, unconsumed outputs, unknown nested options and disabled
nodes round-trip without collapsing the pipeline into the demo's five roles.
Roles remain presentation hints only. Original input aliases are preserved when
wiring is unchanged. New output topics are derived from the unique node name.

A topic broadcasts every publisher to every subscriber. If a canvas edit tries
to represent an impossible partial disconnection of a shared topic, export/run
reports it rather than silently executing different wiring. Orphan subscriptions
remain in the config so server validation can report them; importing does not
silently repair an invalid graph.

The server decides whether a graph is runnable. The client locks at validation
and uses authoritative startup/running/seeking/stopping states. Snapshot epochs
reject late results; seek clears subtitle selection/history. Reconnect restores
active state, while an idle snapshot does not replace local draft edits.

## Failure and notification ownership

Backend snapshots retain the authoritative run failure and transport state is
tracked separately. Neither is cleared when a toast is dismissed. The runtime
adapter converts state transitions into notification events: one event per failed
run epoch and one per connection-loss incident. WebSocket snapshots update runtime
state but do not repeatedly enqueue the same transition. A successful reconnect
resolves the connection event, while run failures remain inspectable in the
profiler after their notification is dismissed.

The notification store owns presentation lifetime, queueing, automatic expiry for
informational events and explicit dismissal. It has no knowledge of Python errors,
module names or dependency messages. This keeps server truth, transport state and
temporary UI feedback independently testable.

The default graph uses named mock modules. UI metrics are actual measurements
of that graph, including mock inference delays. Real models use the same adapter;
package/model availability is reported by Python. The history line is explicitly
an observed lifetime mean, not a synthetic waveform or per-operation trace.

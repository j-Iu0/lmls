# Canvas editor architecture

The interface review is followed by the implemented [backend integration](studio-integration.md).
Window and dock resizing preserve the viewport; only initial load, opening a
document, and an explicit Fit command frame the graph.

## Decision

Build a standalone React + TypeScript SPA in `studio/`, with React Flow for the
graph and Vite for development and static builds. Use Deno tasks and a Deno
lockfile. The Python service owns execution. The editor now loads its real catalog, imports/exports executable configs, and uses HTTP commands and WebSocket snapshots. The earlier simulator remains isolated for tests.

## Options considered

| Option | Fit and cost | Decision |
| --- | --- | --- |
| React + React Flow | HTML forms and media inside custom nodes; built-in connection gestures, selection, keyboard navigation, viewport, and accessible node wrappers. React requires attention to subscription scope. | Selected: most of the hard canvas interaction is already implemented, leaving small domain components. |
| Svelte 5 + Svelte Flow | Concise components and reactive state; the same XYFlow family and a compelling greenfield choice. | Runner-up. React Flow's extensive interaction examples and established integration surface reduce implementation and future maintenance risk for this editor. |
| Vue + Vue Flow | Also supports DOM nodes and a component architecture. | Viable, but there is no existing Vue investment here to prefer it over XYFlow's own React/Svelte implementations. |
| Rete 2 | Extensible visual programming framework with rendering, connection and execution plugins. | Extra plugin assembly and a second execution model are unnecessary when Python owns processing. |
| LiteGraph / custom Canvas or WebGL | Good drawing throughput for very large graphs. | Native inputs, selection, accessibility, IME, text and video become harder. Reconsider only after measuring a real DOM bottleneck. |
| Excalidraw / tldraw as the editor foundation | Excellent whiteboard interactions. | Borrow interaction design; typed sockets, live controls and graph validation would require substantial adaptation. |
| Custom SVG + framework | Maximum control and fewer named dependencies. | Reimplements edge routing, hit testing, zoom, selection, touch, keyboard interactions and accessibility. Poor maintenance tradeoff. |

Use native controls and scoped CSS with shared design tokens; avoid a large UI
kit, CSS utility framework, charting library, router, or SSR framework. Lucide
provides consistent tree-shaken icons. Small SVG plots are data visualizations.
Do not add a layout engine until arbitrary large-graph layout is a demonstrated
need; the demo starts with a hand-composed editable example.

## Architecture

```text
React components ── graph commands ── editable document + undo history
       │                                   │
       │ node/metric subscriptions          └─ validation / config codec
       ▼
Runtime adapter interface
       ├─ deterministic demo adapter (implemented)
       ├─ Python HTTP + WebSocket adapter (implemented)
       └─ state transitions ── notification event queue
```

- `domain/`: typed module definitions, fields, ports, graph validation and example.
- `state/`: document commands, bounded undo/redo; runtime lock enforced here,
  not merely by disabling buttons. Moving and inspecting remain possible in a run.
- `runtime/`: separate observable runtime, media clocks, bounded samples, captions
  and logs. A simulated adapter can later be replaced independently of the UI.
- `components/`: canvas, node shell, field controls, source transport, subtitle
  dock, profiler and contextual menus. No runtime clock in node document data.
- `styles/`: palette/spacing tokens and component styling, easy to reskin.

No additional state dependency is needed for this slice. React's external-store
subscription primitive separates graph edits from runtime updates. Consider
Zustand for selector ergonomics if state coordination expands; XState is useful
if reconnect/startup/recovery transitions later outgrow an explicit state model.

## Interaction design

The canvas occupies the work area. A hamburger button contains file operations;
the document name is plain text, with no logo or tool-mode toolbar. Left mouse
selects or drags nodes and box-selects empty space. Two-finger scrolling pans,
and a trackpad pinch zooms around the pointer. Middle-drag and Space-drag also
pan; Command/Control plus scrolling also zooms. Right-click or A adds a node.
Profiler opens beside Topics in the bottom monitor and is anchored above that dock.
There is no permanent library, inspector, dashboard, or diagnostics rail.

Dragging either an input or output socket into empty canvas opens a compatible-node
menu. Choosing a node creates it at the drop location and wires it in one undoable
edit. Escape cancels without mutation. Dropping on an existing node or incompatible
socket does not create a node. Socket hit areas remain at least 32 CSS pixels on
screen while zoomed out, with a smaller visible dot and a hover ring.

Nodes expose their common settings as selects, sliders, switches and file
controls. Advanced settings expand within the node. The title is the drag
handle. Color identifies payload type and module role, not arbitrary decoration.
Context menus and hover/focus actions expose duplicate and delete. All essential
actions also have visible buttons or keyboard paths; hover is not the only route.

Run captures the graph and locks configuration, creation, deletion and rewiring.
Node movement, selection, zoom, profiling and source playback controls remain
available. Pause affects source transport; it does not unlock the configuration.
Stop returns to editing and retains the last trace for inspection. Seeking clears
the simulated subtitle generation, matching the current backend's epoch model.

The source node includes a media timeline, pause/play, skips and a local file
picker. The sample uses a clearly labeled simulated media clock. A chosen local
audio/video file can play in-browser without uploading or processing it.

The bottom subtitle dock keeps segment grouping, original/translation topics,
language, partial/final state, revision, producer, stage and end-to-end latency,
and source timestamps. It can resize and collapse. Topic toggles filter observation,
not the graph's execution. Clicking a segment opens its visual timing breakdown.

Profiling has two levels: compact node timing/status during a run, and an optional
panel with per-stage timing bars, p50/p95/mean, queue pressure, drops and processed
counts. Processing-history plots explicitly label milliseconds, older/newer samples,
latest duration and their individual vertical scales. Values are sampled at 4 Hz
while active, retaining at most 80 samples (20 seconds of active sampling). These
plots exclude queue wait. Shared-scale stage bars explicitly represent p95.
A selected subtitle anchors the trace to a specific utterance. Distinguish
service time from queue wait; never label a sum of parallel service durations as
end-to-end latency. All demo values are marked as simulation.

## Implementation sequence

1. Build document model, representative modules, typed connections, undo/redo.
2. Compose the canvas, editable nodes and contextual node creation.
3. Add simulated lifecycle and source controls; enforce editing locks in commands.
4. Add docked revision-aware subtitles and visual profiling.
5. Verify domain invariants, typecheck/build, then exercise the interface.
6. Review the demo before connecting the real service.

## Integration boundaries

Production integration uses the current bootstrap, validate, run/stop, events and
media endpoints. The config codec preserves
unknown configuration settings, fan-in/topic mappings, disabled nodes and editor
metadata. The isolated demo adapter remains a test fixture and does not export
runnable livesub configuration.

The server remains authoritative for graph validation, lifecycle and epoch
rejection. The UI provides earlier feedback but is never the enforcement boundary.
Only source media clocks need frequent updates; batch telemetry notifications and
subscribe nodes by id when integrating real snapshots. Bound history and samples.

One possible future general change is explicit module option metadata (types,
ranges, enums, descriptions, conditional fields). Constructor introspection alone
cannot describe all `**kwargs` or constraints. Such metadata should live in the
module registry and benefit CLI help, validation and generated documentation, with
no browser imports or UI-specific component names. This is a proposal, not a change
made for the demo. Keep presentation hints in the frontend catalog.

Serve the built static SPA from the existing Python web package when integration
is accepted; Deno is a development/build tool, not an extra production service.
Vite's development proxy can forward `/api` to Python. SSR, a Deno backend, remote
hosting and a duplicate inference engine provide no benefit for a local workbench.

## References

- [React Flow custom nodes](https://reactflow.dev/learn/customization/custom-nodes)
- [React Flow performance](https://reactflow.dev/learn/advanced-use/performance)
- [Svelte Flow](https://svelteflow.dev/)
- [Rete editor architecture](https://retejs.org/docs/concepts/editor/)
- [Rete dataflow engine](https://retejs.org/docs/guides/processing/dataflow/)
- [Excalidraw](https://excalidraw.com/) — inspected the current canvas and toolbar.

Dependency versions are resolved from current stable registry tags when installed
and pinned in the manifest and lockfile for reproducibility.

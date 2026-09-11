# lmls studio

The node editor now connects to the existing Python HTTP/WebSocket service.
Deno builds the frontend; Python runs the graph and serves the production files.

From the repository root:

```sh
pip install -e . -e studio/backend
cd studio
deno install
deno task build
cd ..
studio serve --media-root .
```

Open http://127.0.0.1:8080. Build before packaging a wheel: generated assets are
included in the `lmls_studio` (studio/backend) package data. A missing build returns
setup instructions.

For development, keep Python running on port 8080 and run `deno task dev` in
`studio/`. Vite on port 5173 proxies `/api`, media, and WebSockets to Python.
The proxy translates only its own loopback Origin; server origin checks remain
unchanged. Node is not required. `@types/node` supplies Vite's build-time types.

```sh
deno task check
deno task test
deno task build
```

## Interaction

- Left-click selects; left-drag moves a node header or box-selects on canvas.
- Two-finger scrolling pans; trackpad pinch zooms around the pointer.
  Middle-drag and Space + left-drag also pan; Command/Control + scroll zooms.
- Resizing the window or subtitle dock preserves zoom and pan. F explicitly fits.
- Right-click or A opens the module catalog. Drag a socket onto empty space to
  create and connect a compatible module. One undo removes both.
- Options, enabled state and queue policy are edited on nodes. Advanced options
  expand in place. Modules use the backend's real constructor names and ports.
- The hamburger menu opens/saves executable TOML or JSON, validates, and creates
  a new graph. Unknown options, extension settings, disabled nodes and fan-in
  mappings survive import/export. Layout is saved in `[editor.positions]`.
- Run validates and locks configuration. File sources start paused: press Play
  source to begin. Media time comes from the browser, not a simulated timer.
- Pause gates file playback. It does not stop microphones or independent live
  branches. Stop releases the graph and unlocks editing.
- Local file sources carry an Overlay subtitles picker, listing the same text
  endpoints as the subtitle monitor, and an Auto pause switch that holds playback
  until the attached subtitles for the current segment have arrived. Auto pause
  stays off until an overlay is attached; saved `[editor.overlays]` and
  `[editor.auto_pause]` round-trip through these controls.
- Seeking a running file restarts the graph generation and clears old subtitles.
  File playback resumes with Play source after restart. Completed graphs can be
  run again; seek is disabled after completion to protect subsequent draft edits.
- Profiler and an endpoint filter sit beside the bottom subtitle monitor. The
  monitor shows every text endpoint; the Endpoints button hides/show specific
  producing nodes. Click a subtitle to inspect its actual stage lineage. No
  missing queue wait is invented.

## Media and measurements

Use a path under `--media-root` or Choose local media to upload to the local
backend. Uploads are temporary and disappear when the Python service exits.
Use a stable path for portable saved configurations. Browser codec support still
applies; live URLs and devices are not seekable file previews.

The default Python graph has **mock_transcriber** and **mock_translator** nodes.
It runs real media decoding, segmentation, correction, bus delivery and metrics;
mock inference returns scripted text with configured artificial delays. Choose
real modules or import an existing config to use installed inference engines.
Startup and dependency/model failures appear in the profiler's Events tab and
on affected nodes. No automatic fallback to simulated output is used.

History plots show lifetime mean service time at changes in processing count,
retaining 80 observed updates. They are not raw operation samples or a fixed-rate
time axis. Percentiles use the backend's recent 2,048 operations; means/counts
cover the run. Queues, drops and audio levels come directly from snapshots.
Subtitles retain the backend's segment IDs, revisions, producer, topic and lineage.
A missing media timestamp is shown as unavailable, not guessed.

Connection loss pauses local playback and keeps an uncertain active graph locked
until reconnect restores authoritative state. Idle reconnects preserve draft edits.
One backend owns one graph; all connected tabs share its controls. Save before
reloading: browser-local drafts are not automatically persisted.

## Boundaries and verification

`domain/backend.ts` adapts the transport document without changing the Python
config format. `runtime/backend.ts` owns commands, reconnect, snapshots and clocks.
The old deterministic adapter remains only as an isolated test fixture.

Deno tests cover checked-in config round trips, nested extension values, real
port routing, disconnection, generation-bounded telemetry and canvas gestures.
Python tests cover API lifecycle, media ranges/upload, generation rejection,
observation, forwarded option metadata, and production static serving.

See [backend integration notes](../docs/studio-integration.md) and
[the system architecture](../docs/architecture.md).

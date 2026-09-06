# Livesub Studio

Livesub Studio runs next to the Python pipeline on your computer. It uses your
existing module registry and configurations; no cloud deployment or model server
is required for the included mock pipeline.

```bash
pip install -e '.[web]'
python -m livesub web
# Open http://127.0.0.1:8080

# Open an existing graph, and optionally allow media from a different folder:
python -m livesub web -c config/video.toml --media-root /path/to/lectures --port 8080
```

FFmpeg must be on PATH for interactive audio/video files. The starting graph uses
`assets/lecture.wav` relative to the media root. If running outside this checkout,
choose a file with the source node's File button. The starting transcriber and
translator are explicitly **mock** modules producing scripted test text. Select
real modules in the library and connect them to transcribe your recording. Model
packages and devices have the same requirements as the CLI. Microphone capture is
on the Python host, with its normal OS microphone permission, not in the browser.

## Graph editing

Click a module in the searchable library, or drag it onto the canvas. Drag nodes
by their headers; drag the canvas to pan and scroll to zoom. Connect matching
colored sockets by clicking an output and then an input, or dragging between them.
Connections follow declared Python payload types, not node or topic names. One
output can feed many nodes; a module with one input port supports multiple topics.

Select a node to edit its name, constructor options, subscription policy, enabled
state, and topic mappings in the inspector. The JSON options editor also accepts
implementation-specific options not listed in a constructor signature, including
segmenter timing controls. Select a connection and press Delete to disconnect it.
Delete removes a selected node; Escape cancels a connection, and F fits the graph.

Validation uses the same Python validator as `livesub graph`. Missing publishers,
incompatible types, duplicate names, cycles, and invalid ports prevent Run. A
missing optional package or model startup failure is reported in diagnostics.
Once starting, running, seeking, or stopping, graph editing is locked in the UI;
the server independently rejects graph replacement. Stop unlocks the editor.
Media controls, canvas navigation, and panel sizing remain available while running.

Import and export support **TOML and JSON**, including the existing `[[node]]`
format. Global settings and unknown extension tables are preserved. The optional
`[editor]` table stores node positions, subtitle subscriptions, overlay topics, and
automatic-pause choices. Export is the portable save operation. Layout/local draft
storage belongs only to this browser; it does not alter an on-disk config.

Uploads are streamed to temporary files and limited to 2 GiB. They are deleted
when the server exits. To reuse an exported config across server sessions, put the
media under `--media-root` and set a stable path in the node options instead of an
upload's temporary path. Media files are served with HTTP Range support; native
browser codec support determines which videos play. FFmpeg's ability to decode a
file for subtitles does not imply a browser can display its video codec.

## Subtitle monitor and video

Use Topics in the bottom subtitle monitor to connect any text output to the panel.
Each segment/topic updates in place when a newer revision arrives. The panel can
be resized, minimized, or expanded to the viewport. Text is inserted as text, never
interpreted as markup. The media source's overlay selector independently connects
a text output to the video. Its fullscreen button includes the subtitle overlay.

Native playback controls support play, pause, and seeking; the node also provides
five-second skips. **Every seek starts a fresh graph generation and clears all
subtitle history**, including forward seeks. This conservative policy resets VAD
buffers, pending work, corrections, and translator discourse context together.
Generation checks reject old clocks and late output from the prior position.
All source clocks in a multi-source run are restarted at their current positions
when one source seeks. Restarting can reload models; it favors correct reset
semantics over selectively retaining undocumented state.

"Pause while captions catch up" pauses playback when a **completed utterance**
awaits a final result on the selected overlay topic, and resumes when it arrives.
The decoder can read 0.5 seconds ahead of the displayed video so trailing silence
can finalize an utterance. Partials do not trigger pausing: some correction and
translation modules intentionally discard partials. This keeps completed speech
current; it does not promise captions before an utterance has been segmented.
If the selected module suppresses a final output, playback remains paused and the
status identifies the outstanding subtitle; use diagnostics and Stop to adjust
the pipeline. Automatic pause requires an attached text output that originates
only from that file, avoiding ambiguous synchronization across mixed inputs.

The web driver adapts local `wav` and non-device `ffmpeg` sources to the same
clocked transport, preserving the saved config. Streaming URLs remain ordinary
FFmpeg live sources and do not offer seekable local-file playback. The explicit
`media` implementation requires a playback-clock driver; for an unattended CLI
run, select `wav` or `ffmpeg`. Browser pause gates file decoding, not microphone
capture or independent live source branches.

## Profiling, debugging, and lifecycle

Nodes show runtime state, mean/p95 processing time, queue depth/drop counts, and
live audio levels where available. Diagnostics includes startup and model-loading
progress, processing counts, end-to-end timing, warnings, and errors. Download the
snapshot and log JSON to investigate a run. Values come from the running graph;
there are no simulated dashboard metrics. Mock module inference delays are
intentionally configured by those modules and clearly identified as mock output.

Service time measures module processing, separately from queue depth. It excludes
queue waits and observer overhead; async processing includes awaited inference.
End-to-end timing starts at the captured end of speech, while file overlay timing
uses a separate bounded media-clock mapping. Percentiles use the most recent
2,048 observations; counts and means are lifetime aggregates. The web monitor
retains 500 latest subtitle entries, 250 logs, and 2,000 utterance timing entries.
This bounds monitor retention, not the entire runtime or model memory.

The service binds to loopback only and rejects cross-origin requests and
unexpected Host headers. Media reads are restricted to the selected media root
and temporary uploads. Closing the last connected browser stops the run and
releases its devices. Reconnection restores authoritative active state and
retained captions, while an idle server does not overwrite local draft edits.
One server owns one active graph; all connected tabs share its controls.

## Justification for Python changes

| Change | Why it is needed and how it stays general |
| --- | --- |
| `core.config.config_from_dict` / `config_to_dict` | Share parsing and lossless graph serialization between files and API clients, including fan-in. No browser-specific schema enters the core. |
| `input.media` + one registry entry | Adds an externally clocked source with bounded timestamp mapping. Any playback driver can control it; existing input modules retain their behavior. See [media transport](media-transport.md). |
| `Graph.on_event` / `snapshot`, bus delivery return and queue depth | Observe arbitrary typed modules without injecting web code into each module or adding consumers that alter backpressure. See [runtime changes](web-runtime-changes.md). |
| Graph startup, shutdown, and pump cleanup | Repeated Run/Stop/Seek requires reliable ownership of devices and workers, especially after failed or cancelled startup. CLI callers benefit from the same fixes. |
| Bounded metrics and corrected stage accounting | A continuously open monitor needs bounded samples and accurate operation counts. Inherited lineage timings were being counted repeatedly. |
| `web` package | Isolates HTTP, media access, generation handling, UI history, and editor-only metadata from the inference modules. Transport schemas are documented below. |
| CLI `web`, optional dependencies, static package data | Makes the frontend installable and runnable from the existing command. `aiohttp` handles HTTP/WebSockets/ranged files; `tomli-w` writes valid TOML. Neither is required for core CLI use. |

## API

- `GET /api/bootstrap`: module catalog, editor document, and session snapshot.
- `POST /api/config/import`: `{text, format}` to an editor document.
- `POST /api/config/export`: `{config, format}` to pipeline TOML/JSON.
- `POST /api/validate`: `{config}` to warnings, or HTTP 400 with the validation error.
- `POST /api/run`: `{config}` to start one graph; conflicts return HTTP 409.
- `POST /api/stop`: stop and drain resource cleanup.
- `POST /api/seek`: `{node, position, epoch}` to a fresh generation.
- `POST /api/playback`: `{node, position, paused, epoch}` to advance a file clock.
- `GET /api/events`: WebSocket, snapshots every 300 ms; send clock messages with
  the same playback fields and `type: "clock"`.
- `POST /api/upload?name=FILE`: raw streamed media upload.
- `GET /api/media?path=PATH`: scoped media playback with byte ranges.

A snapshot contains `state`, `epoch`, `config`, `nodes`, `metrics`, `topics`,
`subtitles`, `logs`, `media`, and `error`. Editor documents contain normalized
`nodes`, global `settings`, and `editor`. Node ports map names to bus topics.

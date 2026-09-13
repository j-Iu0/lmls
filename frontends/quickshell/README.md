# Quickshell subtitle window

A standalone subtitle frontend for `lmls`. It connects to the pipeline's WebSocket
sink and shows subtitles in a normal floating window as a scrolling transcript:
each segment lists the source line above its selected translation, with the newest
segment at the bottom. Raw transcription appears immediately; corrections and
translations update their segment in place. The list follows new segments as they
arrive — unless you have scrolled up to review, in which case the view stays put.

## Requirements and desktop support

- Quickshell 0.3.1 and the Qt 6 `QtWebSockets` QML module.
- A Wayland compositor (tested on niri). The window is an ordinary toplevel, so it
  works on any compositor Quickshell supports; stacking above fullscreen video is
  intentionally not provided.
- The backend from this checkout, which includes `topic` in WebSocket events.

The window is translucent (`color: "transparent"` on the FloatingWindow plus an
adjustable panel backing) and is moved with `startSystemMove`/`startSystemResize`
from its title bar and edge handles. Quickshell's QML plugins ship inside the
`quickshell` binary, so unit tests target the Quickshell-free components
(`Subtitles.js`, `SubtitlePanel`, `WindowChrome`) while the thin wrappers are
covered by an opt-in live smoke test.

## Run

From the repository root, start the frontend:

```bash
quickshell --no-duplicate --path frontends/quickshell
```

In another terminal, run the mock pipeline. It reads the bundled lecture recording
in real time and emits scripted subtitles; it does not play sound or load models.

```bash
.venv/bin/python -m lmls run -c frontends/quickshell/mock.toml
```

For live microphone captions, the existing default config already enables WebSocket
output and can replace the mock command:

```bash
.venv/bin/python -m lmls run -c config/default.toml
```

The demo command can also serve the window:

```bash
.venv/bin/python -m lmls demo --source mic --language vi --websocket-port 8765
```

The frontend can start before the backend. Captions still update once the
connection succeeds; the stream reconnects with delays from 500 ms up to 10
seconds, and a successful connection clears state before accepting the server's
replay. Captions expire after eight seconds without a visible text change,
including after disconnection. Replay uses the original emission timestamps so
expired text stays hidden.

## Window controls

- Drag the title bar to move the window; drag any edge or corner to resize.
  Compositor-enforced limits keep the window between 480x260 and 1600x1000.
- `Source` and `Translation` toggle the two lines of every segment independently.
- The list autoscrolls to the newest segment while you are at the bottom; scroll
  up to read earlier segments and the view stays there until you scroll back
  down (scrolling to the bottom resumes autoscroll).
- `Hide` collapses the window to its title bar; `Show` restores it.
- `Quit` closes the window and the Quickshell instance.

## Configure

Edit `Settings.qml`; Quickshell reloads changes. Environment variables override the
connection, monitor, and target language when starting the frontend:

```bash
LMLS_WS_URL=ws://127.0.0.1:8765 LMLS_MONITOR=eDP-1 LMLS_LANGUAGE=vi \
  quickshell --no-duplicate --path frontends/quickshell
```

Use `niri msg outputs` to find monitor names. An empty or unavailable monitor name
falls back to the first connected output. Changing `LMLS_LANGUAGE` only selects
which received language to display. Configure the backend to generate that
language as well.

| Setting | Default | Behavior |
| --- | --- | --- |
| `sourceLanguage` / `targetLanguage` | `en` / `vi` | Exact language tags to display. |
| `sourceTopics` | corrected, then raw | Priority list of topic names for the source line. |
| `translationTopics` | `text.out`, `text.vi`, `text.zh` | Priority list for the selected translation. |
| `initialWidth` / `initialHeight` | 900 / 520 | First-show window size, within the 480x260–1600x1000 limits. |
| `sourceFontSize` / `translationFontSize` | 28 / 32 | Font sizes in logical pixels. |
| `backgroundOpacity` | 0.78 | Panel backing opacity; 0 shows the desktop through the window. |
| `timeoutMs` | 8000 | Inactivity timeout; 0 keeps the latest subtitle visible. |
| `historyLimit` | 40 | Maximum retained segment blocks. |

Topic lists are ordered from highest to lowest priority. Unknown topics have lower
priority than listed topics, and language selection applies first. For custom
pipeline wiring, update these lists to match its actual topic names. Revisions are
compared only within the same `(segment_id, topic, lang)`; revisions from different
topics are not comparable. Delayed events for an earlier utterance do not replace
the newest utterance, ordered using `t_audio_end` (falling back to `t_emit` or
arrival time when absent). Missing timestamps limit how reliably delayed segments
can be ordered. The standard backend supplies both timestamps.

## IPC and niri keybindings

Always include the `--` separator: without it, Quickshell can interpret `show` as
its own CLI subcommand instead of calling the frontend.

```bash
quickshell ipc --path frontends/quickshell call -- subtitles toggle
quickshell ipc --path frontends/quickshell call -- subtitles show
quickshell ipc --path frontends/quickshell call -- subtitles hide
quickshell ipc --path frontends/quickshell call -- subtitles clear
quickshell ipc --path frontends/quickshell call -- subtitles status
quickshell ipc --path frontends/quickshell call -- subtitles quit
```

`hide` collapses the window to its title bar; `show` restores the captions.
`clear` empties stored captions. `status` returns JSON containing the connection
state, current text, monitor, last error, and count of rejected messages
(including stale revisions).

Example niri configuration, with the path changed to your checkout:

```kdl
spawn-at-startup "quickshell" "--no-duplicate" "--path" "/home/jerry/Projects/lmls/frontends/quickshell"

binds {
    Mod+Shift+S { spawn "quickshell" "ipc" "--path" "/home/jerry/Projects/lmls/frontends/quickshell" "call" "--" "subtitles" "toggle"; }
    Mod+Shift+C { spawn "quickshell" "ipc" "--path" "/home/jerry/Projects/lmls/frontends/quickshell" "call" "--" "subtitles" "clear"; }
}
```

Choose unused bindings and merge them into your existing `binds` block. These are
examples; installation does not edit your niri configuration.

## Troubleshooting and validation

- No subtitles: inspect `status`. Confirm the backend has a WebSocket sink and
  produces the selected language.
- `Subtitle event lacks topic`: restart the backend using this checkout. Older
  backends omitted provenance; those messages are rejected instead of guessing
  which revision belongs to which stage.
- Missing `QtWebSockets`: install the Qt 6 WebSockets QML package for your distro.
- Translucency shows black: your compositor or GPU path may not support
  translucent windows; raise `backgroundOpacity` to 1 for an opaque panel.

Run these from the repository root:

```bash
.venv/bin/python -m pytest -q
node frontends/quickshell/tests/subtitles.test.cjs
/usr/lib/qt6/bin/qmltestrunner -platform offscreen -input frontends/quickshell/tests
```

The Qt 6 runner path above is for Arch Linux. Use your distro's Qt 6 executable;
an unqualified `qmltestrunner` can select Qt 5. The QML tests run offscreen and
cover the panel (history ordering, autoscroll, toggles, fonts, backing opacity),
the window chrome (drag and resize requests, hide/quit, size limits), and the
stream against real loopback WebSockets (receive, malformed JSON, reconnect,
session reset, expiry).

The live niri smoke test is opt-in because it briefly opens the window:

```bash
.venv/bin/python -m pytest frontends/quickshell/tests/test_overlay.py -q
```

It checks subtitle reception, IPC controls, window discovery by title, and clean
shutdown. Move, resize, and translucency interact with the compositor and are
worth a quick manual check on each new desktop setup.

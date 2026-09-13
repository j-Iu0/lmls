# Quickshell subtitle overlay

A standalone transparent subtitle frontend for `lmls`. It connects to
the pipeline's WebSocket sink and shows the newest utterance with source text above
one selected translation. Raw transcription appears immediately; corrections and
translations update the same block.

## Requirements and desktop support

- Quickshell 0.3.1 and the Qt 6 `QtWebSockets` QML module.
- A Wayland compositor supporting `wlr-layer-shell`. Tested on niri with Quickshell
  0.3.1. Other compositors with that protocol may work but are not validated here.
- The backend from this checkout, which includes `topic` in WebSocket events.

The window starts with a transparent surface. `WlrLayer.Overlay` places it above
normal and fullscreen windows on niri; `Top` would disappear behind fullscreen
windows. An empty input region lets clicks pass through, keyboard focus is disabled,
and `ExclusionMode.Ignore` reserves no screen space. It runs independently of
DankMaterialShell and needs no niri layer rule. Other overlay surfaces can still
overlap it; this does not bypass the session lock.

This version targets native Wayland. GNOME without layer-shell support and X11 need
different window-management approaches and are not supported by this configuration.

References: [Quickshell windows](https://quickshell.org/docs/v0.3.1/types/Quickshell/QsWindow/),
[layer shell](https://quickshell.org/docs/v0.3.1/types/Quickshell.Wayland/WlrLayershell/),
[niri fullscreen behavior](https://github.com/niri-wm/niri/wiki/Fullscreen-and-Maximize).

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

The demo command can also serve the overlay:

```bash
.venv/bin/python -m lmls demo --source mic --language vi --websocket-port 8765
```

The frontend can start before the backend. It remains invisible until a caption
arrives and reconnects with delays from 500 ms up to 10 seconds. A successful
connection resets history before accepting the server's replay. Captions expire
after eight seconds without a visible text change, including after disconnection.
Replay uses the original emission timestamps so expired text stays hidden.

## Configure

Edit `Settings.qml`; Quickshell reloads changes. Environment variables override the
connection, monitor, and target language when starting the frontend:

```bash
LMLS_WS_URL=ws://127.0.0.1:8765 LMLS_MONITOR=eDP-1 LMLS_LANGUAGE=vi \
  quickshell --no-duplicate --path frontends/quickshell
```

Use `niri msg outputs` to find monitor names. An empty or unavailable monitor name
falls back to the first connected output; output changes update this selection.
Changing `LMLS_LANGUAGE` only selects which received language to display. Configure
the backend to generate that language as well.

| Setting | Default | Behavior |
| --- | --- | --- |
| `sourceLanguage` / `targetLanguage` | `en` / `vi` | Exact language tags to display. |
| `sourceTopics` | corrected, then raw | Priority list of topic names for the source line. |
| `translationTopics` | `text.out`, `text.vi`, `text.zh` | Priority list for the selected translation. |
| `showSource` / `showTranslation` | both `true` | Hide either line. |
| `bottomMargin` | 80 | Logical pixels above the screen bottom. |
| `maxWidth` / `widthFraction` | 1100 / 0.8 | Maximum width, also capped at 80% of output width. |
| `sourceFontSize` / `translationFontSize` | 28 / 32 | Font sizes in logical pixels. |
| `fontFamily` | `sans-serif` | Use a font with glyphs for the chosen languages. |
| `maxLines` | 3 | Wrapped lines per language; longer text is elided. |
| `backgroundOpacity` | 0.0 | Optional black backing; try 0.35 over busy video. |
| `timeoutMs` | 8000 | Inactivity timeout; 0 keeps the latest subtitle visible. |
| `historyLimit` | 40 | Maximum retained segment blocks. |

Topic lists are ordered from highest to lowest priority. Unknown topics have lower
priority than listed topics, and language selection applies first. For custom
pipeline wiring, update these lists to match its actual topic names. Revisions are
compared only within the same `(segment_id, topic, lang)`; revisions from different
topics are not comparable. Delayed events for an earlier utterance do not replace
the newest utterance, ordered using `t_audio_end` (falling back to `t_emit` or arrival
time when absent). Missing timestamps limit how reliably delayed segments can be
ordered. The standard backend supplies both timestamps.

## Controls and niri keybindings

Always include the `--` separator: without it, Quickshell can interpret `show` as
its own CLI subcommand instead of calling the overlay.

```bash
quickshell ipc --path frontends/quickshell call -- subtitles toggle
quickshell ipc --path frontends/quickshell call -- subtitles show
quickshell ipc --path frontends/quickshell call -- subtitles hide
quickshell ipc --path frontends/quickshell call -- subtitles clear
quickshell ipc --path frontends/quickshell call -- subtitles status
quickshell ipc --path frontends/quickshell call -- subtitles quit
```

`hide` keeps receiving subtitles; `show` displays the current unexpired caption.
`clear` empties stored captions; the next event can show text again. `status` returns
JSON containing the connection state, current text, monitor, last error, and count
of rejected messages (including stale revisions).

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

- Nothing visible: inspect `status`. Confirm the backend has a WebSocket sink and
  produces the selected language. Silence and expired captions intentionally hide
  the window.
- `Subtitle event lacks topic`: restart the backend using this checkout. Older
  backends omitted provenance; those messages are rejected instead of guessing
  which revision belongs to which stage.
- Missing `QtWebSockets`: install the Qt 6 WebSockets QML package for your distro.
- Wrong stacking: `niri msg layers` should list namespace `lmls-subtitles`, layer
  `Overlay`, keyboard interaction `None` while a caption is visible. Check custom
  compositor layer rules matching this namespace if behavior differs.

Run these from the repository root:

```bash
.venv/bin/python -m pytest -q
node frontends/quickshell/tests/subtitles.test.cjs
/usr/lib/qt6/bin/qmltestrunner -platform offscreen -input frontends/quickshell/tests
```

The Qt 6 runner path above is for Arch Linux. Use your distro's Qt 6 executable;
an unqualified `qmltestrunner` can select Qt 5. The QML test uses real loopback
WebSockets and covers receive, malformed JSON, reconnect, session reset, and expiry.

The live niri smoke test is opt-in because it briefly opens an overlay:

```bash
.venv/bin/python -m pytest frontends/quickshell/tests/test_overlay.py -q
```

It checks received text, layer/output selection, keyboard interactivity, preservation
of the focused window, and IPC controls. Fullscreen appearance was also checked
against a dedicated fullscreen Qt window. Physical mouse click-through and monitor
hotplug should be checked manually when deploying on a different desktop setup.

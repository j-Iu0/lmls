# livesub — live multilingo lecture subtitles

Captures live speech (microphone, video file, stream URL, or system audio),
transcribes it to English, corrects recognition errors, translates to Vietnamese
or Chinese or other languages, and emits a live subtitle stream that corrects itself in place.

```
Microphone ──▶ noise reduction ──▶ segmentation ──▶ transcription ──▶ correction ──▶ translation ──▶ display
   (or video / stream URL      (optional, off by   (utterance       (optional)      (optional)     (many at once)
    / system audio)             default — measured   boundaries)
                                to hurt accuracy)
```

Every stage is an **independent module**. None imports another. Which implementation runs,
whether a stage runs at all, and who consumes each stage's output are all decided by a
config file — see [Architecture](docs/architecture.md).

---

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the whole pipeline with **no models to download** — this works immediately:

```bash
python -m livesub run --config config/mock.toml
```

### For the real thing: Ollama (the portable default)

`config/default.toml` runs transcription in-process on faster-whisper (CPU) and the LLM
stages through a local [Ollama](https://ollama.com) server — portable across Linux,
Windows, NVIDIA, AMD, and CPU, still local, no API key, nothing leaves the machine:

```bash
ollama pull qwen3.5:4b                        # server side (outside the venv)
pip install -r requirements-cpu.txt # faster-whisper + the Ollama client
python -m livesub run --config config/default.toml
```

First run downloads ~0.5 GB (Whisper small.en) into `~/.cache/huggingface`. The Ollama
server loads `qwen3.5:4b` once and keeps it warm between subtitle lines. Everything
after that runs offline.

### On Apple Silicon: MLX instead

Same architecture, Metal-accelerated models running in-process, measured faster per line
(see docs/architecture.md):

```bash
pip install -r requirements-mlx.txt
python -m livesub run --config config/mlx.toml
```

First run downloads ~3.1 GB (Qwen3.5-4B 4-bit) alongside the Whisper weights above.
Everything after that runs offline. The adapters work standalone too:

```bash
python -m livesub.translate run ollama --target vi --text "Hello class"
python -m livesub.correct run ollama --text "grade ee ent dissent uses back propagation"
```

### macOS permissions

The terminal needs microphone access: **System Settings → Privacy & Security →
Microphone**. Check it is working before anything else:

```bash
python -m livesub.input level --seconds 5
```

A bar that never moves means the permission was not granted to *this* terminal app.

### On Windows

**Activation syntax.**:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

In `cmd.exe` it is `.venv\Scripts\activate.bat`. 

**System audio (loopback).** There is no Background Music/BlackHole equivalent installed
by default. ffmpeg 6.1 or newer can capture speaker output directly through the WASAPI
loopback mode of its `dshow` input — find your output device name under *Settings →
Sound → More sound settings → Playback*, then point the ffmpeg source at it via config
(the `extra_args` option reaches raw ffmpeg arguments):

```toml
[[node]]
name        = "input"
impl        = "ffmpeg"
out         = "audio.raw"
url         = "audio=Speakers (Realtek High Definition Audio)"
extra_args  = ["-f", "dshow", "-wasapi_loopback", "1"]
```

```powershell
python -m livesub run -c config/video.toml
```

ffmpeg itself can be installed with `winget install ffmpeg` if it is not already on
`PATH`.

---

## Captioning a video, a stream, or a video call

The input module normalises everything to the same 16 kHz mono frames, so no other stage
changes:

```bash
# a video or audio file, or a stream URL (HLS/RTSP/HTTP)
python -m livesub run -c config/video.toml --source lecture.mp4
python -m livesub run -c config/video.toml --source https://example.com/live.m3u8

# system audio — captions a Zoom call or a video playing on this Mac
python -m livesub devices
python -m livesub run -c config/video.toml --source "Background Music" --device
```

System audio needs a virtual loopback device. **Background Music** (free, already
installed on the development machine) or **BlackHole** both work — install one, then it
appears in `livesub devices` and is selected by name.

---

## Live demonstration

Run the fixed demonstration pipeline from a media file:

```bash
python -m livesub demo --source ffmpeg --input lecture.mp3 --language vi \
  --start 240 --seconds 90
```

English appears in about a second on the Apple Silicon wiring (a little longer on the
portable default), the corrected revision replaces that same line in
place (marked `*`), and the Vietnamese fills the line beneath it. Each block shows its own
end-of-speech-to-display delay.

The demo does not read a config file. It uses a fixed variant of the default graph and
only exposes deliberate choices. Use `--source mic` (the default) for a microphone, or
`--source ffmpeg --input FILE_OR_URL` for anything ffmpeg can read. Repeat `--language`
for simultaneous output, for example `--language vi --language zh`. Choose `--backend
mlx`, `--backend whisper-ollama`, or `--backend cuda`; when omitted, the host is detected.
CUDA currently means faster-whisper on CUDA plus Ollama for the language stages.
Use `--model MODEL` to select the backend language model and `--asr-model MODEL` to
override Whisper separately. A backend with a fixed model ignores `--model` with a
visible warning instead of silently accepting it.

For ffmpeg inputs, the same file or URL is played aloud with `ffplay` while it is
captioned; `--start` and `--seconds` apply to both paths. Use `--no-audio` to caption
silently. Microphone input is not played back, avoiding an acoustic feedback loop.

Models are loaded and warmed **before** capture begins. `--buffer-mode live` stays current
by dropping stale queued items; `--buffer-mode block` applies backpressure and drops
nothing. Startup events show model-loading progress before capture, and startup failures
exit cleanly with the module's error. The catch-up mode is intentionally not available in
the demo. See `livesub demo --help` for model, logging, JSONL, WebSocket, timing, and
statistics options.

---

## Choosing a wiring

Each preset is a different architecture. Nothing in the Python differs between them.

| Preset | What it is |
| --- | --- |
| `config/default.toml` | All five stages, with correction and translation fused into **one** node, one model call. **Portable**: faster-whisper + Ollama, runs on any OS/CPU/GPU. |
| `config/mlx.toml` | Correction and translation as separate nodes on **Apple Silicon** — MLX models running in-process, measured faster. |
| `config/fused.toml` | The fused wiring on **Apple Silicon** (MLX). The portable fused wiring is now the default. |
| `config/no_correct.toml` | **No correction stage.** Raw English reaches the screen as-is; use the default's fused node instead to repair errors. |
| `config/with_denoise.toml` | Adds a noise-reduction node. **Measured worse** than `default` — see the architecture doc. |
| `config/bilingual.toml` | Vietnamese **and** Chinese from a single correction pass. |
| `config/video.toml` | File / URL / system audio instead of a microphone. |
| `config/mock.toml` | No models at all. Used by CI. |

```bash
python -m livesub run -c config/bilingual.toml
python -m livesub run -c config/no_correct.toml --target zh
```

Inspect a wiring without opening a device or loading a model:

```bash
python -m livesub graph -c config/default.toml
python -m livesub graph -c config/default.toml --mermaid   # diagram for the report
```

There is also a shorthand where **omitting a stage name skips that stage**:

```bash
python -m livesub run --chain mic,segment,asr,correct,translate --target vi
python -m livesub run --chain mic,segment,asr,translate --target vi   # no correct
```

---

## Every module runs on its own

Each module has its own command line and can be tested in isolation.

```bash
# input
python -m livesub.input list-devices
python -m livesub.input mic --seconds 5 --out clip.wav
python -m livesub.input ffmpeg lecture.mp4 --out clip.wav

# noise reduction
python -m livesub.denoise compare --speech assets/lecture.wav --noise assets/classroom_noise.wav --snr 5
python -m livesub.denoise mix-noise --speech a.wav --noise b.wav --snr 5 --out noisy.wav

# transcription
python -m livesub.transcribe run faster_whisper --in assets/lecture.wav --text-only
python -m livesub.transcribe devices          # which backends are installed

# correction
python -m livesub.correct run rules --text "grade ee ent dissent uses back propagation"
python -m livesub.correct bench ollama        # latency + what it changes

# translation
python -m livesub.translate run ollama --target vi --text "Hello class"
python -m livesub.translate run mlx_llm --target vi --text "Hello class"   # Apple Silicon
python -m livesub.translate run ollama --target vi --repair --text "we use a for loop hear"
python -m livesub.translate compare --target vi    # split vs repair-while-translating
```

And they compose as ordinary Unix processes — audio stages speak raw `f32le` PCM, text
stages speak JSON Lines:

```bash
python -m livesub.input mic --raw \
  | python -m livesub.denoise run spectral --raw \
  | python -m livesub.transcribe run faster_whisper --raw \
  | python -m livesub.correct run ollama \
  | python -m livesub.translate run ollama --target vi --text-only
```

Drop the correction stage from that pipe and the translator picks up the job:

```bash
python -m livesub.input mic --raw \
  | python -m livesub.transcribe run faster_whisper --raw \
  | python -m livesub.translate run ollama --target vi --repair --text-only
```

---

## Attaching a user interface

The pipeline serves every subtitle event over a WebSocket (`ws://localhost:8765` in
`config/default.toml`). A UI subscribes and applies one rule:

> Keep a block per `segment_id`. When a message arrives whose `revision` is greater than
> or equal to the one held for that `segment_id` on this topic, replace that line. Leave
> every other block alone. The topic the event arrived on tells you what the text
> represents (raw ASR, corrected English, translation).

```json
{"type": "subtitle", "segment_id": "u0007", "revision": 2,
 "lang": "vi", "text": "...", "is_final": true, "end_to_end_ms": 2140,
 "stage_latency_ms": {"asr": 310, "fused_llm": 980}}
```

---

## Measuring it

```bash
# word error rate + latency, on a fixture so two configs are comparable
python -m livesub bench -c config/default.toml \
    --in assets/lecture.wav --reference assets/lecture.txt

# under classroom babble at a known SNR
python -m livesub bench -c config/default.toml \
    --in assets/lecture.wav --reference assets/lecture.txt \
    --noise assets/classroom_noise.wav --snr 5
```

Measured results and the reasoning behind each choice are in
[docs/architecture.md](docs/architecture.md).

```bash
python -m pytest -q      # 62 tests, no models or microphone needed
```

---

## Swapping an implementation

Change a string. The default is the portable stack; to use the Metal transcriber and
in-process MLX models instead:

```toml
[[node]]
name = "asr"
impl = "mlx_whisper"    # was faster_whisper
in   = "utterance.speech"
out  = "text.raw"
```

or from the command line:

```bash
python -m livesub run -c config/default.toml --transcribe mlx_whisper --translate mlx_llm_translator --correct mlx_llm_corrector
```

(`config/mlx.toml` is exactly that, as a file.)

Available everywhere: `python -m livesub list`

| Module kind | Implementations |
| --- | --- |
| input (source) | `mic`, `ffmpeg`, `wav`, `stdin` |
| denoiser | `passthrough_denoiser`, `highpass_gate`, `spectral`, `noisereduce`, `deepfilternet` |
| segmenter | `energy`, `silero` |
| transcriber | `mlx_whisper`, `faster_whisper`, `mock_transcriber` |
| corrector | `mlx_llm_corrector`, `ollama_corrector`, `rules`, `cloud_llm_corrector`, `passthrough_corrector` |
| translator | `mlx_llm_translator`, `ollama_translator`, `cloud_llm_translator`, `mock_translator` |
| fused | `fused_llm` (MLX) / `fused_ollama` (Ollama) — corrects and translates in one LLM call |
| sink | `stdout_pretty`, `jsonl`, `websocket_server`, `collect` |

The **Ollama** adapters are the default wiring: they need `requirements-cpu.txt` (which
also carries faster-whisper, the default ASR) and a running Ollama server, but no API key.
The cloud **LLM** adapters (`cloud_llm`, for
correction and translation) need `requirements-cloud.txt` and an API key, and are off by
default. Either way no lecture content leaves the machine. There is no cloud
speech-to-text adapter: transcription is local only, which is the stage where the raw
audio lives.

---

## Adding your own

Subclass `Module` from `livesub/core/interfaces.py`, declare your port types, add one
line to `livesub/core/registry.py`, name it in a config. Nothing else changes.

```python
from livesub.core.interfaces import Module
from livesub.core.types import TextFrame

class MyTranslator(Module):
    inputs  = {"text_in": TextFrame}
    outputs = {"text_out": TextFrame}

    def __init__(self, target: str = "vi"):
        super().__init__()
        self.target = target

    async def process(self, frame: TextFrame) -> TextFrame:
        from dataclasses import replace
        translated = _my_translation(frame.text, self.target)
        return replace(frame, text=translated, lang=self.target)
        # Note: do not set frame.lineage.revision — the bus assigns it on publish
```

---

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Level meter never moves | Terminal lacks microphone permission (System Settings → Privacy & Security). |
| `no input device matching ...` | Run `livesub devices`; match on any part of the name. |
| PowerShell refuses `.venv\Scripts\Activate.ps1` ("running scripts is disabled") | `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`, then retry. |
| `ffmpeg not found on PATH` (Windows) | `winget install ffmpeg`, reopen the terminal. |
| dshow device not found (Windows) | The name must match *Settings → Sound → Playback* exactly, or use the short form `audio=0` with `-f dshow`; run `ffmpeg -list_devices true -f dshow -i dummy` to list names. |
| Subtitles appear during silence | Whisper hallucinating on noise. Try `--denoise spectral`, or raise `start_db` on the segmenter. |
| Sentences cut mid-word | Segmenter never sees a pause. Lower `silence_ms` in the VAD node options. |
| `audio frames dropped under backpressure` | A stage cannot keep up. Use a smaller model, or the fused wiring. |
| `needs a package that is not installed` | The message names the requirements file to install. |

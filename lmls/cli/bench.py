"""``lmls bench`` -- replay a fixture through a wiring and measure it.

Produces the spec's "evidence of testing": word error rate against a reference
transcript, per-stage service times, and end-to-end delay from end-of-speech to display,
all on a file so two configurations can be compared on byte-identical input. A microphone
cannot do that, which is why every measurement here uses replay.

Two modes, and the difference matters when reading the numbers:

``--realtime`` (default) paces the file at wall-clock speed, so the pipeline experiences
the same arrival rate it would live and the latency figures mean what they say.

``--no-realtime`` pushes audio as fast as it is consumed. Throughput and WER stay valid;
the *latency* column becomes meaningless, because "end of speech" is no longer a real
moment in time. It is there to make WER sweeps quick, not to report delay.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Optional

import typer

from ..core.audioutil import load_wav, mix_at_snr, save_wav
from ..core.config import GraphConfig, load_config
from ..core.graph import Graph
from ..core.registry import resolve
from ..core.types import SAMPLE_RATE


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, spell out small numerals.

    Word error rate is meant to measure recognition, not formatting. Whisper writes
    "Chapter 4" where the reference says "chapter four"; counting that as two errors
    would say more about the comparison than about the model.
    """
    numbers = {
        "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
        "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten",
    }
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    words = [numbers.get(w, w) for w in text.split()]
    return " ".join(words)


def word_error_rate(reference: str, hypothesis: str) -> dict[str, Any]:
    import jiwer

    ref, hyp = normalise(reference), normalise(hypothesis)
    out = jiwer.process_words(ref, hyp)
    return {
        "wer": round(out.wer * 100, 2),
        "substitutions": out.substitutions,
        "deletions": out.deletions,
        "insertions": out.insertions,
        "ref_words": len(ref.split()),
    }


def _retarget(cfg: GraphConfig, audio: Path, realtime: bool) -> GraphConfig:
    """Replace the graph's input with a wav replay of ``audio`` and add collectors.

    Every other node is left exactly as the preset declares it, so what is measured is
    the preset, not a special benchmark pipeline. Two collectors are added: one on every
    text topic (what the viewer ends up seeing) and one on the raw ASR topic alone, so
    the uncorrected transcript can be scored separately from the displayed one.
    """
    for node in list(cfg.nodes):
        if not node.inputs:  # a source: declares no input ports
            node.impl = "wav"
            node.outputs = {"audio": "audio.raw"}
            node.inputs = {}
            node.options = {"path": str(audio), "realtime": realtime}
        if not node.outputs:  # a sink
            node.enabled = False  # no terminal repainting or websocket during a bench

    if not realtime:
        # Without wall-clock pacing the file outruns the ASR immediately, and dropping
        # stale frames would throw away most of the recording -- which would corrupt the
        # word error rate, not just the latency. A file can wait; make it.
        cfg.settings["audio_backpressure"] = "block"

    # Discover topics by payload type, never by name: a topic's name means nothing.
    from ..core.types import TextFrame

    topic_types: dict[str, type] = {}
    for n in cfg.active:
        cls = resolve(n.impl)
        for port_name, topic in n.outputs.items():
            topic_types.setdefault(topic, cls.outputs[port_name])
    text_topics = sorted(t for t, tt in topic_types.items() if tt is TextFrame)
    asr_topics = sorted(
        {t for n in cfg.active if n.impl in _TRANSCRIBE_IMPLS for t in n.out_topics}
    )
    from ..core.config import _parse_node

    cfg.nodes.append(
        _parse_node(
            {"name": "_collect", "impl": "collect", "in": text_topics},
            len(cfg.nodes),
        )
    )
    if asr_topics:
        cfg.nodes.append(
            _parse_node(
                {"name": "_collect_raw", "impl": "collect", "in": asr_topics},
                len(cfg.nodes) + 1,
            )
        )
    return cfg


#: Impl names grouped the way the CLI flags think about them. This is CLI ergonomics,
#: not architecture: the graph itself routes purely by port declarations.
_INPUT_IMPLS = {"mic", "ffmpeg", "wav", "stdin"}
_DENOISE_IMPLS = {
    "passthrough_denoiser", "highpass_gate", "spectral", "noisereduce",
    "deepfilternet",
}
_TRANSCRIBE_IMPLS = {"mock_transcriber", "mlx_whisper", "faster_whisper"}
_CORRECT_IMPLS = {
    "passthrough_corrector", "rules", "mlx_llm_corrector", "ollama_corrector",
    "cloud_llm_corrector",
}
_TRANSLATE_IMPLS = {
    "mock_translator", "mlx_llm_translator", "ollama_translator",
    "cloud_llm_translator",
}


def register(app: typer.Typer) -> None:
    @app.command()
    def bench(
        config: Path = typer.Option(..., "--config", "-c", help="Wiring to measure."),
        infile: Path = typer.Option(..., "--in", help="Audio fixture to replay."),
        reference: Optional[Path] = typer.Option(
            None, help="Reference transcript, for word error rate."
        ),
        noise: Optional[Path] = typer.Option(None, help="Noise bed to mix in first."),
        snr: float = typer.Option(5.0, help="SNR for --noise, in dB."),
        realtime: bool = typer.Option(
            True, help="Pace the file at wall-clock speed. Required for valid latency."
        ),
        target: Optional[str] = typer.Option(None, help="Target language override."),
        timeout: float = typer.Option(600.0, help="Give up after N seconds."),
        json_out: Optional[Path] = typer.Option(
            None, "--json", help="Write the full result as JSON."
        ),
        show_text: bool = typer.Option(True, help="Print the transcript produced."),
    ) -> None:
        """Measure a wiring on a fixture: WER, per-stage latency, end-to-end delay."""
        audio = infile
        if noise is not None:
            mixed = Path(".bench_noisy.wav")
            save_wav(
                mixed,
                mix_at_snr(load_wav(infile), load_wav(noise), snr),
                SAMPLE_RATE,
            )
            audio = mixed
            typer.secho(f"mixed {noise.name} at {snr:+.0f} dB SNR", fg=typer.colors.BLUE)

        cfg = load_config(config)
        if target:
            for node in cfg.nodes:
                if node.impl in _TRANSLATE_IMPLS or node.impl.startswith("fused"):
                    node.options["target"] = target
        cfg = _retarget(cfg, audio, realtime)

        graph = Graph(cfg)
        collector = next(n.stage for n in graph.nodes if n.config.name == "_collect")

        typer.secho(f"\nrunning {config.name} on {audio.name}"
                    f"{' (realtime)' if realtime else ' (as fast as possible)'}...",
                    fg=typer.colors.BLUE)
        asyncio.run(graph.run(timeout=timeout))

        result: dict[str, Any] = {
            "config": str(config),
            "audio": str(audio),
            "realtime": realtime,
            "nodes": {n.config.name: n.stage.describe() for n in graph.nodes},
            "metrics": graph.metrics.summary(),
            "bus": graph.bus.report(),
        }

        english = collector.text("corrected") or collector.text("raw")
        raw_text = collector.text("raw")
        result["transcript"] = english
        result["raw_transcript"] = raw_text

        if show_text:
            typer.secho("\nEnglish (as displayed):", bold=True)
            for event in collector.finals("corrected") or collector.finals("raw"):
                flag = " *" if event.meta.get("corrected") else ""
                typer.echo(f"  {event.text}{flag}")
            translations = collector.finals("translated")
            if translations:
                typer.secho("\nTranslation:", bold=True)
                for event in translations:
                    typer.secho(f"  [{event.lang}] {event.text}", fg=typer.colors.CYAN)

        if reference is not None:
            ref_text = reference.read_text()
            result["wer"] = {
                "displayed": word_error_rate(ref_text, english),
                "raw_asr": word_error_rate(ref_text, raw_text),
            }
            typer.secho("\nword error rate vs reference:", bold=True)
            for label, stats in result["wer"].items():
                typer.echo(
                    f"  {label:<12} {stats['wer']:>6.2f}%   "
                    f"sub {stats['substitutions']}  del {stats['deletions']}  "
                    f"ins {stats['insertions']}  ({stats['ref_words']} ref words)"
                )
            delta = result["wer"]["raw_asr"]["wer"] - result["wer"]["displayed"]["wer"]
            verdict = (
                f"correction improved WER by {delta:.2f} points"
                if delta > 0
                else f"correction did NOT improve WER ({delta:+.2f} points)"
            )
            typer.secho(f"  {verdict}", fg=typer.colors.GREEN if delta > 0
                        else typer.colors.YELLOW)

        typer.secho(graph.metrics.format_table())
        if not realtime:
            typer.secho(
                "  (latency figures are meaningless without --realtime)",
                fg=typer.colors.YELLOW,
            )
        dropped = graph.bus.total_dropped()
        if dropped:
            typer.secho(
                f"\n  {dropped} audio frames dropped under backpressure "
                f"({dropped * 20 / 1000:.1f}s of audio).",
                fg=typer.colors.YELLOW,
            )

        if json_out is not None:
            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(json.dumps(result, indent=2, ensure_ascii=False,
                                           default=str))
            typer.secho(f"\nwrote {json_out}", fg=typer.colors.GREEN)

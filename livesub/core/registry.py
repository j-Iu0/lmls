"""Name -> implementation lookup, resolved lazily.

Every implementation is listed here as a dotted path string rather than an import, so
that ``import livesub`` never pulls in mlx, torch or an HTTP client. A missing optional
dependency surfaces only when you actually ask for that implementation, and it surfaces
as an instruction rather than a traceback:

    $ livesub run --config config/default.toml
    implementation 'mlx_whisper' needs a package that is not installed:
    No module named 'mlx_whisper'
      try: .venv/bin/pip install -r requirements-mlx.txt

This is what keeps ``mock.toml`` runnable on a machine with no models at all.

The registry is flat: one impl name -> one class. There is no ``kind`` dimension --
roles are expressed by a module's port declarations, not by which sub-table it sits in.
Names that would collide across the old kinds are disambiguated with a suffix
(``passthrough_denoiser`` vs ``passthrough_corrector``, ``ollama_corrector`` vs
``ollama_translator``).
"""

from __future__ import annotations

import importlib
from typing import Any

#: impl name -> "module.path:ClassName"
REGISTRY: dict[str, str] = {
    # inputs
    "mic": "livesub.input.mic:MicSource",
    "ffmpeg": "livesub.input.ffmpeg_source:FfmpegSource",
    "wav": "livesub.input.wav_replay:WavReplaySource",
    "stdin": "livesub.input.stdin_source:StdinPcmSource",
    # denoisers
    "passthrough_denoiser": "livesub.denoise.passthrough:PassthroughDenoiser",
    "highpass_gate": "livesub.denoise.highpass_gate:HighpassGateDenoiser",
    "spectral": "livesub.denoise.spectral:SpectralDenoiser",
    "noisereduce": "livesub.denoise.noisereduce_offline:NoiseReduceDenoiser",
    "deepfilternet": "livesub.denoise.deepfilternet:DeepFilterNetDenoiser",
    # segmenters
    "energy": "livesub.segment.energy:EnergySegmenter",
    "silero": "livesub.segment.silero:SileroSegmenter",
    # transcribers
    "mock_transcriber": "livesub.transcribe.mock:MockTranscriber",
    "mlx_whisper": "livesub.transcribe.mlx_whisper:MlxWhisperTranscriber",
    "faster_whisper": "livesub.transcribe.faster_whisper:FasterWhisperTranscriber",
    # correctors
    "passthrough_corrector": "livesub.correct.passthrough:PassthroughCorrector",
    "rules": "livesub.correct.rules:RuleCorrector",
    "mlx_llm_corrector": "livesub.correct.mlx_llm:MlxLlmCorrector",
    "ollama_corrector": "livesub.correct.ollama_llm:OllamaLlmCorrector",
    "cloud_llm_corrector": "livesub.correct.cloud_llm:CloudLlmCorrector",
    # translators
    "mock_translator": "livesub.translate.mock:MockTranslator",
    "mlx_llm_translator": "livesub.translate.mlx_llm:MlxLlmTranslator",
    "ollama_translator": "livesub.translate.ollama_llm:OllamaLlmTranslator",
    "cloud_llm_translator": "livesub.translate.cloud_llm:CloudLlmTranslator",
    # fused
    "fused_llm": "livesub.fused.llm_correct_translate:FusedLlmStage",
    "fused_ollama": "livesub.fused.ollama_llm:OllamaFusedStage",
    # sinks
    "collect": "livesub.sink.collect:CollectSink",
    "stdout_pretty": "livesub.sink.stdout_pretty:PrettyStdoutSink",
    "jsonl": "livesub.sink.jsonl:JsonlSink",
    "websocket_server": "livesub.sink.websocket_server:WebSocketSink",
}

#: Hint shown when an implementation's dependency is missing.
_INSTALL_HINTS = {
    "mlx_whisper": "requirements-mlx.txt",
    "mlx_llm_corrector": "requirements-mlx.txt",
    "mlx_llm_translator": "requirements-mlx.txt",
    "fused_llm": "requirements-mlx.txt",
    "fused_ollama": "requirements-cpu.txt",
    "faster_whisper": "requirements-cpu.txt",
    "ollama_corrector": "requirements-cpu.txt",
    "ollama_translator": "requirements-cpu.txt",
    "cloud_llm_corrector": "requirements-cloud.txt",
    "cloud_llm_translator": "requirements-cloud.txt",
    "deepfilternet": "requirements-extra.txt (pip install deepfilternet)",
    "silero": "pip install silero-vad onnxruntime",
}


class UnknownImplementation(KeyError):
    pass


class MissingDependency(RuntimeError):
    pass


def available() -> list[str]:
    return sorted(REGISTRY)


def resolve(impl: str) -> type:
    """Import and return the class for ``impl``."""
    try:
        path = REGISTRY[impl]
    except KeyError as exc:
        known = ", ".join(available()) or "(none)"
        raise UnknownImplementation(
            f"unknown implementation {impl!r}; available: {known}"
        ) from exc

    module_path, _, class_name = path.partition(":")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        hint = _INSTALL_HINTS.get(impl)
        extra = f"\n  try: .venv/bin/pip install -r {hint}" if hint else ""
        raise MissingDependency(
            f"implementation {impl!r} needs a package that is not installed: "
            f"{exc}{extra}"
        ) from exc
    return getattr(module, class_name)


def build(impl: str, name: str = "", **kwargs: Any) -> Any:
    """Instantiate an implementation with the node's config keyword arguments."""
    cls = resolve(impl)
    try:
        obj = cls(**kwargs)
    except TypeError as exc:
        raise TypeError(
            f"could not construct {impl!r} with options {sorted(kwargs)}: {exc}"
        ) from exc
    obj.name = name or impl
    return obj
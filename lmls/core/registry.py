"""Name -> implementation lookup, resolved lazily.

Every implementation is listed here as a dotted path string rather than an import, so
that ``import lmls`` never pulls in mlx, torch or an HTTP client. A missing optional
dependency surfaces only when you actually ask for that implementation, and it surfaces
as an instruction rather than a traceback:

    $ lmls run --config config/default.toml
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
import inspect
from typing import Any

#: impl name -> "module.path:ClassName"
REGISTRY: dict[str, str] = {
    # inputs
    "mic": "lmls.input.mic:MicSource",
    "ffmpeg": "lmls.input.ffmpeg_source:FfmpegSource",
    "media": "lmls.input.media:ControlledMediaSource",
    "wav": "lmls.input.wav_replay:WavReplaySource",
    "stdin": "lmls.input.stdin_source:StdinPcmSource",
    # denoisers
    "passthrough_denoiser": "lmls.denoise.passthrough:PassthroughDenoiser",
    "highpass_gate": "lmls.denoise.highpass_gate:HighpassGateDenoiser",
    "spectral": "lmls.denoise.spectral:SpectralDenoiser",
    "noisereduce": "lmls.denoise.noisereduce_offline:NoiseReduceDenoiser",
    "deepfilternet": "lmls.denoise.deepfilternet:DeepFilterNetDenoiser",
    # segmenters
    "energy": "lmls.segment.energy:EnergySegmenter",
    "silero": "lmls.segment.silero:SileroSegmenter",
    # transcribers
    "mock_transcriber": "lmls.transcribe.mock:MockTranscriber",
    "mlx_whisper": "lmls.transcribe.mlx_whisper:MlxWhisperTranscriber",
    "faster_whisper": "lmls.transcribe.faster_whisper:FasterWhisperTranscriber",
    # correctors
    "passthrough_corrector": "lmls.correct.passthrough:PassthroughCorrector",
    "rules": "lmls.correct.rules:RuleCorrector",
    "mlx_llm_corrector": "lmls.correct.mlx_llm:MlxLlmCorrector",
    "ollama_corrector": "lmls.correct.ollama_llm:OllamaLlmCorrector",
    "cloud_llm_corrector": "lmls.correct.cloud_llm:CloudLlmCorrector",
    # translators
    "mock_translator": "lmls.translate.mock:MockTranslator",
    "mlx_llm_translator": "lmls.translate.mlx_llm:MlxLlmTranslator",
    "ollama_translator": "lmls.translate.ollama_llm:OllamaLlmTranslator",
    "cloud_llm_translator": "lmls.translate.cloud_llm:CloudLlmTranslator",
    # fused
    "fused_llm": "lmls.fused.llm_correct_translate:FusedLlmStage",
    "fused_ollama": "lmls.fused.ollama_llm:OllamaFusedStage",
    # sinks
    "collect": "lmls.sink.collect:CollectSink",
    "stdout_pretty": "lmls.sink.stdout_pretty:PrettyStdoutSink",
    "jsonl": "lmls.sink.jsonl:JsonlSink",
    "websocket_server": "lmls.sink.websocket_server:WebSocketSink",
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
    "silero": "requirements-mlx.txt",
}


class UnknownImplementation(KeyError):
    pass


class MissingDependency(RuntimeError):
    pass


def available() -> list[str]:
    return sorted(REGISTRY)


def option_parameters(cls: type) -> dict[str, inspect.Parameter]:
    """Public keyword options, including explicitly declared forwarded options.

    Configuration objects themselves are implementation details: when their type
    is also an option source, expose their fields instead of the wrapper argument.
    Explicit constructor parameters take precedence over forwarded defaults.
    """
    sources = getattr(cls, 'option_sources', ())
    result = {}
    for source in (*sources, cls):
        for name, param in inspect.signature(source).parameters.items():
            if param.kind not in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
                continue
            annotation = str(param.annotation)
            if any(t.__name__ in annotation for t in sources):
                continue
            result[name] = param
    return result


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

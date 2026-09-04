"""Noise-reduction module -- optional in the graph.

Every implementation is frame-in, frame-out with the length and format unchanged, which
is exactly why the stage can be deleted from the wiring: nothing downstream can tell
whether it ran.

Each adapter reports its own ``latency_ms``, because that number is a real charge against
the spec's three-second budget and belongs in the report:

    passthrough    0 ms    control condition
    highpass_gate  0 ms    causal; kills rumble and fan hum
    spectral      20 ms    streaming STFT gate; the default
    noisereduce  480 ms    whole-signal reference; offline comparison only
    deepfilternet 80 ms    best on babble; needs torch

Run standalone::

    python -m livesub.denoise spectral --in noisy.wav --out clean.wav --report-snr
    python -m livesub.denoise mix-noise --speech a.wav --noise b.wav --snr 5 --out c.wav
    python -m livesub.denoise compare --speech a.wav --noise b.wav --snr 5
"""

from .highpass_gate import HighpassGateDenoiser
from .noisereduce_offline import NoiseReduceDenoiser
from .passthrough import PassthroughDenoiser
from .spectral import SpectralDenoiser

__all__ = [
    "HighpassGateDenoiser",
    "NoiseReduceDenoiser",
    "PassthroughDenoiser",
    "SpectralDenoiser",
]

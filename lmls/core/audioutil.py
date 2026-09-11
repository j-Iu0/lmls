"""Small audio helpers shared by the modules.

Kept in ``core`` because input, denoise and transcribe all need them, and a module is not
allowed to import a sibling module.
"""

from __future__ import annotations

from math import gcd
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

from .types import SAMPLE_RATE


def to_mono(pcm: np.ndarray) -> np.ndarray:
    """Average multi-channel audio down to one channel."""
    if pcm.ndim == 1:
        return pcm.astype(np.float32, copy=False)
    return pcm.mean(axis=1).astype(np.float32)


def resample(pcm: np.ndarray, src_rate: int, dst_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Polyphase resample. Used to get device audio (usually 48 kHz) to Whisper's 16 kHz.

    ``resample_poly`` applies an anti-aliasing filter as part of the decimation, which
    naive slicing (``pcm[::3]``) does not -- that shortcut aliases higher formants down
    into the speech band and measurably hurts word error rate.
    """
    if src_rate == dst_rate:
        return pcm.astype(np.float32, copy=False)
    g = gcd(int(src_rate), int(dst_rate))
    return resample_poly(pcm, dst_rate // g, src_rate // g).astype(np.float32)


def load_wav(path: str | Path, target_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Read any libsndfile-readable file to mono float32 at ``target_rate``."""
    import soundfile as sf

    pcm, rate = sf.read(str(path), dtype="float32", always_2d=False)
    return resample(to_mono(pcm), rate, target_rate)


def save_wav(path: str | Path, pcm: np.ndarray, rate: int = SAMPLE_RATE) -> None:
    import soundfile as sf

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), pcm.astype(np.float32), rate)


def rms(pcm: np.ndarray) -> float:
    if len(pcm) == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(pcm, dtype=np.float64))))


def db(value: float) -> float:
    return 20 * float(np.log10(max(value, 1e-12)))


def snr_db(signal: np.ndarray, noise: np.ndarray) -> float:
    """Signal-to-noise ratio in dB, for the noise-condition test evidence."""
    return db(rms(signal)) - db(rms(noise))


def find_lag(reference: np.ndarray, signal: np.ndarray, max_lag: int) -> int:
    """Samples by which ``signal`` trails ``reference``, by cross-correlation.

    Needed before any sample-by-sample comparison of a processed signal against its
    source: every denoiser here delays the audio (the streaming spectral gate by one
    20 ms hop, the ``noisereduce`` adapter by its whole 480 ms buffer, the IIR high-pass
    by a frequency-dependent phase). Subtracting an unaligned signal measures the delay,
    not the distortion, and makes a good denoiser look catastrophic.
    """
    n = min(len(reference), len(signal))
    if n == 0:
        return 0
    a = reference[:n] - reference[:n].mean()
    b = signal[:n] - signal[:n].mean()
    max_lag = int(min(max_lag, n - 1))
    if max_lag <= 0:
        return 0
    corr = np.correlate(b, a[: n - max_lag], mode="valid")[: max_lag + 1]
    return int(np.argmax(corr)) if len(corr) else 0


def align(reference: np.ndarray, signal: np.ndarray, max_lag_ms: float = 800.0
          ) -> tuple[np.ndarray, np.ndarray, int]:
    """Trim ``signal``'s leading delay so it lines up with ``reference``.

    Returns ``(reference, aligned_signal, lag_samples)`` cropped to a common length.
    """
    lag = find_lag(reference, signal, int(SAMPLE_RATE * max_lag_ms / 1000))
    shifted = signal[lag:]
    n = min(len(reference), len(shifted))
    return reference[:n], shifted[:n], lag


def mix_at_snr(
    speech: np.ndarray, noise: np.ndarray, target_snr_db: float
) -> np.ndarray:
    """Add ``noise`` to ``speech`` scaled to hit a target SNR.

    This is how the noisy classroom test fixtures are built: one clean recording plus a
    babble/fan recording mixed at a known, repeatable SNR, so denoise-on vs denoise-off
    word error rates are comparable rather than anecdotal.
    """
    if len(noise) < len(speech):  # tile the noise bed to cover the speech
        noise = np.tile(noise, int(np.ceil(len(speech) / max(len(noise), 1))))
    noise = noise[: len(speech)]
    speech_rms, noise_rms = rms(speech), rms(noise)
    if noise_rms == 0:
        return speech.astype(np.float32)
    scale = speech_rms / (noise_rms * (10 ** (target_snr_db / 20)))
    mixed = speech + noise * scale
    peak = float(np.max(np.abs(mixed))) if len(mixed) else 0.0
    if peak > 1.0:  # avoid clipping, keep the SNR intact
        mixed = mixed / peak
    return mixed.astype(np.float32)

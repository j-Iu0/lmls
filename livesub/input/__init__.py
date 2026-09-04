"""Input module -- audio capture.

Every source normalises to the one canonical format (16 kHz mono float32, 20 ms frames)
so that no downstream stage can tell a microphone from a video file from a network
stream. Implementations are resolved through ``livesub.core.registry``; importing this
package does not import ``sounddevice`` or spawn ffmpeg.

Run standalone::

    python -m livesub.input list-devices
    python -m livesub.input mic --seconds 5 --out clip.wav
    python -m livesub.input ffmpeg --url lecture.mp4 --out clip.wav
    python -m livesub.input mic --raw | ...
"""

from .ffmpeg_source import FfmpegSource
from .mic import MicSource
from .stdin_source import StdinPcmSource
from .wav_replay import WavReplaySource

__all__ = ["FfmpegSource", "MicSource", "StdinPcmSource", "WavReplaySource"]

"""Transcription module -- utterances in, raw English ``TextFrame``s out.

A segmenter node upstream cuts the audio stream into ``Utterance``s; each adapter here
decodes one utterance per ``process`` call and returns zero or more text frames. The
one-shot ``transcribe_array`` helpers keep an internal segmenter for the standalone CLI
and benchmark paths.

    mock_transcriber     no model, scripted, deterministic -- CI and demos without a download
    mlx_whisper          default here: Whisper on the M1 GPU through Metal
    faster_whisper       portable CPU fallback (CTranslate2 has no Metal backend)
    deepgram             optional continuous cloud stream (raw AudioFrames, no segmenter)

Transcription is local by default.  Selecting Deepgram is an explicit privacy boundary:
raw lecture audio leaves the machine, so the shipped default remains Whisper.

Run standalone::

    python -m lmls.transcribe run mlx_whisper --in assets/lecture.wav
    python -m lmls.transcribe run mock --in assets/lecture.wav
    ... | python -m lmls.transcribe run mlx_whisper --raw
"""

from .mock import MockTranscriber

__all__ = ["MockTranscriber"]

"""Scripted transcriber -- no model, no download, no network.

This is what lets the graph, the sinks, the CLIs and the whole test suite run on a
machine that has never downloaded a model, and what makes end-to-end tests deterministic:
a real ASR returns slightly different text run to run, so nothing built on it could be
asserted exactly.

It is also the honest way to demonstrate the correction stage. The default script
contains realistic Whisper-style errors -- homophones and mis-split compounds, which is
what Whisper actually gets wrong on lecture audio -- so ``correct`` and the repair-mode
translator have something genuine to fix during a demo:

    "the mitochondria is the power house of the sell"  -> "... of the cell"
    "we use grade ee ent dissent to train the model"   -> "gradient descent"

``delay_ms`` simulates model latency so latency plumbing can be exercised without a GPU.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from ..core.interfaces import Module
from ..core.types import Lineage, TextFrame, Utterance

#: Deliberately contains the kinds of errors real Whisper output contains.
DEFAULT_SCRIPT = [
    "good morning everyone and welcome back to the coarse",
    "today we are going to talk about neural net works and how they learn",
    "a neural network is made of layers of simple units called neurons",
    "each neuron takes several inputs multiplies them by weights and adds a bias",
    "the result is passed through an activation function such as a rectified linear unit",
    "during training we compare the prediction with the correct answer",
    "the difference between them is called the loss",
    "grade ee ent dissent then adjusts every weight to make the loss smaller",
    "this process repeats for many eepocks until the model converges",
    "please read chapter for before the next lecture on thursday",
]


class MockTranscriber(Module):
    """Args:
        script: path to a text file, one line per utterance. Defaults to the built-in
            script above.
        delay_ms: pretend inference time, so latency accounting is exercised.
        loop: restart the script when it runs out, for long soak runs.
    """

    inputs: ClassVar = {"audio": Utterance}
    outputs: ClassVar = {"text": TextFrame}

    def __init__(
        self,
        script: str | Path | None = None,
        delay_ms: float = 120.0,
        loop: bool = True,
        **_: Any,
    ):
        super().__init__()
        if script is not None:
            lines = [
                line.strip()
                for line in Path(script).read_text().splitlines()
                if line.strip()
            ]
        else:
            lines = list(DEFAULT_SCRIPT)
        self.script = lines
        self.delay_ms = delay_ms
        self.loop = loop
        self._index = 0

    def _next_line(self, is_final: bool) -> str:
        if not self.script:
            return ""
        if self._index >= len(self.script):
            if not self.loop:
                return ""
            self._index = 0
        line = self.script[self._index]
        if is_final:
            self._index += 1
        else:
            # A partial is the opening of the line that is about to be finalised, which
            # is how a real streaming ASR behaves.
            words = line.split()
            line = " ".join(words[: max(1, len(words) * 2 // 3)])
        return line

    def _transcribe_pcm(self, pcm: np.ndarray, is_final: bool) -> str:
        if self.delay_ms:
            time.sleep(self.delay_ms / 1000)
        return self._next_line(is_final)

    async def process(self, utterance: Utterance) -> list[TextFrame]:
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(
            None, self._transcribe_pcm, utterance.pcm, utterance.is_final
        )
        if not text:
            return []
        # One utterance -> one frame. The lineage anchors end-to-end latency at the
        # moment the audio ended; the bus assigns the revision at publish time.
        lineage = replace(
            Lineage.new(segment_id=utterance.id), t_audio_end=utterance.t_end
        )
        return [
            TextFrame(
                text=text,
                lang="en",
                is_final=utterance.is_final,
                lineage=lineage,
                meta={"mock": True, "audio_s": round(utterance.duration, 2)},
            )
        ]

    def transcribe_array(self, pcm: np.ndarray, sample_rate: int) -> list[TextFrame]:
        """One-shot path used by the CLI and the benchmark. Not called by the graph."""
        text = self._transcribe_pcm(pcm, True)
        lineage = replace(Lineage.new(), t_audio_end=time.time())
        return [
            TextFrame(
                text=text,
                lang="en",
                lineage=lineage,
                meta={"mock": True, "audio_s": round(len(pcm) / sample_rate, 2)},
            )
        ]

    def describe(self) -> dict[str, Any]:
        return {
            "module": "MockTranscriber",
            "name": self.name,
            "lines": len(self.script),
            "delay_ms": self.delay_ms,
        }
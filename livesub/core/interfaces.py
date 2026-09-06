"""The one base class for every pipeline node.

There used to be an ABC per role (``AudioSource``, ``Denoiser``, ``Transcriber``,
``Corrector``, ``Translator``, ``SubtitleSink``). There is now a single :class:`Module`:
a node's *role* is expressed by its port declarations -- which payload types it reads
and writes -- not by which class hierarchy it sits in. The graph runner inspects the
``inputs``/``outputs`` declarations to wire subscriptions, validate types and pick the
right runner.

Nothing here knows about any other module: a translator does not know whether the text
it receives came from a corrector or straight from the ASR.
"""

from __future__ import annotations

from abc import ABC
from typing import Any, ClassVar

import numpy as np

from .startup import StartupCallback, StartupEvent, StartupPhase, StartupProgress
from .types import AudioFrame, FRAME_SAMPLES


class Module(ABC):
    """Base class for every pipeline node.

    Each subclass declares its port types as class-level dicts. The graph runner
    inspects these to wire subscriptions and validate types.

    Conventions the runner relies on:

    * ``inputs == {}`` -- a **source**; must implement ``async def run()`` yielding
      payloads for its output port(s).
    * ``outputs == {}`` -- a **sink**; implements ``async def process(frame) -> None``.
    * both non-empty -- a **transform**; implements ``process`` returning a single
      payload (1-to-1), a ``dict`` keyed by output port name (1-to-many), or a
      ``list`` for the single output port (1-to-list).
    * ``process`` may be a plain ``def``; the runner wraps it in ``run_in_executor``
      automatically. ``run`` must be ``async``.
    * Internal state (LLM context windows, DSP state) lives on the instance, never in
      method parameters.
    """

    #: {port_name: payload_type} -- what this module reads from the bus.
    #: Empty dict means this is a source (no inputs).
    inputs: ClassVar[dict[str, type]] = {}

    #: {port_name: payload_type} -- what this module writes to the bus.
    #: Empty dict means this is a sink (no outputs).
    outputs: ClassVar[dict[str, type]] = {}

    #: Optional destination for a bare result from a multi-output module. Named
    #: dictionary results still select their own ports. Topic names never select
    #: the default; unconnected default ports intentionally publish nothing.
    default_output: ClassVar[str | None] = None

    def __init__(self) -> None:
        # ``name`` is set by the registry after construction; initialised here as an
        # instance variable so subclasses that call super().__init__() see a real
        # string, not the class-level fallback.
        self.name: str = "unnamed"
        # Set by Graph before start() is called.  None means no listener.
        # May be called from a worker thread — callbacks must be thread-safe.
        self._startup_reporter: StartupCallback | None = None

    async def start(self) -> None:  # pragma: no cover - trivial default
        """Called once before data flows. Load models, open devices, bind ports."""
        return None

    def _report_startup(
        self,
        phase: StartupPhase,
        message: str = "",
        progress: StartupProgress | None = None,
    ) -> None:
        """Emit a :class:`~livesub.core.startup.StartupEvent` to the registered
        callback, if any.

        Call this from ``_load()`` or ``start()`` to report cold-start progress.
        Safe to call from a worker thread.
        """
        if self._startup_reporter is not None:
            self._startup_reporter(
                StartupEvent(
                    module_name=self.name,
                    phase=phase,
                    message=message,
                    progress=progress,
                )
            )

    async def stop(self) -> None:  # pragma: no cover - trivial default
        """Called after all data is processed. Release resources, flush buffers,
        cancel background tasks. Sinks move their former ``flush()`` logic here."""
        return None

    def drain(self) -> list[Any]:
        """Called once by the graph runner after the input stream is exhausted, before
        closing the output topic. Return any buffered payloads to publish.

        Segmenters use this to flush the utterance still being accumulated when the
        audio ends. The default is empty because most modules buffer nothing.
        """
        return []

    def describe(self) -> dict[str, Any]:
        """Short dict used by ``livesub graph`` and by the bench report."""
        return {
            "module": type(self).__name__,
            "name": self.name,
            "inputs": {k: v.__name__ for k, v in self.inputs.items()},
            "outputs": {k: v.__name__ for k, v in self.outputs.items()},
        }

    def process_array(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray:
        """Whole-signal convenience for frame-in/frame-out modules (denoisers).

        Not used by the graph; used by the CLI tools. The default walks the array
        through :meth:`process` frame by frame so an implementation only has to provide
        the streaming path.
        """
        out = np.empty_like(pcm, dtype=np.float32)
        for i in range(0, len(pcm), FRAME_SAMPLES):
            block = pcm[i : i + FRAME_SAMPLES].astype(np.float32)
            if len(block) < FRAME_SAMPLES:  # pad the tail, trim after processing
                padded = np.zeros(FRAME_SAMPLES, dtype=np.float32)
                padded[: len(block)] = block
                processed = self.process(
                    AudioFrame(padded, sample_rate, i)
                ).pcm
                out[i : i + len(block)] = processed[: len(block)]
            else:
                out[i : i + FRAME_SAMPLES] = self.process(
                    AudioFrame(block, sample_rate, i)
                ).pcm
        return out

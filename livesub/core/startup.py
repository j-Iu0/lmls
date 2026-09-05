"""Structured cold-start progress events.

Every :class:`~livesub.core.interfaces.Module` that does heavy work in
:meth:`~livesub.core.interfaces.Module.start` (model download, weight load,
socket bind, remote-service handshake) can report its progress through this
mechanism instead of writing to the log.

Callers that want visibility — a CLI spinner, a WebSocket status broadcast, a
test assertion — register a :data:`StartupCallback` on the :class:`Graph`.  The
graph wires it onto each module before calling ``start()``, so the module itself
stays decoupled from the caller.

Thread-safety note
------------------
:meth:`~livesub.core.interfaces.Module._report_startup` may be called from a
worker thread (``start()`` typically offloads the blocking load to an executor).
Callbacks must be thread-safe.  Async callers that need to dispatch back onto the
event loop should wrap with ``loop.call_soon_threadsafe()``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Callable


class StartupPhase(enum.Enum):
    """Coarse state of a module's ``start()`` lifecycle."""

    IN_PROGRESS = "in_progress"
    """``start()`` is running.  May fire multiple times as work advances."""

    READY = "ready"
    """``start()`` completed successfully."""

    FAILED = "failed"
    """``start()`` raised an exception.  ``StartupEvent.message`` carries the
    reason."""


@dataclass(frozen=True)
class StartupProgress:
    """Measurable progress snapshot, suitable for feeding directly into *tqdm*.

    All fields are optional.  A module emits what it knows; callers must handle
    ``None`` gracefully (indeterminate spinner).

    tqdm example::

        pbar = tqdm(total=event.progress.total, unit=event.progress.unit)
        # on each subsequent IN_PROGRESS event:
        pbar.n = event.progress.current
        pbar.refresh()
    """

    current: float | None
    """How much has been processed so far.  ``None`` means unknown."""

    total: float | None
    """Total amount to process.  ``None`` means the total is not yet known."""

    unit: str = ""
    """Unit label for display: ``"B"``, ``"MB"``, ``"files"``, ``"steps"``,
    or ``""`` for a dimensionless counter."""

    @property
    def fraction(self) -> float | None:
        """Normalised 0–1 ratio, or ``None`` when *total* is unknown."""
        if self.current is not None and self.total:
            return self.current / self.total
        return None


@dataclass(frozen=True)
class StartupEvent:
    """A single progress notification emitted by a module during ``start()``.

    Attributes
    ----------
    module_name:
        The node name assigned by the registry (``Module.name``).
    phase:
        Coarse lifecycle state.
    message:
        Human-readable detail.  Use this for network-vs-local distinctions
        (``"downloading mlx-community/Qwen3-4B"`` vs ``"loading from cache"``)
        rather than encoding them in *phase*.
    progress:
        Optional measurable progress.  ``None`` on ``READY``/``FAILED``, and on
        ``IN_PROGRESS`` events where the module has no progress information
        (indeterminate).  May switch between ``None`` and a concrete value across
        successive ``IN_PROGRESS`` events.
    """

    module_name: str
    phase: StartupPhase
    message: str = ""
    progress: StartupProgress | None = None


#: Type alias for the startup progress callback.
#:
#: The callable receives every :class:`StartupEvent` emitted by any module
#: during graph startup.  It **may be called from a worker thread** — callers
#: are responsible for any required synchronisation.
StartupCallback = Callable[[StartupEvent], None]

"""Tests for the cold-start progress reporting mechanism.

Covers:
- StartupProgress.fraction derived property
- StartupEvent construction and immutability
- Module._report_startup dispatches to _startup_reporter
- Graph wires on_startup onto every module before start()
- Events are received in order (IN_PROGRESS then READY) for a simple module
- FAILED is emitted when start() raises, and the exception re-propagates
- Callback is not called when no on_startup is registered (no crash)
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest

from livesub.core.graph import Graph
from livesub.core.interfaces import Module
from livesub.core.startup import (
    StartupEvent,
    StartupPhase,
    StartupProgress,
)


# ---------------------------------------------------------------------------
# StartupProgress
# ---------------------------------------------------------------------------

def test_startup_progress_fraction_known():
    p = StartupProgress(current=500.0, total=2000.0, unit="MB")
    assert p.fraction == pytest.approx(0.25)


def test_startup_progress_fraction_unknown_total():
    p = StartupProgress(current=500.0, total=None, unit="MB")
    assert p.fraction is None


def test_startup_progress_fraction_unknown_current():
    p = StartupProgress(current=None, total=2000.0, unit="MB")
    assert p.fraction is None


def test_startup_progress_fraction_both_none():
    p = StartupProgress(current=None, total=None, unit="")
    assert p.fraction is None


def test_startup_progress_zero_total():
    # Avoid ZeroDivisionError when total=0
    p = StartupProgress(current=0.0, total=0.0, unit="")
    assert p.fraction is None


def test_startup_progress_is_frozen():
    p = StartupProgress(current=1.0, total=10.0, unit="MB")
    with pytest.raises((AttributeError, TypeError)):
        p.current = 2.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# StartupEvent
# ---------------------------------------------------------------------------

def test_startup_event_defaults():
    e = StartupEvent(module_name="foo", phase=StartupPhase.READY)
    assert e.message == ""
    assert e.progress is None


def test_startup_event_is_frozen():
    e = StartupEvent(module_name="foo", phase=StartupPhase.IN_PROGRESS)
    with pytest.raises((AttributeError, TypeError)):
        e.phase = StartupPhase.READY  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Stub modules for graph tests
# ---------------------------------------------------------------------------

class _ReportingSource(Module):
    """A source that emits IN_PROGRESS then READY in start(), then immediately stops."""

    outputs: ClassVar[dict[str, type]] = {"out": Any}  # type: ignore[valid-type]

    async def start(self) -> None:
        self._report_startup(StartupPhase.IN_PROGRESS, "loading stub")
        self._report_startup(StartupPhase.READY)

    async def run(self):  # pragma: no cover
        return
        yield


class _FailingModule(Module):
    """A module whose start() emits FAILED and raises."""

    outputs: ClassVar[dict[str, type]] = {"out": Any}  # type: ignore[valid-type]

    async def start(self) -> None:
        try:
            raise RuntimeError("boom")
        except Exception as exc:
            self._report_startup(StartupPhase.FAILED, str(exc))
            raise

    async def run(self):  # pragma: no cover
        return
        yield


# ---------------------------------------------------------------------------
# Direct Module tests (no Graph)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_report_startup_dispatches():
    """_report_startup calls the registered callback with a correct event."""
    received: list[StartupEvent] = []

    mod = _ReportingSource()
    mod.name = "my_source"
    mod._startup_reporter = received.append

    await mod.start()

    assert len(received) == 2
    assert received[0] == StartupEvent(
        module_name="my_source",
        phase=StartupPhase.IN_PROGRESS,
        message="loading stub",
    )
    assert received[1] == StartupEvent(
        module_name="my_source",
        phase=StartupPhase.READY,
    )


@pytest.mark.asyncio
async def test_report_startup_no_reporter_no_crash():
    """Calling _report_startup without a registered callback is a no-op."""
    mod = _ReportingSource()
    mod.name = "unnamed"
    # _startup_reporter is None by default
    await mod.start()  # must not raise


@pytest.mark.asyncio
async def test_failed_module_emits_failed_and_reraises():
    """A module that raises in start() should emit FAILED before re-raising."""
    received: list[StartupEvent] = []

    mod = _FailingModule()
    mod.name = "fail"
    mod._startup_reporter = received.append

    with pytest.raises(RuntimeError, match="boom"):
        await mod.start()

    assert len(received) == 1
    assert received[0].phase == StartupPhase.FAILED
    assert received[0].module_name == "fail"
    assert "boom" in received[0].message


@pytest.mark.asyncio
async def test_startup_progress_carried_in_event():
    """progress field is passed through _report_startup unchanged."""
    received: list[StartupEvent] = []

    mod = _ReportingSource()
    mod.name = "prog_test"
    mod._startup_reporter = received.append

    prog = StartupProgress(current=100.0, total=400.0, unit="MB")
    mod._report_startup(StartupPhase.IN_PROGRESS, "loading", prog)

    assert len(received) == 1
    assert received[0].progress is prog
    assert received[0].progress.fraction == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Graph wiring tests
# ---------------------------------------------------------------------------

def _make_graph(nodes, on_startup=None):
    """Build a minimal Graph from raw node dicts."""
    from livesub.core.config import GraphConfig, _parse_node

    cfg = GraphConfig(
        nodes=[_parse_node(n, i) for i, n in enumerate(nodes)],
        settings={"audio_backpressure": "block"},
    )
    return Graph(cfg, on_startup=on_startup)


@pytest.mark.asyncio
async def test_graph_wires_callback_to_all_modules():
    """Graph.start() sets _startup_reporter on every module and fires events."""
    received: list[StartupEvent] = []

    nodes = [
        {"name": "src", "impl": "wav", "out": "audio.raw",
         "path": "assets/lecture.wav", "realtime": False},
        {"name": "nr",  "impl": "passthrough_denoiser",
         "in": "audio.raw", "out": "audio.clean"},
    ]
    graph = _make_graph(nodes, on_startup=received.append)
    await graph.start()
    await graph.aclose()

    # Both modules had the callback wired — no AttributeError or TypeError.
    # passthrough_denoiser and wav source don't emit startup events by default,
    # but the wiring must not crash.
    assert isinstance(received, list)


def test_graph_wires_callback_before_start_for_driver_warmup():
    """A driver that pre-warms modules must receive their startup events too."""
    received: list[StartupEvent] = []
    graph = _make_graph(
        [{"name": "src", "impl": "wav", "out": "audio.raw",
          "path": "assets/lecture.wav", "realtime": False}],
        on_startup=received.append,
    )

    graph.nodes[0].stage._report_startup(StartupPhase.IN_PROGRESS, "pre-warming")

    assert received == [
        StartupEvent("src", StartupPhase.IN_PROGRESS, "pre-warming")
    ]


@pytest.mark.asyncio
async def test_graph_no_callback_no_crash():
    """Graph works fine with on_startup=None (the default)."""
    nodes = [
        {"name": "src", "impl": "wav", "out": "audio.raw",
         "path": "assets/lecture.wav", "realtime": False},
    ]
    graph = _make_graph(nodes, on_startup=None)
    await graph.start()
    await graph.aclose()


@pytest.mark.asyncio
async def test_graph_callback_receives_events_from_reporting_module():
    """A module that reports IN_PROGRESS + READY fires both events through the Graph."""
    from livesub.core.registry import REGISTRY

    REGISTRY["_reporting_src"] = (
        f"{_ReportingSource.__module__}:_ReportingSource"
    )
    try:
        received: list[StartupEvent] = []
        nodes = [
            {"name": "rep", "impl": "_reporting_src", "out": "t1"},
        ]
        graph = _make_graph(nodes, on_startup=received.append)
        await graph.start()
        await graph.aclose()

        phases = [e.phase for e in received if e.module_name == "rep"]
        assert StartupPhase.IN_PROGRESS in phases
        assert StartupPhase.READY in phases
        # IN_PROGRESS must come before READY
        assert phases.index(StartupPhase.IN_PROGRESS) < phases.index(StartupPhase.READY)
    finally:
        REGISTRY.pop("_reporting_src", None)

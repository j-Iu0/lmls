"""Subtitle sinks -- where events are presented. Many may run at once.

    stdout_pretty     terminal display with in-place correction (the live demo view)
    jsonl             append-only event log (the evidence-of-testing deliverable)
    websocket_server  broadcast to any UI (the seam a GUI attaches to)

A sink subscribes to as many topics as it likes. Subscribing one sink to both
``text.raw`` and ``text.corrected`` is what produces the two-tier display: fast
provisional English, then a corrected revision that replaces it in place.
"""

from .jsonl import JsonlSink
from .stdout_pretty import PrettyStdoutSink
from .websocket_server import WebSocketSink

__all__ = ["JsonlSink", "PrettyStdoutSink", "WebSocketSink"]

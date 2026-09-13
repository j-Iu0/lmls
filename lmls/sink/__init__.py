"""Subtitle sinks -- where events are presented. Many may run at once.

    stdout_pretty     terminal display with in-place correction (the live demo view)
    jsonl             append-only event log (the evidence-of-testing deliverable)
    websocket_server  broadcast to any UI (the seam a GUI attaches to)

A sink exposes one declared port for each subtitle stream it accepts. Multiple sinks may
subscribe to the same topic, but every individual port is wired to exactly one topic.
"""

from .jsonl import JsonlSink
from .stdout_pretty import PrettyStdoutSink
from .websocket_server import WebSocketSink

__all__ = ["JsonlSink", "PrettyStdoutSink", "WebSocketSink"]

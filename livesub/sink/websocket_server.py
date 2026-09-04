"""Broadcast subtitle events over a WebSocket.

The contract a user interface attaches to. No GUI ships in this backend, so this is the
seam: anything that can open a WebSocket -- a browser page, a native macOS app, OBS --
receives the same event stream the terminal sink renders, and implements the same
``segment_id``/``revision`` replace rule.

Wire protocol, one JSON object per message::

    {"type": "subtitle",
     "segment_id": "u0007", "revision": 2, "kind": "translated",
     "lang": "vi", "text": "...", "is_final": true,
     "t_audio_end": 1756...,  "t_emit": 1756..., "end_to_end_ms": 2140,
     "stage_latency_ms": {"asr": 310, "fused_llm": 980}, "meta": {...}}

Client rule, the whole of it: keep a block per ``segment_id``; on each message, if
``revision`` is greater than or equal to the revision already held for that ``kind``,
replace that line and leave every other block alone.

A late joiner gets the last ``replay`` events on connect, so a page refreshed mid-lecture
is not staring at a blank screen until the next sentence.

Slow or dead clients are dropped rather than allowed to apply backpressure: a browser tab
that stops reading must never be able to stall the pipeline feeding everyone else.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from typing import Any, ClassVar

from ..core.codec import event_to_dict
from ..core.interfaces import Module
from ..core.types import TextFrame

log = logging.getLogger("livesub.sink.ws")


class WebSocketSink(Module):
    """Args:
        host/port: bind address. Defaults to localhost only -- a lecture transcript is
            not something to expose on a classroom network by accident.
        replay: events buffered for clients that connect late.
        send_timeout: a client that cannot accept a message within this window is
            disconnected.
    """

    inputs: ClassVar[dict[str, type]] = {"text": TextFrame}
    outputs: ClassVar[dict[str, type]] = {}

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        replay: int = 20,
        send_timeout: float = 1.0,
        **_: Any,
    ):
        super().__init__()
        self.host = host
        self.port = port
        self.send_timeout = send_timeout
        self._clients: set[Any] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=replay)
        self._server = None
        self.dropped_clients = 0

    async def start(self) -> None:
        if self._server is not None:
            return  # already serving; a second bind would raise EADDRINUSE
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - core dependency
            raise ImportError("`pip install websockets`") from exc

        async def handler(connection) -> None:
            self._clients.add(connection)
            log.info("subtitle client connected (%d total)", len(self._clients))
            try:
                for item in list(self._history):
                    await connection.send(json.dumps(item, ensure_ascii=False))
                await connection.wait_closed()
            finally:
                self._clients.discard(connection)

        self._server = await websockets.serve(handler, self.host, self.port)
        log.info("subtitle stream on ws://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def process(self, frame: TextFrame) -> None:
        payload = {"type": "subtitle", **event_to_dict(frame)}
        self._history.append(payload)
        if not self._clients:
            return
        message = json.dumps(payload, ensure_ascii=False)

        async def send(client) -> Any:
            return await asyncio.wait_for(client.send(message), self.send_timeout)

        results = await asyncio.gather(
            *(send(c) for c in list(self._clients)), return_exceptions=True
        )
        for client, result in zip(list(self._clients), results):
            if isinstance(result, Exception):
                # Never let one stuck browser tab stall the pipeline.
                self._clients.discard(client)
                self.dropped_clients += 1
                log.info("dropped unresponsive subtitle client: %r", result)

    def describe(self) -> dict[str, Any]:
        return {
            "stage": "WebSocketSink",
            "name": self.name,
            "url": f"ws://{self.host}:{self.port}",
            "clients": len(self._clients),
            "dropped_clients": self.dropped_clients,
        }

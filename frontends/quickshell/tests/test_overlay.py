"""Opt-in live niri smoke test: launches only its own overlay and local server.

Run from the repository root with pytest explicitly targeting this file.
"""

import asyncio
import json
import os
from pathlib import Path
import subprocess

import pytest
from websockets.asyncio.server import serve

FRONTEND = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_live_overlay_receives_subtitles_and_exposes_ipc_controls(tmp_path):
    ready = asyncio.Event()
    connections = []

    async def client_connected(client):
        connections.append(client)
        ready.set()
        await client.wait_closed()

    def niri(command):
        return json.loads(subprocess.check_output(["niri", "msg", "--json", command]))

    def ipc(command):
        return subprocess.check_output([
            "quickshell", "ipc", "--pid", str(process.pid), "call", "--", "subtitles", command,
        ], text=True).strip()

    focus_before = niri("focused-window")
    async with serve(client_connected, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        environment = dict(os.environ, LMLS_WS_URL=f"ws://127.0.0.1:{port}")
        with (tmp_path / "quickshell.log").open("w+") as log:
            process = subprocess.Popen(["quickshell", "--path", str(FRONTEND)],
                                       env=environment, stdout=log, stderr=log)
            try:
                try:
                    await asyncio.wait_for(ready.wait(), 5)
                except TimeoutError:
                    log.seek(0)
                    pytest.fail("Overlay did not connect:\n" + log.read())
                await connections[0].send(json.dumps({
                    "type": "subtitle", "topic": "text.corrected", "segment_id": "smoke",
                    "revision": 0, "lang": "en", "text": "Window smoke test",
                }))
                async with asyncio.timeout(5):
                    while True:
                        state = json.loads(ipc("status"))
                        if state["source"] == "Window smoke test":
                            break
                        await asyncio.sleep(0.05)
                assert state["visible"] is True
                next(w for w in niri("windows") if w["title"] == "LMLS Subtitles")
                ipc("hide")
                assert json.loads(ipc("status"))["visible"] is False
                show_result = ipc("show")
                state = json.loads(ipc("status"))
                assert state["visible"] is True, (state, show_result)
                ipc("clear")
                assert json.loads(ipc("status"))["source"] == ""
                ipc("quit")
                await asyncio.to_thread(process.wait, timeout=5)
                assert process.returncode == 0
            finally:
                if process.poll() is None:
                    process.terminate()
                    await asyncio.to_thread(process.wait, timeout=5)
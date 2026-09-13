"""The subtitle UI contract, exercised through an actual WebSocket client."""

import asyncio
import contextlib
import json

import pytest
from websockets.asyncio.client import connect

from lmls.core.config import NodeConfig, load_config
from lmls.core.graph import Graph


@pytest.mark.asyncio
async def test_graph_websocket_preserves_topic_for_live_events_and_replay(unused_tcp_port):
    config = load_config("config/mock.toml")
    config.node("src").options["realtime"] = False
    config.settings["audio_backpressure"] = "block"
    config.nodes = [node for node in config.nodes if node.name != "screen"]
    config.nodes.append(NodeConfig(
        name="ws", impl="websocket_server",
        inputs={"raw": "text.raw", "fixed": "text.corrected", "vi": "text.out"},
        options={"port": unused_tcp_port, "replay": 200},
    ))
    graph = Graph(config)
    await graph.start()
    task = None
    try:
        async with connect(f"ws://127.0.0.1:{unused_tcp_port}") as client:
            task = asyncio.create_task(graph.run())
            messages = []
            async with asyncio.timeout(5):
                while not any(message.get("topic") == "text.out" for message in messages):
                    messages.append(json.loads(await client.recv()))
                    assert "topic" in messages[-1]
            assert {message["topic"] for message in messages} == {
                "text.raw", "text.corrected", "text.out",
            }
            assert all(message["type"] == "subtitle" for message in messages)
            async with connect(f"ws://127.0.0.1:{unused_tcp_port}") as late_client:
                replay = [json.loads(await asyncio.wait_for(late_client.recv(), 5)) for _ in messages]
                assert replay == messages
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await graph.aclose()

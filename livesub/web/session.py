"""One authoritative local run, with generation-safe playback and bounded UI history."""
from __future__ import annotations

import asyncio
import copy
import logging
import math
import time
from collections import OrderedDict, deque
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..core.codec import event_to_dict
from ..core.config import GraphConfig
from ..core.graph import Graph
from ..core.startup import StartupEvent
from ..core.types import AudioFrame, TextFrame, Utterance
from ..input.media import ControlledMediaSource
from .configuration import from_editor, to_editor, validate_editor

ACTIVE = {"starting", "running", "seeking", "stopping"}


class SessionConflict(ValueError):
    """A command is incompatible with the current run or generation."""


class Session:
    def __init__(self, config: GraphConfig, media_path):
        self.config = to_editor(config)
        self.media_path = media_path
        self.state = "idle"
        self.epoch = 0
        self.error: str | None = None
        self.graph: Graph | None = None
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.logs: deque = deque(maxlen=250)
        self.subtitles: OrderedDict = OrderedDict()
        self.utterances: OrderedDict = OrderedDict()
        self.sources: dict[str, ControlledMediaSource] = {}
        self.media: dict[str, dict] = {}
        self.pending: dict[str, dict[str, float]] = {}
        self.ancestors: dict[str, set[str]] = {}
        self.downstream_topics: dict[str, set[str]] = {}
        self.levels: dict[str, float] = {}
        self.startup: dict[str, dict] = {}
        self._last_clock = time.monotonic()

    def log(self, level: str, message: str, node: str = "") -> None:
        self.logs.append({"time": time.time(), "level": level, "node": node, "message": str(message)})

    def _check_epoch(self, epoch: Any) -> None:
        if epoch != self.epoch:
            raise SessionConflict("stale playback generation; refresh the session state")

    def _runtime_config(self, offsets: dict[str, float]) -> GraphConfig:
        cfg = from_editor(copy.deepcopy(self.config))
        cfg.settings["audio_backpressure"] = "block"
        # These file transports have identical AudioFrame ports. Preserve the saved
        # implementation/options; select the clocked adapter only for this web run.
        for n in cfg.active:
            if n.impl in {"media", "wav", "ffmpeg"} and not n.options.get("device"):
                raw = n.options.get("url") or n.options.get("path") or n.options.get("source")
                if not raw:
                    raise ValueError(f"{n.name}: choose a media file first")
                if str(raw).startswith(("http://", "https://", "rtsp://", "rtmp://")):
                    if n.impl == "media":
                        raise ValueError("interactive media needs a local file; use ffmpeg for live URLs")
                    continue
                path = self.media_path(str(raw))
                n.impl = "media"
                n.options = {**n.options, "url": str(path),
                             "start_seconds": offsets.get(n.name, 0), "loop": False}
                # A clocked file must not silently drop audio even if an imported
                # preset selected realtime dropping. Other source branches keep modes.
                n.options.pop("path", None)
        return cfg

    def _routing(self, cfg: GraphConfig) -> None:
        produced = {t: {n.name for n in cfg.active if t in n.out_topics}
                    for n in cfg.active for t in n.out_topics}
        self.ancestors = {n.name: ({n.name} if not n.inputs else set()) for n in cfg.active}
        for _ in cfg.active:
            for n in cfg.active:
                for t in n.in_topics:
                    for upstream in produced.get(t, set()):
                        self.ancestors[n.name].update(self.ancestors[upstream])
        # Topics reachable from each utterance-producing node; do not infer a role
        # from names. This is also used to validate overlay synchronization routes.
        self.downstream_topics = {}
        for start in cfg.active:
            reachable = set(start.out_topics)
            for _ in cfg.active:
                for n in cfg.active:
                    if reachable.intersection(n.in_topics):
                        reachable.update(n.out_topics)
            self.downstream_topics[start.name] = reachable

    async def start(self, data: dict) -> dict:
        async with self.lock:
            if self.state in ACTIVE:
                raise SessionConflict("stop the running graph before editing or starting another")
            cfg, warnings = validate_editor(data)
            old_config = self.config
            self.config = to_editor(cfg)
            try:
                runtime = self._runtime_config({})
                # Constructor validation belongs before accepting an active run.
                self._routing(runtime)
                for n, enabled in self.config['editor'].get('auto_pause', {}).items():
                    if enabled and not self.config['editor'].get('overlays', {}).get(n):
                        raise ValueError(f"{n}: automatic pause needs an attached text output")
            except Exception:
                self.config = old_config
                raise
            self.logs.clear()
            for warning in warnings:
                self.log("warning", warning)
            await self._launch(runtime, "starting")
            return self.snapshot()

    async def _launch(self, cfg: GraphConfig, state: str) -> None:
        self.epoch += 1
        epoch = self.epoch
        self.state, self.error = state, None
        self.subtitles.clear()
        self.utterances.clear()
        self.pending.clear()
        self.levels.clear()
        self.startup.clear()
        self.sources.clear()
        self.media.clear()
        self._last_clock = time.monotonic()
        loop = asyncio.get_running_loop()
        try:
            graph = Graph(cfg, on_startup=lambda event: loop.call_soon_threadsafe(self._startup, epoch, event),
                          on_event=lambda event: self._event(epoch, event))
            self.graph = graph
            self._routing(cfg)
            for node in graph.nodes:
                if isinstance(node.stage, ControlledMediaSource):
                    self.sources[node.config.name] = node.stage
                    self.media[node.config.name] = {"position": node.stage.start_seconds,
                        "paused": True, "decoded_s": node.stage.start_seconds,
                        "auto_paused": False, "pending": 0, "status": "Ready"}
                    self.pending[node.config.name] = {}
            # Synchronization requires an unambiguous path from this file to text.
            for source, topic in self.config['editor'].get('overlays', {}).items():
                if source not in self.sources or not topic:
                    continue
                publishers = [n for n in cfg.active if topic in n.out_topics]
                if not publishers or any(self.ancestors[n.name] != {source} for n in publishers):
                    raise ValueError(f"{source}: overlay topic {topic!r} must originate only from this file")
            self.log("info", "Starting graph; editing locked" if state == "starting" else
                     "Playback position changed; old subtitles and module context discarded")
            self.task = asyncio.create_task(self._run(graph, epoch), name=f"web-run-{epoch}")
        except Exception as exc:
            self.state, self.error = "failed", str(exc)
            self.log("error", str(exc))
            raise

    async def _run(self, graph: Graph, epoch: int) -> None:
        try:
            await graph.start()
            if epoch != self.epoch:
                return
            self.state = "running"
            self.log("info", "All modules ready; graph running")
            await graph.run()
            if epoch == self.epoch:
                self.state = "completed"
                self.log("info", "Input finished; all downstream work drained")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if epoch == self.epoch:
                self.state, self.error = "failed", str(exc)
                self.log("error", str(exc))
        finally:
            await graph.aclose()

    async def _cancel(self) -> None:
        task = self.task
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.task = None

    async def stop(self) -> dict:
        async with self.lock:
            if self.state in ACTIVE:
                self.state = "stopping"
                await self._cancel()
                self.state = "idle"
                self.log("info", "Graph stopped; editing unlocked")
            return self.snapshot()

    async def seek(self, node: str, position: float, epoch: int) -> dict:
        position = finite_seconds(position)
        async with self.lock:
            self._check_epoch(epoch)
            if self.state not in {"running", "completed"} or node not in self.sources:
                raise SessionConflict("seek requires a running or completed file graph")
            offsets = {name: info["position"] for name, info in self.media.items()}
            offsets[node] = position
            cfg = self._runtime_config(offsets)
            self.state = "seeking"
            # Invalidate callbacks before cancellation can deliver a late result.
            self.epoch += 1
            await self._cancel()
            await self._launch(cfg, "seeking")
            return self.snapshot()

    def clock(self, node: str, position: float, paused: bool, epoch: int) -> None:
        self._check_epoch(epoch)
        position = finite_seconds(position)
        if self.state not in {"starting", "seeking", "running"} or node not in self.sources:
            return
        info = self.media[node]
        # A clock is not a seek. Enforce this on the server as well as in the UI.
        if position < info["position"] - 0.25 or position > info["position"] + 2.0:
            raise SessionConflict("use seek to change playback position")
        info.update(position=position, paused=bool(paused))
        self._last_clock = time.monotonic()
        if self.state == "running":
            self.sources[node].advance(position)

    def _startup(self, epoch: int, event: StartupEvent) -> None:
        if epoch != self.epoch or self.state not in ACTIVE:
            return
        data = {"phase": event.phase.value, "message": event.message}
        if event.progress:
            data["progress"] = {"current": event.progress.current, "total": event.progress.total,
                                "unit": event.progress.unit, "fraction": event.progress.fraction}
        self.startup[event.module_name] = data
        self.log("error" if event.phase.value == "failed" else "info",
                 event.message or event.phase.value, event.module_name)

    def _event(self, epoch: int, event: dict) -> None:
        if epoch != self.epoch:
            return
        kind = event.get("type", event.get("kind"))
        node = event.get("node", "")
        if kind == "node_state":
            state = event.get("state", "")
            self.log("error" if state == "failed" else "info", event.get("message") or state, node)
            return
        if kind != "output":
            return
        payload, topic = event.get("payload"), event.get("topic")
        if isinstance(payload, AudioFrame):
            import numpy as np
            self.levels[node] = max(-120.0, float(20 * np.log10(max(1e-6, np.sqrt(np.mean(payload.pcm ** 2))))))
        elif isinstance(payload, Utterance):
            origins = self.ancestors.get(node, set()) & self.sources.keys()
            source = next(iter(origins)) if len(origins) == 1 else None
            timing: dict[str, Any] = {}
            if source:
                decoder = self.sources[source]
                end = decoder.media_time(payload.t_end - .02) + .02
                timing = {"source": source, "media_start": max(decoder.start_seconds, end - payload.duration),
                          "media_end": end}
                selected = self.config['editor'].get('overlays', {}).get(source)
                delivered = self.subtitles.get((selected, payload.id), {})
                if (payload.is_final and selected in self.downstream_topics.get(node, set())
                        and not delivered.get('is_final')):
                    self.pending[source][payload.id] = time.monotonic()
                    if len(self.pending[source]) > 2000:
                        self.pending[source].pop(next(iter(self.pending[source])))
                        self.log('warning', 'Pending subtitle history reached its 2000-segment limit', source)
            self.utterances[payload.id] = timing
            # A blocking publication may let a fast downstream consumer publish
            # before the upstream observer is notified. Attach timing retroactively
            # and avoid waiting for a final result that is already delivered.
            for item in self.subtitles.values():
                if item['segment_id'] == payload.id:
                    item.update(timing)
            while len(self.utterances) > 2000:
                self.utterances.popitem(last=False)
        elif isinstance(payload, TextFrame):
            item = {**event_to_dict(payload), "topic": topic, "node": node,
                    **self.utterances.get(payload.segment_id, {})}
            key = (topic, payload.segment_id)
            previous = self.subtitles.get(key)
            if previous is None or payload.revision >= previous['revision']:
                self.subtitles[key] = item
            while len(self.subtitles) > 500:
                self.subtitles.popitem(last=False)
            source = item.get('source')
            if (source and payload.is_final and topic == self.config['editor'].get('overlays', {}).get(source)):
                self.pending[source].pop(payload.segment_id, None)

    def snapshot(self) -> dict:
        raw = self.graph.snapshot() if self.graph else {}
        nodes = raw.get("nodes", {})
        # Normalize a stable UI contract independently of runtime diagnostics fields.
        if isinstance(nodes, list):
            nodes = {n.get('name', ''): n for n in nodes}
        metrics = self.graph.metrics.summary() if self.graph else {}
        bus = self.graph.bus.report() if self.graph else {}
        for name, item in nodes.items():
            stat = metrics.get('stages', {}).get(name, {})
            item.setdefault('processed', stat.get('n', 0))
            item.setdefault('mean_ms', stat.get('mean', 0))
            item.setdefault('p95_ms', stat.get('p95', 0))
            item['queues'] = [{'topic': topic, **data['subscribers'][name]}
                              for topic, data in bus.items() if name in data['subscribers']]
            if name in self.levels:
                item['level_dbfs'] = self.levels[name]
            if name in self.startup:
                item['startup'] = self.startup[name]
        for name, source in self.sources.items():
            info = self.media[name]
            count = len(self.pending[name])
            auto = bool(self.config['editor'].get('auto_pause', {}).get(name))
            info.update(decoded_s=source.position, pending=count,
                        auto_paused=auto and count > 0 and self.state == "running")
            info['status'] = ("Waiting for attached subtitle" if info['auto_paused'] else
                              "Paused" if info['paused'] else "Playing")
            if count and time.monotonic() - min(self.pending[name].values()) > 15:
                info['status'] = "Attached output has not completed; inspect module diagnostics"
        return {"state": self.state, "epoch": self.epoch, "config": self.config,
                "error": self.error, "nodes": nodes, "metrics": metrics,
                "topics": bus,
                "subtitles": list(self.subtitles.values()), "logs": list(self.logs),
                "media": copy.deepcopy(self.media)}


def finite_seconds(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("position must be finite and non-negative")
    return number

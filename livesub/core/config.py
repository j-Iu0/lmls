"""Graph configuration: a declarative list of nodes and the topics wiring them.

A config file *is* the architecture. Removing noise reduction is deleting the ``nr``
node and pointing the ASR node's ``in`` at ``audio.raw``; handing correction duty to the
translator is deleting the ``fix`` node and pointing the translator at ``text.raw``.
No Python changes in either case.

A node declares *which implementation* it runs (``impl``) and which bus topics (pipes)
each of that module's ports connects to. The class's ``inputs``/``outputs`` port
declarations say what the module does; the config only does the wiring. Port names are
scoped to their module and mean nothing outside it; topic names mean nothing at all --
all behaviour (routing, type checking, backpressure) follows from the payload types the
ports declare.

Example::

    [[node]]
    name = "vad"
    impl = "energy"
    in = "audio.clean"
    out = "utterance.speech"

    [[node]]
    name = "asr"
    impl = "faster_whisper"
    in = "utterance.speech"
    out = "text.raw"
    model = "small.en"

``in`` and ``out`` accept three forms each:

* a single string -- wired to the module's sole port;
* a list of strings -- topics mapped positionally onto the declared ports, in
  declaration order (a module with one input port takes any number of topics: fan-in).
  In an ``out`` list, a ``"_"`` entry skips that port, leaving it unwired;
* a table -- explicit ``port_name = "topic"`` entries, validated against the
  module's port declarations.

Any key that is not one of the structural keys (``name``, ``impl``, ``in``, ``out``,
``enabled``, ``mode``, ``skip_if_finalized``) is passed to the implementation's
constructor as a keyword argument, which is how per-node options like ``model`` or
``target`` reach it.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .registry import resolve

_STRUCTURAL = {"name", "impl", "in", "out", "enabled", "mode", "skip_if_finalized"}

_SUBSCRIPTION_MODES = ("default", "live", "blocking", "catchup")


class ConfigError(ValueError):
    pass


@dataclass
class NodeConfig:
    name: str
    impl: str
    #: {port_name: topic_name} -- maps each input port to a bus topic
    inputs: dict[str, str] = field(default_factory=dict)
    #: {port_name: topic_name} -- maps each output port to a bus topic.
    #: The ``"*"`` key means "publish everything here" and is resolved to the module's
    #: sole output port at wiring time.
    outputs: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    #: Bus subscription mode for all input subscriptions on this node.
    mode: str = "default"  # "default" | "live" | "blocking" | "catchup"
    #: Only used when mode="catchup". Topic to monitor for finalized segment_ids to skip.
    skip_if_finalized: str | None = None
    enabled: bool = True

    @property
    def in_topics(self) -> list[str]:
        return list(self.inputs.values())

    @property
    def out_topics(self) -> list[str]:
        return sorted(set(self.outputs.values()))

    def topic_for_port(self, port_name: str) -> str | None:
        return self.outputs.get(port_name)


@dataclass
class GraphConfig:
    nodes: list[NodeConfig] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None

    def node(self, name: str) -> NodeConfig:
        for n in self.nodes:
            if n.name == name:
                return n
        raise ConfigError(f"no node named {name!r}")

    @property
    def active(self) -> list[NodeConfig]:
        return [n for n in self.nodes if n.enabled]


def _parse_in(raw_in: Any, cls: type, name: str, index: int) -> dict[str, str]:
    """Parse the ``in`` key into a {port_name: topic} mapping.

    Port names are scoped to the module and mean nothing to the graph; a topic name
    means nothing at all. Every form below resolves to real declared port names:

    * string -- the module's sole input port (error if it declares none or several);
    * list -- topics mapped positionally onto the declared ports, in declaration
      order. When the module declares exactly one input port, any number of topics is
      accepted and all of them subscribe to that port (fan-in);
    * table -- explicit ``port_name = "topic"`` entries, validated against the
      module's declarations.
    """
    declared = list(cls.inputs)
    if isinstance(raw_in, str):
        if len(declared) != 1:
            raise ConfigError(
                f"node {name!r}: a single 'in' string needs a module with exactly one "
                f"input port; {cls.__name__} declares {declared or 'none'}"
            )
        return {declared[0]: str(raw_in)}
    if isinstance(raw_in, list):
        if not all(isinstance(t, str) for t in raw_in):
            raise ConfigError(f"node #{index}: 'in' list entries must be topic strings")
        if not cls.inputs:
            raise ConfigError(
                f"node {name!r}: {cls.__name__} declares no input ports"
            )
        if len(raw_in) == len(declared):
            # Unambiguous: one topic per declared port, positionally.
            return {port: str(t) for port, t in zip(declared, raw_in)}
        if len(declared) == 1:
            # Fan-in: several topics, one port. Synthetic keys are internal bookkeeping
            # for the same port; every one of them is type-checked against it.
            port = declared[0]
            return {port if i == 0 else f"{port}_{i}": str(t)
                    for i, t in enumerate(raw_in)}
        raise ConfigError(
            f"node {name!r}: 'in' list has {len(raw_in)} topics for "
            f"{cls.__name__} which declares {len(declared)} input ports "
            f"{declared}; use a table of port = topic to name them"
        )
    if isinstance(raw_in, dict):
        for port_name in raw_in:
            if port_name not in cls.inputs:
                raise ConfigError(
                    f"node {name!r}: input port {port_name!r} not declared by "
                    f"{cls.__name__}; declared: {sorted(cls.inputs)}"
                )
        return {str(k): str(v) for k, v in raw_in.items()}
    raise ConfigError(
        f"node #{index}: 'in' must be a string, a list of topics, or a table of "
        f"port -> topic"
    )


def _parse_out(raw_out: Any, cls: type, name: str, index: int) -> dict[str, str]:
    declared = list(cls.outputs)
    if raw_out is None:
        return {}
    if isinstance(raw_out, str):
        if raw_out == "_":
            raise ConfigError(
                f"node {name!r}: '_' is not a topic name; use a list "
                f"(out = [\"_\"]) to skip a port"
            )
        if len(declared) != 1:
            raise ConfigError(
                f"node {name!r}: a single 'out' string needs a module with exactly one "
                f"output port; {cls.__name__} declares {declared or 'none'}; "
                f"use a table of port = topic to name them"
            )
        return {declared[0]: str(raw_out)}
    if isinstance(raw_out, list):
        if not all(isinstance(t, str) for t in raw_out):
            raise ConfigError(
                f"node #{index}: 'out' list entries must be topic strings"
            )
        if not cls.outputs:
            raise ConfigError(
                f"node {name!r}: {cls.__name__} declares no output ports"
            )
        if len(raw_out) != len(declared):
            raise ConfigError(
                f"node {name!r}: 'out' list has {len(raw_out)} topics for "
                f"{cls.__name__} which declares {len(declared)} output ports "
                f"{declared}; use a table of port = topic to name them"
            )
        # A "_" entry skips that port: it is left unwired and publishes nothing.
        return {port: str(t) for port, t in zip(declared, raw_out) if t != "_"}
    if isinstance(raw_out, dict):
        for port_name, topic in raw_out.items():
            if port_name == "*":
                raise ConfigError(
                    f"node {name!r}: '*' is not a port name; name the port "
                    f"explicitly (declared: {sorted(cls.outputs)})"
                )
            if topic == "_":
                raise ConfigError(
                    f"node {name!r}: '_' is not a topic name; omit the port to "
                    f"leave it unwired (declared: {sorted(cls.outputs)})"
                )
            if port_name not in cls.outputs:
                raise ConfigError(
                    f"node {name!r}: output port {port_name!r} not declared by "
                    f"{cls.__name__}; declared: {sorted(cls.outputs)}"
                )
        return {str(k): str(v) for k, v in raw_out.items()}
    raise ConfigError(
        f"node #{index}: 'out' must be a string, a list of topics, or a table of "
        f"port -> topic"
    )


def _parse_node(raw: dict[str, Any], index: int) -> NodeConfig:
    if "impl" not in raw:
        raise ConfigError(f"node #{index}: missing required key 'impl'")

    impl = raw["impl"]
    name = raw.get("name") or f"{impl}{index}"
    mode = raw.get("mode", "default")
    if mode not in _SUBSCRIPTION_MODES:
        raise ConfigError(
            f"node {name!r}: mode must be one of {_SUBSCRIPTION_MODES}, got {mode!r}"
        )
    skip_if_finalized = raw.get("skip_if_finalized")

    # Resolve the class so port names can be validated before anything is built.
    from .registry import MissingDependency, UnknownImplementation

    try:
        cls = resolve(impl)
    except (UnknownImplementation, MissingDependency):
        raise  # surfaced with its own, more helpful message
    except Exception as exc:  # e.g. a broken optional dependency at import time
        raise ConfigError(f"node #{index}: could not resolve impl {impl!r}: {exc}")

    raw_in = raw.get("in", [])
    if raw_in == []:
        inputs: dict[str, str] = {}
    else:
        inputs = _parse_in(raw_in, cls, name, index)

    outputs = _parse_out(raw.get("out"), cls, name, index)

    return NodeConfig(
        name=name,
        impl=impl,
        inputs=inputs,
        outputs=outputs,
        options={k: v for k, v in raw.items() if k not in _STRUCTURAL},
        mode=mode,
        skip_if_finalized=skip_if_finalized,
        enabled=bool(raw.get("enabled", True)),
    )


def load_config(path: str | Path) -> GraphConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    data = tomllib.loads(path.read_text())
    raw_nodes = data.get("node", [])
    if not raw_nodes:
        raise ConfigError(f"{path} declares no [[node]] entries")
    nodes = [_parse_node(raw, i) for i, raw in enumerate(raw_nodes)]
    settings = {k: v for k, v in data.items() if k != "node"}
    return GraphConfig(nodes=nodes, settings=settings, source_path=path)


# -- chain shorthand ---------------------------------------------------------

#: Default impl and topics for each stage in the linear shorthand. Portable by default:
#: faster-whisper ASR in-process, LLM stages via the Ollama server.
_CHAIN_DEFAULTS: dict[str, dict[str, Any]] = {
    "mic": {"impl": "mic", "out": "audio.raw"},
    "ffmpeg": {"impl": "ffmpeg", "out": "audio.raw"},
    "wav": {"impl": "wav", "out": "audio.raw"},
    "segment": {"impl": "energy", "in": "audio.raw", "out": "utterance.speech"},
    "denoise": {"impl": "spectral", "in": "audio.raw", "out": "audio.clean"},
    "asr": {"impl": "faster_whisper", "in": "utterance.speech", "out": "text.raw"},
    "correct": {"impl": "ollama_corrector", "in": "text.raw", "out": "text.corrected"},
    "translate": {
        "impl": "ollama_translator",
        "in": "text.corrected",
        "out": {"text_out": "text.out"},
    },
}


def chain_config(
    stages: list[str],
    overrides: dict[str, str] | None = None,
    settings: dict[str, Any] | None = None,
    sinks: list[str] | None = None,
) -> GraphConfig:
    """Build a linear graph from stage names, wiring each node to its predecessor.

    Omitting a name simply skips that stage -- ``mic,segment,asr,translate`` wires the
    translator straight to ``text.raw``, so a faithful translator there would freeze ASR
    errors into the subtitles. Pass a fused implementation for that stage (e.g.
    ``--translate fused_ollama``) to repair the errors in the same call.
    """
    overrides = overrides or {}
    nodes: list[NodeConfig] = []
    prev_topic: str | None = None

    for i, stage in enumerate(stages):
        stage = stage.strip()
        if stage not in _CHAIN_DEFAULTS:
            raise ConfigError(
                f"unknown chain stage {stage!r}; known: {sorted(_CHAIN_DEFAULTS)}"
            )
        spec = dict(_CHAIN_DEFAULTS[stage])
        if stage in overrides:
            spec["impl"] = overrides[stage]
        raw: dict[str, Any] = {"name": stage, **spec}
        if prev_topic is not None:
            raw["in"] = prev_topic
        elif "in" not in spec:
            raw.pop("in", None)
        node = _parse_node(raw, i)
        nodes.append(node)
        prev_topic = node.out_topics[0]

    # Sinks subscribe to every topic carrying a TextFrame. The type comes from the
    # producing port's declaration -- the topic's *name* decides nothing.
    from .types import TextFrame

    topic_types: dict[str, type] = {}
    for n in nodes:
        cls = resolve(n.impl)
        for port_name, topic in n.outputs.items():
            topic_types.setdefault(topic, cls.outputs[port_name])
    text_topics_out = sorted(t for t, tt in topic_types.items() if tt is TextFrame)

    for j, sink_impl in enumerate(sinks or ["stdout_pretty"]):
        raw_sink: dict[str, Any] = {"name": f"sink_{sink_impl}", "impl": sink_impl}
        if text_topics_out:
            raw_sink["in"] = text_topics_out
        nodes.append(_parse_node(raw_sink, len(nodes) + j))
    return GraphConfig(nodes=nodes, settings=settings or {})
"""Transport-neutral editor documents over livesub's existing config format."""
from __future__ import annotations

import copy
import inspect
import json
import tomllib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..core.config import ConfigError, GraphConfig, config_from_dict, config_to_dict
from ..core.graph import validate
from ..core.registry import available, resolve, option_parameters
from ..core.types import TextFrame


def catalog() -> list[dict[str, Any]]:
    result = []
    for impl in available():
        item: dict[str, Any] = {"impl": impl, "inputs": {}, "outputs": {}, "options": []}
        try:
            cls = resolve(impl)
            item.update(description=inspect.getdoc(cls) or "",
                        inputs={p: t.__name__ for p, t in cls.inputs.items()},
                        outputs={p: t.__name__ for p, t in cls.outputs.items()})
            for name, param in option_parameters(cls).items():
                default = None if param.default is inspect.Parameter.empty else param.default
                try:
                    json.dumps(default)
                except (TypeError, ValueError):
                    default = str(default)
                item["options"].append({"name": name, "default": default,
                                        "required": param.default is inspect.Parameter.empty,
                                        "annotation": str(param.annotation)})
        except Exception as exc:
            item["error"] = str(exc)
        result.append(item)
    return result


def to_editor(cfg: GraphConfig) -> dict[str, Any]:
    settings = copy.deepcopy(cfg.settings)
    editor = settings.pop("editor", {})
    topics = sorted({topic for n in cfg.active for p, topic in n.outputs.items()
                     if resolve(n.impl).outputs[p] is TextFrame})
    editor.setdefault("subtitle_topics", topics)
    editor.setdefault("positions", {})
    editor.setdefault("overlays", {})
    editor.setdefault("auto_pause", {})
    return {"nodes": [asdict(n) for n in cfg.nodes], "settings": settings, "editor": editor}


def from_editor(data: dict[str, Any]) -> GraphConfig:
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        raise ConfigError("editor configuration needs a nodes array")
    raw = copy.deepcopy(data.get("settings", {}))
    if not isinstance(raw, dict):
        raise ConfigError("settings must be an object")
    raw["editor"] = copy.deepcopy(data.get("editor", {}))
    if not isinstance(raw["editor"], dict):
        raise ConfigError("editor must be an object")
    raw["node"] = []
    for n in data["nodes"]:
        if not isinstance(n, dict):
            raise ConfigError("every node must be an object")
        options = n.get("options", {})
        if not isinstance(options, dict):
            raise ConfigError("node options must be an object")
        item = copy.deepcopy(options)
        item.update({k: n[k] for k in ("name", "impl", "enabled", "mode") if k in n})
        if n.get("skip_if_finalized") is not None:
            item["skip_if_finalized"] = n["skip_if_finalized"]
        cls = resolve(n.get("impl", ""))
        ins = n.get("inputs", {})
        outs = n.get("outputs", {})
        if not isinstance(ins, dict) or not isinstance(outs, dict):
            raise ConfigError("ports must be objects mapping port names to topic strings")
        if not all(isinstance(t, str) and t for t in [*ins.values(), *outs.values()]):
            raise ConfigError("topic names must be nonempty strings")
        if ins:
            if len(cls.inputs) == 1 and (len(ins) > 1 or set(ins) != set(cls.inputs)):
                base = next(iter(cls.inputs))
                if any(p != base and not (p.startswith(base + "_") and p[len(base)+1:].isdigit())
                       for p in ins):
                    raise ConfigError(f"invalid fan-in ports for {n.get('name')!r}")
                item["in"] = list(ins.values())
            else:
                item["in"] = ins
        if outs:
            item["out"] = outs
        raw["node"].append(item)
    return config_from_dict(raw)


def validate_editor(data: dict[str, Any]) -> tuple[GraphConfig, list[str]]:
    cfg = from_editor(data)
    warnings = validate(cfg)
    names = [n.name for n in cfg.nodes]
    if len(set(names)) != len(names):
        raise ConfigError("node names must be unique, including disabled nodes")
    if any(not isinstance(n, str) or not n.strip() for n in names):
        raise ConfigError("node names must be nonempty strings")
    ed = data.get("editor", {})
    topics = {t for n in cfg.active for p, t in n.outputs.items()
              if resolve(n.impl).outputs[p] is TextFrame}
    selected = ed.get("subtitle_topics", [])
    if not isinstance(selected, list) or not all(isinstance(t, str) for t in selected):
        raise ConfigError("subtitle_topics must be a list of text topics")
    if set(selected) - topics:
        raise ConfigError(f"subtitle panel references unavailable text topics: {sorted(set(selected)-topics)}")
    for key in ("overlays", "auto_pause", "positions"):
        if not isinstance(ed.get(key, {}), dict):
            raise ConfigError(f"editor.{key} must be an object")
    for name, topic in ed.get("overlays", {}).items():
        if name not in names or (topic and topic not in topics):
            raise ConfigError(f"invalid overlay connection for {name!r}")
    observed = set(selected) | {t for t in ed.get('overlays', {}).values() if t}
    warnings = [w for w in warnings if w not in {
        f"topic {t!r} is published but nobody subscribes to it" for t in observed}]
    return cfg, warnings


def import_document(text: str, format: str) -> dict[str, Any]:
    if format not in ("json", "toml"):
        raise ConfigError("format must be json or toml")
    data = json.loads(text) if format == "json" else tomllib.loads(text)
    # Accept both the public pipeline format and a saved editor API document.
    return to_editor(from_editor(data) if "nodes" in data else config_from_dict(data))


def export_document(data: dict[str, Any], format: str) -> str:
    cfg = from_editor(data)
    raw = config_to_dict(cfg)
    if format == "json":
        return json.dumps(raw, indent=2, ensure_ascii=False) + "\n"
    if format != "toml":
        raise ConfigError("format must be json or toml")
    for node in raw['node']:
        params = inspect.signature(resolve(node['impl']).__init__).parameters
        for key in list(node):
            if node[key] is None:
                if key in params and params[key].default is None:
                    del node[key]  # omission means exactly the same constructor value
                else:
                    raise ConfigError(f"{node['name']}.{key}: TOML cannot represent null; export JSON")
    def reject_null(value, path='config'):
        if value is None:
            raise ConfigError(f"{path}: TOML cannot represent null; export JSON")
        if isinstance(value, dict):
            for key, child in value.items():
                reject_null(child, f'{path}.{key}')
        elif isinstance(value, list):
            for index, child in enumerate(value):
                reject_null(child, f'{path}[{index}]')
    reject_null(raw)
    import tomli_w
    return tomli_w.dumps(raw)


def default_config(root: Path) -> GraphConfig:
    """A model-free, explicitly labelled starting graph, valid outside the checkout."""
    nodes = [
        {"name": "media", "impl": "media", "out": "audio.raw", "url": "assets/lecture.wav"},
        {"name": "segment", "impl": "energy", "in": "audio.raw", "out": "utterance.speech"},
        {"name": "transcribe", "impl": "mock_transcriber", "in": "utterance.speech", "out": "text.raw"},
        {"name": "correct", "impl": "rules", "in": "text.raw", "out": "text.corrected"},
        {"name": "translate", "impl": "mock_translator", "in": "text.corrected",
         "out": {"text_out": "text.translated"}, "target": "vi"},
    ]
    return config_from_dict({"node": nodes, "audio_backpressure": "block", "editor": {
        "subtitle_topics": ["text.corrected", "text.translated"],
        "overlays": {"media": "text.translated"}, "auto_pause": {"media": True},
        "positions": {"media": {"x": 70, "y": 110}, "segment": {"x": 440, "y": 110},
                      "transcribe": {"x": 740, "y": 110}, "correct": {"x": 1040, "y": 50},
                      "translate": {"x": 1340, "y": 110}}}})

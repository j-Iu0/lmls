"""Utterance segmentation.

The segmenters are pipeline nodes now: the :class:`Module` wrappers in ``energy.py`` and
``silero.py`` declare audio frames in / utterances out, and the registry points the
``energy``/``silero`` impl names at them. ``make_segmenter`` remains for the offline
transcriber path only, where the ASR builds its own segmenter (see ``base.py`` for why
chunking policy belongs to the ASR).
"""

from __future__ import annotations

from typing import Any

from .base import Segmenter, SegmenterConfig
from .energy import _EnergySegmenterImpl

__all__ = [
    "Segmenter",
    "SegmenterConfig",
    "make_segmenter",
]


def make_segmenter(kind: str = "energy", **options: Any) -> Segmenter:
    """Build a segmenter by name, splitting timing options from detector options."""
    timing_keys = set(SegmenterConfig.__dataclass_fields__)
    config = SegmenterConfig(
        **{k: v for k, v in options.items() if k in timing_keys}
    )
    rest = {k: v for k, v in options.items() if k not in timing_keys}

    if kind == "energy":
        return _EnergySegmenterImpl(config=config, **rest)
    if kind == "silero":
        from .silero import _SileroSegmenterImpl

        return _SileroSegmenterImpl(config=config, **rest)
    raise ValueError(f"unknown segmenter {kind!r}; available: energy, silero")

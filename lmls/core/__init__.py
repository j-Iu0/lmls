"""Shared contracts. The only thing the pipeline modules have in common.

Import rule for the whole project: a module package (``lmls.input``,
``lmls.segment``, ``lmls.denoise``, ``lmls.transcribe``, ``lmls.correct``,
``lmls.translate``, ``lmls.fused``, ``lmls.sink``) may import from
``lmls.core`` and from nothing else inside ``lmls`` (except each module's own
package internals). ``lmls.core.graph`` is the sole exception -- it instantiates every
kind, via the registry, and is what the wiring config drives.
"""

from .bus import Bus, CatchupSubscription, RingBuffer, Subscription
from .config import GraphConfig, NodeConfig, chain_config, load_config
from .interfaces import Module
from .metrics import Metrics
from .types import (
    AUDIO_TOPIC_PREFIX,
    FRAME_MS,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    TEXT_TOPIC_PREFIX,
    AudioFrame,
    Lineage,
    TextFrame,
    Utterance,
)

__all__ = [
    "AUDIO_TOPIC_PREFIX",
    "AudioFrame",
    "Bus",
    "CatchupSubscription",
    "FRAME_MS",
    "FRAME_SAMPLES",
    "GraphConfig",
    "Lineage",
    "Metrics",
    "Module",
    "NodeConfig",
    "RingBuffer",
    "SAMPLE_RATE",
    "Subscription",
    "TEXT_TOPIC_PREFIX",
    "TextFrame",
    "Utterance",
    "chain_config",
    "load_config",
]
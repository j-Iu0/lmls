"""livesub -- a modular live bilingual subtitle pipeline.

Five swappable module kinds (input, denoise, transcribe, correct, translate) plus sinks,
wired together by a declarative graph of named topics. Any stage can be replaced,
omitted, or given several downstream consumers without changing another module's code.

See ``docs/architecture.md`` for the design and ``README.md`` for setup.
"""

__version__ = "0.1.0"

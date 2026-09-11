"""Terminal subtitle display with in-place correction.

This sink is where the spec's hardest display requirement is actually met: "when a
transcription error is identified, the application should correct the relevant word or
sentence without unnecessarily disrupting the rest of the live display."

It works because of what the sink *subscribes to*, not because of special-case code. Wire
it to the English topics and the translation topics at once and it receives several
frames per utterance, all sharing a ``segment_id``; frames whose ``lang`` starts with
``en`` fill the English line and every other ``lang`` fills a translation:

    rev 0  en  "grade ee ent dissent then adjusts every weight"
    rev 1  en  "Gradient descent then adjusts every weight"
    rev 2  vi  "Gradient descent sau đó điều chỉnh mọi trọng số"

The sink keeps one block per ``segment_id`` and repaints only that block. Earlier lines
on screen never move. Because each subscriber has its own bus queue, the corrected
English is painted the moment it arrives -- it does not wait for the translation, even
though both come from the same upstream stage.

Rendering uses ANSI cursor movement over the last few blocks rather than clearing the
screen, so scrollback survives and the display does not flicker.
"""

from __future__ import annotations

import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar, TextIO

from ..core.interfaces import Module
from ..core.types import TextFrame

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
WHITE = "\033[97m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
GREEN = "\033[92m"


@dataclass
class Block:
    """One utterance's worth of screen: an English line and a translated line."""

    segment_id: str
    english: str = ""
    english_rev: int = -1
    corrected: bool = False
    translations: dict[str, str] = field(default_factory=dict)
    first_seen: float = field(default_factory=time.time)
    latency_ms: float = 0.0
    lines_drawn: int = 0
    final: bool = False


class PrettyStdoutSink(Module):
    """Args:
        show_latency: print each block's end-of-speech-to-display delay. On by default
            because the spec asks for the delay to be demonstrated live.
        keep: how many recent blocks stay repaintable. Older blocks scroll away and
            become immutable history.
        colour: disable for a plain log or a non-tty.
    """

    inputs: ClassVar[dict[str, type]] = {"text": TextFrame}
    outputs: ClassVar[dict[str, type]] = {}

    def __init__(
        self,
        show_latency: bool = True,
        keep: int = 6,
        colour: bool | None = None,
        stream: TextIO | None = None,
        **_: Any,
    ):
        super().__init__()
        self.show_latency = show_latency
        self.keep = keep
        self.stream = stream or sys.stdout
        self.colour = self.stream.isatty() if colour is None else colour
        self._blocks: list[Block] = []
        self._index: dict[str, Block] = {}
        self._drawn_lines = 0

    # -- painting -------------------------------------------------------------

    def _c(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.colour else text

    @property
    def _width(self) -> int:
        return max(40, shutil.get_terminal_size((100, 24)).columns)

    def _render(self, block: Block) -> list[str]:
        lines: list[str] = []
        marker = "" if block.final else self._c(DIM, " ...")
        flag = self._c(GREEN, " *") if block.corrected else ""
        lines.append(self._c(BOLD + WHITE, f"EN  {block.english}") + flag + marker)
        for lang, text in block.translations.items():
            lines.append(self._c(CYAN, f"{lang.upper():<3} {text}"))
        if self.show_latency and block.latency_ms:
            lines.append(self._c(DIM, f"    {block.latency_ms:.0f} ms"))
        # Wrapped lines occupy more than one terminal row; count them so the cursor
        # arithmetic below moves by the right amount.
        return lines

    def _repaint(self) -> None:
        visible = self._blocks[-self.keep :]
        if self._drawn_lines:
            self.stream.write(f"\033[{self._drawn_lines}A")  # up to the top of the area
        width = self._width
        rows = 0
        for block in visible:
            for line in self._render(block):
                self.stream.write("\033[2K" + line + "\n")
                # A line longer than the terminal wraps onto extra rows.
                rows += max(1, (_visible_len(line) - 1) // width + 1)
            self.stream.write("\033[2K\n")
            rows += 1
        self._drawn_lines = rows
        self.stream.flush()

    def _append_plain(self, block: Block) -> None:
        """Non-tty fallback: append, never rewrite. A log file should keep the history of
        what was shown, including the uncorrected version."""
        for line in self._render(block):
            self.stream.write(line + "\n")
        self.stream.write("\n")
        self.stream.flush()

    # -- sink interface -------------------------------------------------------

    async def process(self, frame: TextFrame) -> None:
        block = self._index.get(frame.segment_id)
        if block is None:
            block = Block(segment_id=frame.segment_id)
            self._index[frame.segment_id] = block
            self._blocks.append(block)
            if len(self._blocks) > self.keep * 3:  # bound memory on a long lecture
                dropped = self._blocks[: -self.keep * 2]
                self._blocks = self._blocks[-self.keep * 2 :]
                for old in dropped:
                    self._index.pop(old.segment_id, None)

        if frame.lang.startswith("en"):
            # Never let a stale revision overwrite a newer one; with per-subscriber
            # queues, two topics can deliver slightly out of order.
            if frame.revision >= block.english_rev:
                block.english = frame.text
                block.english_rev = frame.revision
                block.corrected = bool(frame.meta.get("corrected"))
        else:
            block.translations[frame.lang] = frame.text

        if frame.is_final:
            block.final = True
        if frame.end_to_end_ms:
            block.latency_ms = max(block.latency_ms, frame.end_to_end_ms)

        if self.colour:
            self._repaint()
        elif not frame.lang.startswith("en") or (frame.is_final and not block.translations):
            self._append_plain(block)

    async def stop(self) -> None:
        if self.colour and self._blocks:
            self._repaint()
        self.stream.write("\n")
        self.stream.flush()

    def describe(self) -> dict[str, Any]:
        return {"stage": "PrettyStdoutSink", "name": self.name,
                "blocks": len(self._blocks)}


def _visible_len(text: str) -> int:
    """Length ignoring ANSI escapes, so wrapped-row arithmetic stays correct."""
    out, i = 0, 0
    while i < len(text):
        if text[i] == "\033":
            while i < len(text) and text[i] not in "m":
                i += 1
            i += 1
        else:
            out += 1
            i += 1
    return out

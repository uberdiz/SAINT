"""
ui/components/activity_item.py

Rows of what SAINT did:

    TrailView   the agent's activity trail — 15:03:24  ACTION  Open VS Code — coloured by kind,
                newest at the bottom (a task's details) or top (the Overview feed)
    ToolRow     one tool call as it runs and finishes (the live feed, the overlay)
"""

import re
import time
from typing import Iterable, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ui.design import tokens
from ui.reactive import ui_bus
from ui.theme import current_palette
from ui.widgets import ElidedLabel, StatusDot, chip, clear_layout, set_chip

KIND_COLOR = {"TASK_START": "accent", "OBSERVE": "info", "PLAN": "info", "ACTION": "text", "VERIFY": "success",
              "ERROR": "danger", "RECOVERY": "warning", "WAIT": "warning", "PAUSE": "muted", "RESUME": "muted",
              "COMPLETE": "success", "FAILED": "danger", "CANCELLED": "muted", "LEARN": "accent", "NOTE": "muted"}


class TrailRow(QFrame):
    def __init__(self, entry: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("TrailRow")
        p = current_palette()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(tokens.SPACE_SM)
        when = QLabel(time.strftime("%H:%M:%S", time.localtime(entry.get("at", time.time()))))
        when.setObjectName("TrailTime")
        lay.addWidget(when, 0, Qt.AlignTop)
        kind = entry.get("kind", "")
        k = QLabel(kind.replace("_", " "))
        k.setObjectName("TrailKind")
        k.setFixedWidth(76)
        k.setStyleSheet(f"color: {getattr(p, KIND_COLOR.get(kind, 'muted'))};")
        lay.addWidget(k, 0, Qt.AlignTop)
        text = QLabel(entry.get("text", ""))
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(text, 1)


class TrailView(QWidget):
    """A list of trail entries (dicts with at / kind / text)."""

    def __init__(self, newest_first: bool = False, limit: int = 60, parent=None):
        super().__init__(parent)
        self._newest_first = newest_first
        self._limit = limit
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(0)
        self.empty = QLabel("Nothing yet.")
        self.empty.setObjectName("Faint")
        self.lay.addWidget(self.empty)

    def set_entries(self, entries: Iterable[dict], empty_text: str = "Nothing yet."):
        clear_layout(self.lay)
        rows: List[dict] = list(entries)[-self._limit:]
        if self._newest_first:
            rows.reverse()
        if not rows:
            self.empty = QLabel(empty_text)
            self.empty.setObjectName("Faint")
            self.empty.setWordWrap(True)
            self.lay.addWidget(self.empty)
            self.updateGeometry()
            return
        for e in rows:
            self.lay.addWidget(TrailRow(e))
        self.updateGeometry()


# ---------------------------------------------------------------------- #
# Tool calls (the live feed) — moved here from the old Home page
# ---------------------------------------------------------------------- #
def tool_label(tool: str) -> str:
    """Human description of a tool — never its internal id."""
    try:
        from modules.automation.tools import get_tool_registry
        t = get_tool_registry().get(tool)
        if t is not None and t.description:
            d = re.sub(r"\s*\([^)]*\)", "", t.description)
            return re.split(r"[:,;]| — ", d)[0].strip()[:56]
    except Exception:
        pass
    from core.assistant_state import tool_activity
    return tool_activity(tool)


class ToolRow(QFrame):
    def __init__(self, tool: str):
        super().__init__()
        self.tool = tool
        self.running = True
        self.demo = ui_bus.demo
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(10)
        self.dot = StatusDot(size=8)
        lay.addWidget(self.dot)
        name = ElidedLabel(tool_label(tool))
        lay.addWidget(name, 1)
        self.badge = chip("running", "accent")
        lay.addWidget(self.badge)
        self.blink(True)

    def blink(self, on: bool):
        p = current_palette()
        if self.running:
            self.dot.set_color(p.accent if on else p.border_strong)

    def done(self, ok: bool, ms: float, error: str = ""):
        self.running = False
        p = current_palette()
        self.dot.set_color(p.success if ok else p.danger)
        set_chip(self.badge, f"{ms:.0f} ms" if ok else "failed", "" if ok else "err")
        if error:
            self.setToolTip(error)


def drop_demo_rows(rows: list, empty_label):
    for r in [r for r in rows if r.demo]:
        rows.remove(r)
        r.deleteLater()
    empty_label.setVisible(not rows)

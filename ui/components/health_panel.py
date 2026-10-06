"""
ui/components/health_panel.py

System health: every subsystem SAINT started (core/startup.py), whether it's ready, and — when it
isn't — why, with what can be done about it right there:

    TTS            ⚠ Failed to initialize
                   CUDA backend unavailable
                   [Retry] [Use CPU] [Settings]

One broken part never stops SAINT; this is where it says so. Used on the Overview (compact) and the
System page (full).
"""

import re
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from core.config import config
from core.events import EventType
from ui.components.panel import Panel
from ui.design import tokens
from ui.reactive import ui_bus
from ui.theme import current_palette
from ui.widgets import ElidedLabel, StatusDot, clear_layout, run_async

# Startup keys -> the Settings section that fixes them.
SETTINGS_FOR = {"tts": "Voice", "voice": "Voice", "ai": "AI", "spotify": "Spotify", "link": "Devices",
                "mcp": "MCP", "automation": "Automation", "game_mode": "Gaming Mode"}
WORDS = {"ok": "Ready", "failed": "Needs attention", "off": "Off"}


def gpu_failure(detail: str) -> bool:
    return bool(re.search(r"\bcuda\b|\bgpu\b|cudnn|nvidia|out of memory|device-side", detail or "", re.I))


class HealthRow(QFrame):
    def __init__(self, step: Dict, on_retry: Callable[[str], None], on_settings: Callable[[str], None],
                 compact: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("HealthRow")
        p = current_palette()
        status = step.get("status", "")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 6)
        lay.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(tokens.SPACE_SM)
        dot = StatusDot(size=8)
        dot.set_color({"ok": p.success, "failed": p.warning, "off": p.faint}.get(status, p.muted))
        top.addWidget(dot)
        name = ElidedLabel(step.get("label", step.get("key", "")))
        top.addWidget(name, 1)
        word = QLabel(WORDS.get(status, "Starting…"))
        word.setObjectName({"ok": "OkText", "failed": "WarnText"}.get(status, "Faint"))
        top.addWidget(word)
        lay.addLayout(top)
        if status == "failed":
            why = QLabel(step.get("detail") or "It didn't start.")
            why.setObjectName("Muted")
            why.setWordWrap(True)
            why.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lay.addWidget(why)
            buttons = QHBoxLayout()
            buttons.setSpacing(6)
            key = step.get("key", "")
            if step.get("retryable"):
                retry = QPushButton("Retry")
                retry.clicked.connect(lambda: (retry.setEnabled(False), on_retry(key)))
                buttons.addWidget(retry)
            if key == "tts" and gpu_failure(step.get("detail", "")):
                cpu = QPushButton("Use CPU")
                cpu.setToolTip("Run the voice on the processor instead of the graphics card")
                cpu.clicked.connect(lambda: (cpu.setEnabled(False), on_retry("tts:cpu")))
                buttons.addWidget(cpu)
            if key in SETTINGS_FOR:
                go = QPushButton("Settings")
                go.setObjectName("Ghost")
                go.clicked.connect(lambda: on_settings(SETTINGS_FOR[key]))
                buttons.addWidget(go)
            buttons.addStretch()
            lay.addLayout(buttons)


class HealthPanel(Panel):
    """``compact``: problems in full, the healthy ones as one summary line."""

    def __init__(self, shell=None, compact: bool = False, parent=None):
        super().__init__("System health", parent=parent)
        self.shell = shell
        self.compact = compact
        self.rows = QVBoxLayout()
        self.rows.setSpacing(0)
        self.body.addLayout(self.rows)
        self.summary = QLabel("")
        self.summary.setObjectName("Faint")
        self.summary.setWordWrap(True)
        self.body.addWidget(self.summary)
        self._sig = None
        ui_bus.event.connect(self._on_event)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(5000)              # catches subsystems that report late (speech output loads async)
        self.refresh()

    def _on_event(self, ev):
        if ev.type in (EventType.STARTUP_STATUS, EventType.SPOTIFY_CONNECTED, EventType.SPOTIFY_DISCONNECTED,
                       EventType.WAKE_STATUS, EventType.WAKE_ERROR):
            self.refresh()

    def steps(self) -> List[Dict]:
        from core.startup import startup
        return startup.steps()

    def refresh(self):
        if not self.isVisible() and self._sig is not None:
            return
        steps = self.steps()
        sig = tuple((s["key"], s["status"], s["detail"]) for s in steps)
        if sig == self._sig:
            return
        self._sig = sig
        clear_layout(self.rows)
        ok = [s for s in steps if s["status"] == "ok"]
        bad = [s for s in steps if s["status"] == "failed"]
        off = [s for s in steps if s["status"] == "off"]
        shown = bad + ([] if self.compact else ok + off)
        for s in shown:
            self.rows.addWidget(HealthRow(s, self._retry, self._settings, self.compact))
        if not steps:
            self.summary.setText("SAINT is still starting…")
        elif self.compact:
            names = ", ".join(s["label"].split(" (")[0] for s in ok[:6])
            self.summary.setText((f"{len(ok)} ready: {names}" + ("…" if len(ok) > 6 else "")) if ok else "")
        else:
            self.summary.setText("")
        self.summary.setVisible(bool(self.summary.text()))
        self.set_hint(f"{len(ok)} ready" + (f" · {len(bad)} need attention" if bad else "")
                      + (f" · {len(off)} off" if off else "") if steps else "")
        self.set_active(bool(bad))
        self.relayout()

    def showEvent(self, e):
        super().showEvent(e)
        self._sig = None
        self.refresh()

    def _retry(self, key: str):
        def go():
            from core.startup import startup
            if key == "tts:cpu":
                config.set("voice.tts_device", "cpu")
                return startup.retry("tts")
            return startup.retry(key)
        run_async(go, lambda _ok: self._force(), lambda _e: self._force())

    def _force(self):
        self._sig = None
        self.refresh()

    def _settings(self, section: str):
        if self.shell is not None:
            self.shell.open_settings(section)

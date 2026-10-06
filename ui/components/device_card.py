"""
ui/components/device_card.py

Paired devices at a glance:

    Home PC                     iPhone
    ● Connected                 ● Connected
    Wi-Fi · 8 ms                Internet · seen 4 s ago

Data from ``LinkService.device_states()`` (no network traffic); the latency is measured now and
then while the list is on screen (one status.get per connected device).
"""

import time
from typing import Dict, List

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout

from ui import icons
from ui.components.panel import Panel
from ui.design import tokens
from ui.theme import current_palette
from ui.widgets import ElidedLabel, StatusDot, clear_layout, run_async


def _seen(ts: float) -> str:
    if not ts:
        return "never connected"
    s = max(0, int(time.time() - ts))
    if s < 60:
        return f"seen {s} s ago"
    if s < 3600:
        return f"seen {s // 60} min ago"
    if s < 86400:
        return f"seen {s // 3600} h ago"
    return f"seen {s // 86400} d ago"


def describe(d: Dict) -> str:
    """'Wi-Fi · 8 ms' / 'Internet · connected 3 min' / 'seen 2 h ago'."""
    if d.get("connected"):
        parts = [d.get("transport") or "Connected"]
        if d.get("rtt_ms") is not None:
            parts.append(f"{d['rtt_ms']:.0f} ms")
        return " · ".join(parts)
    return _seen(d.get("last_seen", 0))


class DeviceCard(QFrame):
    def __init__(self, d: Dict, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        p = current_palette()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        kind = "smartphone" if (d.get("platform") or "").lower() in ("ios", "iphone", "android") else "cpu"
        ic = QLabel()
        ic.setPixmap(icons.pixmap(kind, p.muted, 15))
        top.addWidget(ic)
        name = ElidedLabel(d.get("name", "Device"))
        name.setStyleSheet("font-weight: 600;")
        top.addWidget(name, 1)
        if d.get("role") == "collaborator":
            friend = QLabel("friend")
            friend.setObjectName("Chip")
            top.addWidget(friend)
        lay.addLayout(top)
        state = QHBoxLayout()
        state.setSpacing(6)
        dot = StatusDot(size=7)
        dot.set_color(p.success if d.get("connected") else p.faint)
        state.addWidget(dot)
        word = QLabel("Connected" if d.get("connected") else "Not connected")
        word.setObjectName("OkText" if d.get("connected") else "Faint")
        state.addWidget(word)
        state.addStretch()
        lay.addLayout(state)
        how = ElidedLabel(describe(d))
        how.setObjectName("Faint")
        lay.addWidget(how)


class DevicesPanel(Panel):
    """Every paired device as a small card (Overview). Hidden detail lives on the Devices page."""

    def __init__(self, shell=None, parent=None):
        super().__init__("Devices", parent=parent)
        self.shell = shell
        self.grid = QGridLayout()
        self.grid.setSpacing(tokens.SPACE_SM)
        self.body.addLayout(self.grid)
        self._sig = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._lat = QTimer(self)
        self._lat.timeout.connect(self._measure)

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()
        self._timer.start(3000)
        self._lat.start(15000)
        QTimer.singleShot(400, self._measure)

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()
        self._lat.stop()

    @staticmethod
    def _link():
        from modules.link.service import get_link
        return get_link()

    def refresh(self):
        try:
            link = self._link()
            states: List[Dict] = link.device_states() if link.running else \
                [dict(d, transport="", rtt_ms=None) for d in link.devices()]
            on = link.running
        except Exception:
            states, on = [], False
        sig = tuple((d["id"], d["name"], d.get("connected"), d.get("transport"), d.get("rtt_ms"),
                     int(d.get("last_seen", 0) // 30)) for d in states) + (on,)
        if sig == self._sig:
            return
        self._sig = sig
        clear_layout(self.grid)
        connected = sum(1 for d in states if d.get("connected"))
        self.set_hint(("SAINT Link is off" if not on else f"{connected} of {len(states)} connected") if states or not on
                      else "")
        if not states:
            empty = QLabel("No devices yet. Devices → Show pairing code, then scan it with SAINT on your phone."
                           if on else "Turn on SAINT Link on the Devices page to connect your phone and other PCs.")
            empty.setObjectName("Faint")
            empty.setWordWrap(True)
            self.grid.addWidget(empty, 0, 0)
            self.relayout()
            return
        cols = 2 if self.width() < 560 else 3
        for i, d in enumerate(sorted(states, key=lambda x: (not x.get("connected"), x["name"].lower()))[:9]):
            self.grid.addWidget(DeviceCard(d), i // cols, i % cols)
        self.relayout()

    def _measure(self):
        def go():
            link = self._link()
            if not link.running:
                return False
            for d in link.devices():
                if d.get("connected"):
                    link.measure_latency(d["id"])
            return True
        run_async(go, lambda _r: (setattr(self, "_sig", None), self.refresh()), lambda _e: None)

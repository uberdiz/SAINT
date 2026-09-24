"""
ui/widgets.py

Reusable widgets for the SAINT interface.
"""

import html
import math
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRectF, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QRadialGradient
from PySide6.QtWidgets import QFrame, QLabel, QTextBrowser, QVBoxLayout, QWidget, QHBoxLayout

from core.config import config
from ui.theme import current_palette, state_color


# ---------------------------------------------------------------------- #
# Background work (never block the GUI thread)
# ---------------------------------------------------------------------- #
class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn, signals):
        super().__init__()
        self.fn, self.signals = fn, signals

    def run(self):
        try:
            result = self.fn()
        except Exception as e:  # noqa: BLE001
            self.signals.failed.emit(str(e))
            return
        self.signals.done.emit(result)


_alive = set()


def run_async(fn: Callable, on_done: Optional[Callable] = None, on_error: Optional[Callable] = None):
    """Run ``fn`` on the thread pool; callbacks run on the GUI thread."""
    sig = _Signals()
    _alive.add(sig)

    def finish(cb, arg):
        _alive.discard(sig)
        if cb:
            cb(arg)
    sig.done.connect(lambda r: finish(on_done, r))
    sig.failed.connect(lambda e: finish(on_error, e))
    QThreadPool.globalInstance().start(_Task(fn, sig))


# ---------------------------------------------------------------------- #
# Card
# ---------------------------------------------------------------------- #
class Card(QFrame):
    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 14)
        self.body.setSpacing(8)
        self.header = QHBoxLayout()
        self.title_label = QLabel(title.upper())
        self.title_label.setObjectName("CardTitle")
        self.header.addWidget(self.title_label)
        self.header.addStretch()
        if title:
            self.body.addLayout(self.header)


# ---------------------------------------------------------------------- #
# State orb
# ---------------------------------------------------------------------- #
class StateOrb(QWidget):
    """Animated orb whose colour and motion reflect the real assistant state."""

    def __init__(self, parent=None, size: int = 132):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._state = "offline"
        self._level = 0.0
        self._phase = 0.0
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

    def set_state(self, state: str):
        self._state = state
        self.update()

    def set_level(self, level: float):
        self._level = 0.8 * self._level + 0.2 * max(0.0, min(1.0, level))

    def _tick(self):
        if not config.get("appearance.animations", True):
            if self._phase != 0.0:
                self._phase = 0.0
                self.update()
            return
        self._phase = time.monotonic() - self._t0
        if self.isVisible():
            self.update()

    def paintEvent(self, _):
        p = current_palette()
        color = QColor(state_color(self._state, p))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        base_r = min(w, h) * 0.30
        t = self._phase
        anim = config.get("appearance.animations", True)

        pulse = 0.0
        if anim:
            if self._state in ("wake_listening",):
                pulse = 0.04 * math.sin(t * 2.0)
            elif self._state in ("command_listening", "listening", "wake_detected"):
                pulse = 0.05 * math.sin(t * 5.0) + 0.25 * self._level
            elif self._state == "speaking":
                pulse = 0.08 * abs(math.sin(t * 7.0))
            elif self._state in ("processing", "executing", "observing"):
                pulse = 0.03 * math.sin(t * 3.0)
        r = base_r * (1.0 + pulse)

        # glow
        glow = QRadialGradient(cx, cy, r * 1.9)
        g = QColor(color)
        g.setAlpha(90 if self._state not in ("offline", "idle") else 30)
        glow.setColorAt(0.0, g)
        g2 = QColor(color)
        g2.setAlpha(0)
        glow.setColorAt(1.0, g2)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(glow))
        painter.drawEllipse(QRectF(cx - r * 1.9, cy - r * 1.9, r * 3.8, r * 3.8))

        # core
        core = QRadialGradient(cx - r * 0.3, cy - r * 0.35, r * 1.3)
        core.setColorAt(0.0, color.lighter(150))
        core.setColorAt(1.0, color.darker(140))
        painter.setBrush(QBrush(core))
        painter.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))

        # spinning arc while processing / executing
        if anim and self._state in ("processing", "executing", "observing"):
            pen = QPen(color.lighter(130), 3)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            rr = r * 1.35
            start = int((t * 360) % 360 * 16)
            painter.drawArc(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr), -start, 100 * 16)
        # ripple when the wake word fires / speaking
        if anim and self._state in ("wake_detected", "speaking"):
            k = (t * 1.2) % 1.0
            rr = r * (1.2 + 0.8 * k)
            c = QColor(color)
            c.setAlpha(int(160 * (1 - k)))
            painter.setPen(QPen(c, 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr))
        painter.end()


# ---------------------------------------------------------------------- #
# Level meter with threshold marker
# ---------------------------------------------------------------------- #
class LevelMeter(QWidget):
    def __init__(self, parent=None, show_threshold=False):
        super().__init__(parent)
        self.setFixedHeight(10)
        self.setMinimumWidth(120)
        self._value = 0.0
        self._peak = 0.0
        self._threshold = None if not show_threshold else 0.5
        self._decay = QTimer(self)
        self._decay.timeout.connect(self._fall)
        self._decay.start(50)

    def set_value(self, v: float):
        self._value = max(0.0, min(1.0, float(v)))
        self._peak = max(self._peak, self._value)
        self.update()

    def set_threshold(self, t):
        self._threshold = t
        self.update()

    def _fall(self):
        if self._peak > self._value:
            self._peak = max(self._value, self._peak - 0.02)
            self.update()

    def paintEvent(self, _):
        p = current_palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(p.surface2))
        painter.drawRoundedRect(0, 0, w, h, h / 2, h / 2)
        over = self._threshold is not None and self._value >= self._threshold
        painter.setBrush(QColor(p.success if over else p.accent))
        painter.drawRoundedRect(0, 0, int(w * self._value), h, h / 2, h / 2)
        if self._peak > 0.01:
            painter.setBrush(QColor(p.muted))
            painter.drawRect(int(w * self._peak) - 1, 0, 2, h)
        if self._threshold is not None:
            painter.setBrush(QColor(p.warning))
            painter.drawRect(int(w * self._threshold) - 1, 0, 2, h)
        painter.end()


# ---------------------------------------------------------------------- #
# Chat view
# ---------------------------------------------------------------------- #
class ChatView(QTextBrowser):
    """Conversation transcript with streaming support."""

    def __init__(self, parent=None, max_messages=60):
        super().__init__(parent)
        self.setObjectName("ChatView")
        self.setOpenExternalLinks(False)
        self._messages = []          # [role, text, meta]
        self._max = max_messages
        self._streaming = False

    def add(self, role: str, text: str, meta: str = ""):
        self._messages.append([role, text, meta])
        self._messages = self._messages[-self._max:]
        self._render()

    def begin_stream(self):
        self._messages.append(["assistant", "", ""])
        self._streaming = True
        self._render()

    def stream(self, token: str):
        if not self._streaming:
            self.begin_stream()
        self._messages[-1][1] += token
        self._render()

    def end_stream(self, final_text: Optional[str] = None, meta: str = ""):
        if self._streaming:
            if final_text is not None:
                self._messages[-1][1] = final_text
            if meta:
                self._messages[-1][2] = meta
            if not self._messages[-1][1].strip():
                self._messages.pop()
        self._streaming = False
        self._render()

    def annotate_last(self, meta: str):
        if self._messages:
            self._messages[-1][2] = meta
            self._render()

    def clear_chat(self):
        self._messages.clear()
        self._streaming = False
        self._render()

    def _render(self):
        p = current_palette()
        rows = []
        for role, text, meta in self._messages:
            safe = html.escape(text).replace("\n", "<br>")
            # Qt rich text ignores div padding/radius, so bubbles are padded table cells.
            meta_html = (f'<p style="color:{p.faint}; font-size:11px; margin-top:3px;" align="{{a}}">'
                         f'{html.escape(meta)}</p>') if meta else ""
            if role == "user":
                rows.append(
                    f'<table width="100%" cellspacing="0" cellpadding="0"><tr><td width="22%"></td><td>'
                    f'<table align="right" cellspacing="0" cellpadding="9" bgcolor="{p.accent_soft}">'
                    f'<tr><td style="color:{p.text};">{safe}</td></tr></table>'
                    f'{meta_html.replace("{a}", "right")}</td></tr></table>')
            elif role == "system":
                rows.append(f'<p style="color:{p.faint}; font-size:11px;" align="center">{safe}</p>')
            else:
                color = p.danger if meta.startswith("error") else p.text
                rows.append(
                    f'<table width="100%" cellspacing="0" cellpadding="0"><tr><td>'
                    f'<p style="color:{p.accent}; font-size:11px; font-weight:700; margin-bottom:3px;">SAINT</p>'
                    f'<table cellspacing="0" cellpadding="9" bgcolor="{p.surface2}">'
                    f'<tr><td style="color:{color};">{safe or "…"}</td></tr></table>'
                    f'{meta_html.replace("{a}", "left")}</td><td width="22%"></td></tr></table>')
        self.setHtml(f'<body style="background:{p.surface};">' + '<p style="font-size:6px;">&nbsp;</p>'.join(rows)
                     + "</body>")
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


# ---------------------------------------------------------------------- #
# Small status pill
# ---------------------------------------------------------------------- #
class StatusDot(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self.set_color("#6b7076")

    def set_color(self, color: str):
        self.setStyleSheet(f"background:{color}; border-radius:5px;")

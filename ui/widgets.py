"""
ui/widgets.py

Building blocks shared by every SAINT window: background work, cards, chips,
icon buttons, the state orb, meters, charts, cover art and the chat view.
"""

import html
import math
import time
from collections import OrderedDict
from typing import Callable, Optional

from PySide6.QtCore import (QObject, QPointF, QRectF, QRunnable, QSize, Qt, QThreadPool, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QFontMetrics, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QTextBrowser,
                               QToolButton, QVBoxLayout, QWidget)

from ui import icons, motion
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
            try:
                cb(arg)
            except RuntimeError:         # the widget that asked was closed meanwhile
                pass
    sig.done.connect(lambda r: finish(on_done, r))
    sig.failed.connect(lambda e: finish(on_error, e))
    QThreadPool.globalInstance().start(_Task(fn, sig))


def with_alpha(color, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(max(0, min(255, int(alpha))))
    return c


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            clear_layout(item.layout())


# ---------------------------------------------------------------------- #
# Layout atoms
# ---------------------------------------------------------------------- #
class Card(QFrame):
    def __init__(self, title: str = "", parent=None, object_name: str = "Card"):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(10)
        self.header = QHBoxLayout()
        self.header.setSpacing(6)
        self.title_label = QLabel(title.upper())
        self.title_label.setObjectName("CardTitle")
        self.header.addWidget(self.title_label)
        self.header.addStretch()
        if title:
            self.body.addLayout(self.header)


class Page(QWidget):
    """A main-window page: title, subtitle, action row, then content."""

    def __init__(self, title: str, subtitle: str = ""):
        super().__init__()
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(36, 28, 36, 24)
        self.root.setSpacing(18)
        head = QHBoxLayout()
        head.setSpacing(8)
        col = QVBoxLayout()
        col.setSpacing(3)
        self.title = QLabel(title)
        self.title.setObjectName("PageTitle")
        col.addWidget(self.title)
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("PageSubtitle")
        self.subtitle.setWordWrap(True)
        self.subtitle.setVisible(bool(subtitle))
        col.addWidget(self.subtitle)
        head.addLayout(col, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        head.addLayout(self.actions)
        self.root.addLayout(head)


def chip(text: str, kind: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName({"accent": "ChipAccent", "ok": "ChipOk", "warn": "ChipWarn", "err": "ChipErr"}.get(kind, "Chip"))
    return lbl


def set_chip(lbl: QLabel, text: str, kind: str = ""):
    lbl.setText(text)
    name = {"accent": "ChipAccent", "ok": "ChipOk", "warn": "ChipWarn", "err": "ChipErr"}.get(kind, "Chip")
    if lbl.objectName() != name:
        lbl.setObjectName(name)
        lbl.style().unpolish(lbl)
        lbl.style().polish(lbl)


def kbd(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("Kbd")
    return lbl


class ElidedLabel(QLabel):
    """Single-line label that ends in … instead of stretching its parent."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = text
        self.setMinimumWidth(10)
        super().setText(text)

    def setText(self, text):
        self._full = text or ""
        self._elide()

    def text(self):
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        super().setText(QFontMetrics(self.font()).elidedText(self._full, Qt.ElideRight, max(10, self.width())))
        self.setToolTip(self._full if self.fontMetrics().horizontalAdvance(self._full) > self.width() else "")

    def sizeHint(self):
        s = super().sizeHint()
        return QSize(min(s.width(), 260), s.height())


class IconButton(QToolButton):
    """Borderless icon button; re-tints itself when the theme changes."""

    def __init__(self, name: str, tip: str = "", size: int = 18, checkable: bool = False,
                 role: str = "muted", parent=None):
        super().__init__(parent)
        self.setObjectName("IconButton")
        self._name, self._size, self._role = name, size, role
        self.setCheckable(checkable)
        self.setAutoRaise(True)
        self.setToolTip(tip)
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(size, size))
        self.refresh()

    def set_icon(self, name: str):
        self._name = name
        self.refresh()

    def refresh(self):
        p = current_palette()
        base = p.bg if self._role == "inverse" else getattr(p, self._role, p.muted)
        self.setIcon(icons.icon(self._name, base, self._size,
                                active_color=None if self._role == "inverse" else p.text))


class Segmented(QFrame):
    changed = Signal(int)

    def __init__(self, options, parent=None):
        super().__init__(parent)
        self.setObjectName("SegmentBar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        for i, text in enumerate(options):
            b = QPushButton(text.replace("&", "&&"))
            b.setObjectName("Segment")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            self.group.addButton(b, i)
            lay.addWidget(b)
        self.group.button(0).setChecked(True)
        self.group.idClicked.connect(self.changed)

    def index(self) -> int:
        return self.group.checkedId()

    def set_index(self, i: int):
        self.group.button(i).setChecked(True)


# ---------------------------------------------------------------------- #
# State orb
# ---------------------------------------------------------------------- #
class Orb(QWidget):
    """SAINT's presence: colour, breathing, rings and ripples follow the real state."""

    def __init__(self, size: int = 148, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._state = "offline"
        self._target = QColor(state_color("offline", current_palette()))
        self._color = QColor(self._target)
        self._level = 0.0
        self._flash = 0.0
        self._t0 = time.monotonic()
        self._t = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_state(self, state: str):
        self._state = state or "offline"
        self._target = QColor(state_color(self._state, current_palette()))
        if not motion.enabled():
            self._color = QColor(self._target)
        self.update()

    def set_level(self, level: float):
        self._level = max(self._level * 0.6, max(0.0, min(1.0, float(level))))

    def flash(self):
        self._flash = 1.0

    def showEvent(self, e):
        super().showEvent(e)
        if motion.enabled():
            self._timer.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def _tick(self):
        if not motion.enabled():
            self._timer.stop()
            self._color = QColor(self._target)
            self.update()
            return
        self._t = time.monotonic() - self._t0
        c, g, k = self._color, self._target, 0.12
        self._color = QColor(int(c.red() + (g.red() - c.red()) * k), int(c.green() + (g.green() - c.green()) * k),
                             int(c.blue() + (g.blue() - c.blue()) * k))
        self._flash *= 0.92
        self._level *= 0.94
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        c = QPointF(w / 2, h / 2)
        s, t, col = self._state, self._t, QColor(self._color)
        anim = motion.enabled()
        listening = s in ("wake_detected", "command_listening", "listening")
        busy = s in ("processing", "executing", "observing")
        active = s != "offline"
        big = w >= 48

        r = min(w, h) * (0.25 if big else 0.3)
        scale = 1.0
        if anim:
            if s in ("wake_listening", "idle"):
                scale += 0.035 * math.sin(t * 1.6)
            elif listening:
                scale += 0.05 * math.sin(t * 5) + 0.32 * self._level
            elif s == "speaking":
                scale += 0.07 * abs(math.sin(t * 6.5))
            elif busy:
                scale += 0.025 * math.sin(t * 3)
            scale += 0.14 * self._flash
        rr = r * scale

        # glow
        a = (70 if active else 22) + 90 * self._flash + (70 * self._level if listening else 0)
        glow = QRadialGradient(c, rr * 2.3)
        glow.setColorAt(0.0, with_alpha(col, min(210, a)))
        glow.setColorAt(0.45, with_alpha(col, a / 3.2))
        glow.setColorAt(1.0, with_alpha(col, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(c, rr * 2.3, rr * 2.3)

        if big and active:
            ring = r * 1.6
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(with_alpha(col, 26), 1))
            p.drawEllipse(c, ring, ring)
            if anim:
                speed = 240 if busy else 70 if listening else 22
                cg = QConicalGradient(c, -(t * speed) % 360)
                cg.setColorAt(0.0, with_alpha(col, 235 if busy else 150))
                cg.setColorAt(0.3, with_alpha(col, 0))
                cg.setColorAt(0.7, with_alpha(col, 0))
                cg.setColorAt(1.0, with_alpha(col, 70))
                pen = QPen(QBrush(cg), 2.2 if busy else 1.6)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.drawEllipse(c, ring, ring)
            if anim and s in ("speaking", "wake_detected"):
                for i in range(3):
                    k = (t * 0.9 + i / 3) % 1.0
                    rad = rr * (1.12 + 1.05 * k)
                    p.setPen(QPen(with_alpha(col, 150 * (1 - k)), 1.4))
                    p.drawEllipse(c, rad, rad)

        core = QRadialGradient(QPointF(c.x() - rr * 0.35, c.y() - rr * 0.4), rr * 1.7)
        core.setColorAt(0.0, col.lighter(175))
        core.setColorAt(0.45, col)
        core.setColorAt(1.0, col.darker(190))
        p.setPen(Qt.NoPen)
        p.setBrush(core)
        p.drawEllipse(c, rr, rr)
        if big:
            spec = QRadialGradient(QPointF(c.x() - rr * 0.32, c.y() - rr * 0.42), rr * 0.55)
            spec.setColorAt(0.0, with_alpha("#ffffff", 105 if active else 40))
            spec.setColorAt(1.0, with_alpha("#ffffff", 0))
            p.setBrush(spec)
            p.drawEllipse(QPointF(c.x() - rr * 0.3, c.y() - rr * 0.38), rr * 0.5, rr * 0.4)
        p.end()


# ---------------------------------------------------------------------- #
# Meters and charts
# ---------------------------------------------------------------------- #
class LevelMeter(QWidget):
    def __init__(self, parent=None, show_threshold=False):
        super().__init__(parent)
        self.setFixedHeight(6)
        self.setMinimumWidth(80)
        self._value = 0.0
        self._peak = 0.0
        self._threshold = 0.5 if show_threshold else None
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
        g = QPainter(self)
        g.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        g.setPen(Qt.NoPen)
        g.setBrush(QColor(p.surface2))
        g.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        over = self._threshold is not None and self._value >= self._threshold
        g.setBrush(QColor(p.success if over else p.accent))
        if self._value > 0.005:
            g.drawRoundedRect(QRectF(0, 0, max(h, w * self._value), h), h / 2, h / 2)
        if self._peak > 0.01:
            g.setBrush(QColor(p.muted))
            g.drawRect(QRectF(w * self._peak - 1, 0, 2, h))
        if self._threshold is not None:
            g.setBrush(QColor(p.warning))
            g.drawRect(QRectF(w * self._threshold - 1, 0, 2, h))
        g.end()


class ProgressLine(QWidget):
    """Thin rounded progress bar (Spotify position)."""

    def __init__(self, height: int = 4, parent=None):
        super().__init__(parent)
        self.setFixedHeight(height)
        self._v = 0.0
        self.color = None

    def set_value(self, v: float):
        v = max(0.0, min(1.0, v))
        if abs(v - self._v) > 0.0005:
            self._v = v
            self.update()

    def paintEvent(self, _):
        p = current_palette()
        g = QPainter(self)
        g.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        g.setPen(Qt.NoPen)
        g.setBrush(with_alpha(p.text, 30))
        g.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        if self._v > 0:
            g.setBrush(QColor(self.color or p.text))
            g.drawRoundedRect(QRectF(0, 0, max(h, w * self._v), h), h / 2, h / 2)
        g.end()


class Bars(QWidget):
    """Minimal bar chart: one highlighted bar (e.g. today), hover shows the value."""

    def __init__(self, height: int = 72, parent=None):
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setMouseTracking(True)
        self._values, self._labels, self._hi, self._hover = [], [], -1, -1

    def set_data(self, values, labels=None, highlight=-1):
        self._values, self._labels, self._hi = list(values), list(labels or []), highlight
        self.update()

    def _index_at(self, x):
        n = len(self._values)
        return int(x / max(1, self.width()) * n) if n else -1

    def mouseMoveEvent(self, e):
        i = self._index_at(e.position().x())
        if i != self._hover and 0 <= i < len(self._values):
            self._hover = i
            lab = self._labels[i] if i < len(self._labels) else ""
            self.setToolTip(f"{lab}: {self._values[i]}")
            self.update()

    def leaveEvent(self, e):
        self._hover = -1
        self.update()

    def paintEvent(self, _):
        p = current_palette()
        g = QPainter(self)
        g.setRenderHint(QPainter.Antialiasing)
        n = len(self._values)
        if not n:
            g.end()
            return
        w, h = self.width(), self.height()
        top = max(self._values) or 1
        slot = w / n
        bw = max(2.0, slot * 0.62)
        g.setPen(Qt.NoPen)
        for i, v in enumerate(self._values):
            bh = max(2.0, (h - 2) * v / top) if v else 2.0
            x = i * slot + (slot - bw) / 2
            color = p.accent if i == self._hi else (p.muted if i == self._hover else p.border_strong)
            g.setBrush(QColor(color))
            g.drawRoundedRect(QRectF(x, h - bh, bw, bh), min(3, bw / 2), min(3, bw / 2))
        g.end()


# ---------------------------------------------------------------------- #
# Album covers (shared cache; downloads off the GUI thread)
# ---------------------------------------------------------------------- #
class CoverCache(QObject):
    def __init__(self):
        super().__init__()
        self._pix = OrderedDict()
        self._color = {}
        self._waiting = {}

    def put(self, url: str, pm: QPixmap):
        self._pix[url] = pm
        self._color[url] = _dominant(pm)
        while len(self._pix) > 40:
            old, _ = self._pix.popitem(last=False)
            self._color.pop(old, None)

    def get(self, url: str, cb: Callable):
        if not url:
            cb(None)
            return
        if url in self._pix:
            self._pix.move_to_end(url)
            cb(self._pix[url])
            return
        waiting = self._waiting.setdefault(url, [])
        waiting.append(cb)
        if len(waiting) > 1:
            return

        def fetch():
            import requests
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            return r.content
        run_async(fetch, lambda data: self._loaded(url, data), lambda _e: self._loaded(url, b""))

    def color(self, url: str) -> Optional[QColor]:
        return self._color.get(url)

    def _loaded(self, url, data):
        pm = QPixmap()
        if data and pm.loadFromData(data):
            self.put(url, pm)
        else:
            pm = None
        for cb in self._waiting.pop(url, []):
            try:
                cb(pm)
            except RuntimeError:
                pass


def _dominant(pm: QPixmap) -> QColor:
    img = pm.toImage().scaled(1, 1, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    c = img.pixelColor(0, 0)
    hue, sat, val, _ = c.getHsvF()
    return QColor.fromHsvF(max(0.0, hue), min(1.0, sat * 1.35 + 0.1), max(0.55, val))


covers = CoverCache()


class CoverArt(QWidget):
    """Rounded album art that cross-fades between tracks."""

    def __init__(self, size: int = 72, radius: int = 10, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._radius = radius
        self._url = None
        self._pm = None
        self._old = None
        self._mix = 1.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(motion.SLOW)
        self._anim.valueChanged.connect(self._set_mix)

    def set_url(self, url: str):
        if url == self._url:
            return
        self._url = url
        covers.get(url, lambda pm, u=url: self._arrived(u, pm))

    def _arrived(self, url, pm):
        if url != self._url:
            return
        self._old, self._pm = self._pm, pm
        if motion.enabled() and self._old is not None:
            self._anim.stop()
            self._anim.setStartValue(0.0)
            self._anim.setEndValue(1.0)
            self._anim.start()
        else:
            self._set_mix(1.0)

    def _set_mix(self, v):
        self._mix = float(v)
        if self._mix >= 1.0:
            self._old = None
        self.update()

    def paintEvent(self, _):
        p = current_palette()
        g = QPainter(self)
        g.setRenderHint(QPainter.Antialiasing)
        g.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(rect, self._radius, self._radius)
        g.setClipPath(path)
        if self._pm is None or self._mix < 1.0:
            grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
            grad.setColorAt(0, QColor(p.raised))
            grad.setColorAt(1, QColor(p.surface2))
            g.fillRect(rect, grad)
            if self._old is None and self._pm is None:
                s = int(self.width() * 0.36)
                g.drawPixmap(int((self.width() - s) / 2), int((self.height() - s) / 2),
                             icons.pixmap("music", p.faint, s))
        for pm, op in ((self._old, 1.0 - self._mix), (self._pm, self._mix)):
            if pm is not None and op > 0:
                g.setOpacity(op)
                scaled = pm.scaled(self.size() * self.devicePixelRatioF(), Qt.KeepAspectRatioByExpanding,
                                   Qt.SmoothTransformation)
                scaled.setDevicePixelRatio(self.devicePixelRatioF())
                g.drawPixmap(0, 0, scaled)
        g.setOpacity(1.0)
        g.setClipping(False)
        g.setPen(QPen(with_alpha("#ffffff" if p.dark else "#000000", 22), 1))
        g.setBrush(Qt.NoBrush)
        g.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), self._radius, self._radius)
        g.end()


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

    def clear_chat(self):
        self._messages.clear()
        self._streaming = False
        self._render()

    def is_empty(self) -> bool:
        return not self._messages

    def _render(self):
        p = current_palette()
        rows = []
        for role, text, meta in self._messages:
            safe = html.escape(text).replace("\n", "<br>")
            meta_html = (f'<span style="color:{p.faint}; font-size:11px;">{html.escape(meta)}</span>'
                         if meta else "")
            if role == "user":
                rows.append(
                    f'<table width="100%" cellspacing="0" cellpadding="0"><tr><td width="20%"></td><td>'
                    f'<table align="right" cellspacing="0" cellpadding="9" bgcolor="{p.surface2}">'
                    f'<tr><td style="color:{p.text};">{safe}</td></tr></table>'
                    f'<p align="right" style="margin-top:3px;">{meta_html}</p></td></tr></table>')
            elif role == "system":
                rows.append(f'<p style="color:{p.faint}; font-size:11px;" align="center">{safe}</p>')
            else:
                color = p.danger if meta.startswith("error") else p.text
                body = safe or f'<span style="color:{p.faint};">…</span>'
                rows.append(
                    f'<p style="margin:0;"><span style="color:{p.accent}; font-size:11px; font-weight:700;">'
                    f'SAINT</span>&nbsp;&nbsp;{meta_html}</p>'
                    f'<p style="margin-top:4px; color:{color};">{body}</p>')
        self.setHtml('<body>' + '<p style="font-size:8px;">&nbsp;</p>'.join(rows) + "</body>")
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


class StatusDot(QLabel):
    def __init__(self, parent=None, size: int = 8):
        super().__init__(parent)
        self._s = size
        self.setFixedSize(size, size)
        self.set_color("#6b7076")

    def set_color(self, color: str):
        self.setStyleSheet(f"background:{color}; border-radius:{self._s // 2}px;")

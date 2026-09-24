"""
ui/halo.py

The Halo: a soft light that travels around the edge of your monitor while
SAINT runs in the background. Its colour, speed and brightness follow the
real assistant state — a slow orbit while waiting for “Hey SAINT”, a bright
fast sweep while listening (breathing with your voice), twin comets while
thinking, a pulse while speaking. While music plays it takes on the album
cover's colour.

Four thin click-through windows (one per edge) do the drawing, so nothing on
screen is ever blocked. Resting the cursor on the top-centre edge reveals a
small SAINT tab; clicking it opens the overlay.
"""

import math
import time

from PySide6.QtCore import QEasingCurve, QObject, QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from core.config import config
from core.events import EventType
from ui import motion
from ui.reactive import ui_bus
from ui.theme import current_palette, state_color
from ui.widgets import Orb, covers, with_alpha

THICK = 36

# state → (base glow, comet brightness, laps per second, tail length (fraction of perimeter), comets)
PROFILES = {
    "offline": (0.03, 0.30, 1 / 40, 0.08, 1),
    "idle": (0.06, 0.45, 1 / 30, 0.10, 1),
    "wake_listening": (0.10, 0.85, 1 / 14, 0.14, 1),
    "wake_detected": (0.30, 1.00, 1 / 3, 0.16, 1),
    "command_listening": (0.20, 1.00, 1 / 4, 0.14, 1),
    "listening": (0.20, 1.00, 1 / 4, 0.14, 1),
    "processing": (0.16, 1.00, 1 / 2.2, 0.09, 2),
    "observing": (0.16, 1.00, 1 / 2.2, 0.09, 2),
    "executing": (0.18, 1.00, 1 / 1.8, 0.09, 2),
    "speaking": (0.16, 0.90, 1 / 6, 0.20, 1),
    "error": (0.30, 0.0, 1 / 20, 0.10, 1),
}


def halo_brightness(s: float, perimeter: float, head: float, comet: float, tail: float, comets: int,
                    base: float) -> float:
    """Light at perimeter position ``s``: a base glow plus comets whose tails trail behind ``head``."""
    b = base
    tail_px = max(1.0, tail * perimeter)
    for k in range(comets):
        h = (head + k * perimeter / comets) % perimeter
        d = (h - s) % perimeter                         # distance behind the head
        b += comet * math.exp(-d / tail_px)
        b += comet * 0.5 * math.exp(-(perimeter - d) / (tail_px * 0.12))   # small glow just ahead
    return max(0.0, min(1.0, b))


class _Strip(QWidget):
    def __init__(self, halo, edge: str, screen_geo: QRect, rect: QRect):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.halo, self.edge, self.screen_geo = halo, edge, screen_geo
        self.setGeometry(rect)

    def paintEvent(self, _):
        g = QPainter(self)
        self.halo.paint_strip(self, g)
        g.end()


class EdgeTab(QWidget):
    """Small pill that slides down from the top edge; click → overlay."""
    clicked = Signal()

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(212, 44)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 16, 6)
        lay.setSpacing(8)
        self.orb = Orb(26)
        lay.addWidget(self.orb)
        self.label = QLabel("SAINT")
        self.label.setObjectName("Wordmark")
        lay.addWidget(self.label)
        self.state = QLabel("")
        self.state.setObjectName("Faint")
        lay.addWidget(self.state, 1)

    def render(self, s):
        self.orb.set_state(s.get("state", "offline"))
        self.state.setText(s.get("label", ""))

    def paintEvent(self, _):
        p = current_palette()
        g = QPainter(self)
        g.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        g.fillPath(path, with_alpha(p.raised, 245))
        g.setPen(with_alpha(state_color(ui_bus.state.get("state", "offline"), p), 140))
        g.drawPath(path)
        g.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()


class Halo(QObject):
    open_requested = Signal()
    preview_ended = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._strips = []
        self._visible = False
        self.previewing = False
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._end_preview)
        self._t_last = time.monotonic()
        self._t = 0.0
        self._head = 0.0
        self._cur = list(PROFILES["wake_listening"][:4])
        self._comets = 1
        self._color = QColor(current_palette().accent)
        self._flash = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        self.tab = EdgeTab()
        self.tab.clicked.connect(self._tab_clicked)
        self._tab_screen = None
        self._dwell = 0
        self._away = 0
        self._poll = QTimer(self)
        self._poll.setInterval(80)
        self._poll.timeout.connect(self._poll_cursor)

        ui_bus.event.connect(self._on_event)
        app = QGuiApplication.instance()
        app.screenAdded.connect(lambda _s: self._rebuild())
        app.screenRemoved.connect(lambda _s: self._rebuild())

    # ------------------------------------------------------------------ #
    def set_visible(self, on: bool):
        on = bool(on)
        if on == self._visible:
            return
        self._visible = on
        if on:
            self._rebuild()
            self._t_last = time.monotonic()
            self._timer.start(33)
            if config.get("overlay.edge_tab", True):
                self._poll.start()
        else:
            self._timer.stop()
            self._poll.stop()
            self._hide_tab()
            for s in self._strips:
                s.hide()

    @property
    def visible(self):
        return self._visible

    def preview(self, ms: int = 3200):
        """Show the Halo right now for a moment — above everything, even the
        overlay — so a Settings / overlay switch visibly does something."""
        self.previewing = True
        self._flash = 1.0
        self.set_visible(True)
        for s in self._strips:
            s.raise_()
        self._preview_timer.start(ms)

    def _end_preview(self):
        self.previewing = False
        self.preview_ended.emit()

    def refresh(self):
        """Re-read screen / edge-tab settings while visible."""
        if not self._visible:
            return
        self._rebuild()
        if config.get("overlay.edge_tab", True):
            self._poll.start()
        else:
            self._poll.stop()
            self._hide_tab()

    def _screens(self):
        app = QGuiApplication.instance()
        if config.get("overlay.halo_all_screens", False):
            return app.screens()
        return [app.primaryScreen()] if app.primaryScreen() else []

    def _rebuild(self):
        for s in self._strips:
            s.close()
            s.deleteLater()
        self._strips = []
        if not self._visible:
            return
        for sc in self._screens():
            geo, t = sc.geometry(), THICK
            rects = {"top": QRect(geo.x(), geo.y(), geo.width(), t),
                     "bottom": QRect(geo.x(), geo.y() + geo.height() - t, geo.width(), t),
                     "left": QRect(geo.x(), geo.y() + t, t, geo.height() - 2 * t),
                     "right": QRect(geo.x() + geo.width() - t, geo.y() + t, t, geo.height() - 2 * t)}
            for edge, r in rects.items():
                strip = _Strip(self, edge, QRect(geo), r)
                strip.show()
                self._strips.append(strip)

    # ------------------------------------------------------------------ #
    def _target_color(self) -> QColor:
        p = current_palette()
        state = ui_bus.state.get("state", "offline")
        c = QColor(state_color(state, p))
        np = ui_bus.now_playing()
        if state in ("wake_listening", "idle") and np.get("is_playing"):
            url = np.get("cover", "")
            cover = covers.color(url)
            if cover is None and url:
                covers.get(url, lambda _pm: None)
            if cover is not None:
                c = cover
        return c

    def _tick(self):
        now = time.monotonic()
        dt = min(0.1, now - self._t_last)
        self._t_last = now
        self._t += dt
        state = ui_bus.state.get("state", "offline")
        base, comet, speed, tail, comets = PROFILES.get(state, PROFILES["idle"])
        if state in ("command_listening", "listening", "wake_detected"):
            base += 0.5 * ui_bus.level
        elif state == "speaking":
            base += 0.12 * abs(math.sin(self._t * 6))
        elif state == "error":
            base += 0.1 * math.sin(self._t * 2)
        if not motion.enabled():
            speed, comet = 0.0, comet * 0.5
        self._comets = comets
        target = (base + 0.5 * self._flash, comet, speed, tail)
        self._cur = [c + (t - c) * 0.08 for c, t in zip(self._cur, target)]
        self._flash *= 0.93
        tc = self._target_color()
        k = 0.06
        self._color = QColor(int(self._color.red() + (tc.red() - self._color.red()) * k),
                             int(self._color.green() + (tc.green() - self._color.green()) * k),
                             int(self._color.blue() + (tc.blue() - self._color.blue()) * k))
        # One lap = the perimeter of the first screen; strips on other screens reuse the same phase.
        self._head = (self._head + self._cur[2] * dt) % 1.0
        busy = state not in ("wake_listening", "idle", "offline")
        interval = 33 if busy or self._flash > 0.05 else 50
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)
        for s in self._strips:
            s.update()

    def paint_strip(self, strip: _Strip, g: QPainter):
        geo = strip.screen_geo
        W, H, T = geo.width(), geo.height(), THICK
        P = 2 * W + 2 * H
        head = self._head * P
        base, comet, _speed, tail = self._cur
        w, h = strip.width(), strip.height()
        horizontal = strip.edge in ("top", "bottom")
        along = QLinearGradient(0, 0, w, 0) if horizontal else QLinearGradient(0, 0, 0, h)
        core = QLinearGradient(along)
        c1 = self._color
        c2 = QColor(c1).lighter(140)
        n = 36
        for i in range(n + 1):
            f = i / n
            if strip.edge == "top":
                s = f * W
            elif strip.edge == "right":
                s = W + T + f * (H - 2 * T)
            elif strip.edge == "bottom":
                s = W + H + (W - f * W)
            else:
                s = 2 * W + H + (H - T - f * (H - 2 * T))
            a = halo_brightness(s, P, head, comet, tail, self._comets, base)
            mix = 0.5 + 0.5 * math.sin(2 * math.pi * s / P + self._t * 0.25)
            hot = max(0.0, a - 0.7) / 0.3 * 0.45                      # white-hot near the comet head
            col = QColor(int(c1.red() + (c2.red() - c1.red()) * mix), int(c1.green() + (c2.green() - c1.green()) * mix),
                         int(c1.blue() + (c2.blue() - c1.blue()) * mix))
            col = QColor(int(col.red() + (255 - col.red()) * hot), int(col.green() + (255 - col.green()) * hot),
                         int(col.blue() + (255 - col.blue()) * hot))
            along.setColorAt(f, with_alpha(col, 255 * a))
            core.setColorAt(f, with_alpha(col.lighter(120), min(255, 90 + 255 * a)) if a > 0.02 else with_alpha(col, 0))
        rect = strip.rect()
        g.fillRect(rect, along)
        # fade from the screen edge inwards
        g.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        if strip.edge == "top":
            fade = QLinearGradient(0, 0, 0, h)
        elif strip.edge == "bottom":
            fade = QLinearGradient(0, h, 0, 0)
        elif strip.edge == "left":
            fade = QLinearGradient(0, 0, w, 0)
        else:
            fade = QLinearGradient(w, 0, 0, 0)
        for stop, alpha in ((0.0, 255), (0.1, 225), (0.35, 120), (0.7, 36), (1.0, 0)):
            fade.setColorAt(stop, QColor(0, 0, 0, alpha))
        g.fillRect(rect, fade)
        # crisp line right at the edge
        g.setCompositionMode(QPainter.CompositionMode_SourceOver)
        line = {"top": QRect(0, 0, w, 3), "bottom": QRect(0, h - 3, w, 3),
                "left": QRect(0, 0, 3, h), "right": QRect(w - 3, 0, 3, h)}[strip.edge]
        g.fillRect(line, core)

    # ------------------------------------------------------------------ #
    def _on_event(self, ev):
        t = ev.type
        if t in (EventType.VOICE_HOTWORD, EventType.VOICE_WAKE_WORD, EventType.TOOL_COMPLETED,
                 EventType.AUTOMATION_TRIGGERED):
            self._flash = 1.0 if t != EventType.TOOL_COMPLETED else 0.5
        elif t == EventType.ASSISTANT_STATE and self.tab.isVisible():
            self.tab.render(ev.payload or {})

    def _zone_screen(self, pos: QPoint):
        for sc in self._screens():
            geo = sc.geometry()
            if (geo.top() <= pos.y() <= geo.top() + 1
                    and abs(pos.x() - geo.center().x()) <= geo.width() * 0.12):
                return sc
        return None

    def _poll_cursor(self):
        pos = QCursor.pos()
        if self.tab.isVisible():
            near = self.tab.geometry().adjusted(-30, -10, 30, 30).contains(pos)
            self._away = 0 if near or self._zone_screen(pos) else self._away + 80
            if self._away >= 600:
                self._hide_tab()
            return
        sc = self._zone_screen(pos)
        self._dwell = self._dwell + 80 if sc else 0
        if sc is not None and self._dwell >= 400:
            self._dwell = 0
            self._show_tab(sc)

    def _show_tab(self, screen):
        geo = screen.geometry()
        x = geo.center().x() - self.tab.width() // 2
        self.tab.render(ui_bus.state)
        self.tab.move(x, geo.top() - self.tab.height())
        self.tab.show()
        self.tab.raise_()
        motion.animate(self.tab, b"pos", QPoint(x, geo.top() - self.tab.height()), QPoint(x, geo.top() + 8),
                       motion.BASE, QEasingCurve.OutBack)
        self._away = 0

    def _hide_tab(self):
        if self.tab.isVisible():
            self.tab.hide()

    def _tab_clicked(self):
        self._hide_tab()
        self.open_requested.emit()

    def shutdown(self):
        self.set_visible(False)
        self.tab.close()


if __name__ == "__main__":        # quick self-check of the light maths
    P = 1000.0
    at_head = halo_brightness(500, P, 500, 1.0, 0.1, 1, 0.0)
    behind = halo_brightness(450, P, 500, 1.0, 0.1, 1, 0.0)
    ahead = halo_brightness(550, P, 500, 1.0, 0.1, 1, 0.0)
    far = halo_brightness(0, P, 500, 1.0, 0.1, 1, 0.0)
    assert at_head == 1.0 and 0.5 < behind < at_head and ahead < behind and far < 0.01, (at_head, behind, ahead, far)
    assert halo_brightness(0, P, 500, 1.0, 0.1, 2, 0.0) == 1.0          # the second comet sits opposite
    print("halo ok")

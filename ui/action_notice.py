"""
ui/action_notice.py

A small pill at the bottom of the screen whenever SAINT *does* something —
"Skip to the next track ✓", "Search a website… ", "Couldn't find that
window ✗" — so actions are visible even when SAINT is in the tray. It never
takes focus and clicks go straight through it. Turn it off in Settings ›
Appearance (notifications.actions) or from the overlay.
"""

import re
import time

from PySide6.QtCore import QEasingCurve, QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from core.config import config
from core.events import EventType
from ui import icons, motion
from ui.reactive import ui_bus
from ui.theme import current_palette
from ui.widgets import ElidedLabel, with_alpha

# Lookups aren't "actions" — they'd only be noise.
_QUIET = re.compile(r"^(?:screen\.|memory\.(?:recall|list)|desktop\.list_|spotify\.(?:current|queue|devices|search|"
                    r"recent|top)|automation\.list|system\.|web\.)")
_ICONS = (("spotify.", "music"), ("browser.youtube", "video"), ("browser.", "globe"), ("desktop.web_search", "search"), ("desktop.open_url", "globe"),
          ("desktop.", "cursor"), ("memory.", "memory"), ("automation.", "clock"))
_ASKING = re.compile(r"which one should i use|don't have a browser open|confirmation required", re.I)

BOTTOM_GAP = 28


def enabled() -> bool:
    return bool(config.get("notifications.actions", True))


_YT_WORDS = {"play_pause": "Play / pause", "fullscreen": "Full screen", "exit_fullscreen": "Exit full screen",
             "theater": "Theater mode", "miniplayer": "Miniplayer", "captions": "Captions", "captions_on": "Captions on",
             "captions_off": "Captions off", "rewind": "Back 10 seconds", "forward": "Forward 10 seconds",
             "speed_up": "Faster", "speed_down": "Slower", "next_video": "Next video", "previous_video": "Previous video",
             "restart": "Restart", "skip_ad": "Skip the ad", "autoplay_on": "Autoplay on", "autoplay_off": "Autoplay off",
             "loop_on": "Loop on", "loop_off": "Loop off"}


def label_for(tool: str, args: dict = None) -> str:
    """What SAINT is doing, in words: "Search YouTube for lofi", "YouTube · 1.5x speed"."""
    a = args or {}
    if tool == "browser.youtube":
        act, val = str(a.get("action", "")), a.get("value")
        if act == "speed" and val is not None:
            word = f"{float(val):g}x speed"
        elif act == "quality" and val:
            word = f"Quality {val}"
        elif act == "seek_seconds" and val is not None:
            v = float(val)
            word = f"{'Forward' if v > 0 else 'Back'} {abs(v):g} seconds"
        elif act == "seek_percent" and val is not None:
            word = f"Jump to {float(val):g}%"
        else:
            word = _YT_WORDS.get(act) or act.replace("_", " ").capitalize()
        return f"YouTube · {word}"
    if tool == "desktop.web_search" and a.get("query"):
        return f"Search {str(a.get('site') or 'the web').title()} for {a['query']}"
    if tool == "desktop.open_url" and a.get("url"):
        site = re.sub(r"^https?://(www\.)?", "", str(a["url"])).split("/")[0]
        return f"Open {site}"
    if tool == "desktop.open_app" and a.get("name"):
        return f"Open {a['name']}"
    if tool in ("desktop.focus_window",) and a.get("name"):
        return f"Switch to {a['name']}"
    if tool == "desktop.type_text":
        return "Type text"
    if tool == "desktop.press_keys" and a.get("keys"):
        return f"Press {a['keys']}"
    if tool == "desktop.click_element" and a.get("name"):
        return f"Click {a['name']}"
    if tool == "spotify.play" and (a.get("query") or a.get("name")):
        return f"Play {a.get('query') or a.get('name')}"
    if tool == "memory.remember":
        return "Remember that"
    from ui.pages.home import tool_label
    return tool_label(tool)


class ActionNotice(QWidget):
    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setWindowTitle("SAINT action")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 18, 10)
        lay.setSpacing(12)
        self.badge = QLabel()
        self.badge.setFixedSize(28, 28)
        self.badge.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.badge)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.title = ElidedLabel("")
        self.title.setStyleSheet("font-weight: 600; background: transparent;")
        self.sub = ElidedLabel("")
        self.sub.setObjectName("Faint")
        self.sub.setStyleSheet("background: transparent;")
        col.addWidget(self.title)
        col.addWidget(self.sub)
        lay.addLayout(col, 1)
        self._state = "running"        # running | ok | error | ask
        self._tool = ""
        self._label = ""
        self._count = 0
        self._spin = 0.0
        self._done_at = 0.0
        self._closing = False
        self._spinner = QTimer(self)
        self._spinner.setInterval(33)
        self._spinner.timeout.connect(self._rotate)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.leave)
        ui_bus.event.connect(self._on_event)

    # ------------------------------------------------------------------ #
    def _on_event(self, ev):
        t, p = ev.type, ev.payload or {}
        if t == EventType.TOOL_STARTED:
            if enabled() and not _QUIET.match(p.get("tool", "")):
                self.started(p.get("tool", ""), p.get("args") or {})
        elif t in (EventType.TOOL_COMPLETED, EventType.TOOL_FAILED):
            if self.isVisible() and p.get("tool") == self._tool and self._state == "running":
                self.finished(t == EventType.TOOL_COMPLETED, p.get("error", ""))
        elif t == EventType.UI_CHAT_RENDER and p.get("role") == "assistant" and self.isVisible() \
                and self._state in ("ok", "error", "ask") and time.monotonic() - self._done_at < 6:
            reply = (p.get("text") or "").strip()
            if reply and len(reply) <= 110 and not self._closing:
                self.sub.setText(reply)
                self._fit()
                self._hide_timer.start(2800)

    def started(self, tool: str, args: dict = None):
        self._tool = tool
        self._label = label_for(tool, args)
        self._count = self._count + 1 if self.isVisible() and not self._closing else 1
        self._state = "running"
        self.title.setText(self._label + "…")
        self.sub.setText(f"Step {self._count}" if self._count > 1 else "Working on it")
        self._icon(next((ic for pre, ic in _ICONS if tool.startswith(pre)), "zap"))
        self._hide_timer.stop()
        self._spinner.start()
        self._fit()
        self.appear()

    def finished(self, ok: bool, error: str = ""):
        self._spinner.stop()
        self._done_at = time.monotonic()
        asking = not ok and bool(_ASKING.search(error or ""))
        self._state = "ok" if ok else ("ask" if asking else "error")
        self.title.setText(self._label)
        if ok:
            self.sub.setText("Done")
        elif asking:
            self.sub.setText("Waiting for your answer")
        else:
            self.sub.setText(_short(error) or "That didn't work")
        self._fit()
        self.update()
        self._hide_timer.start(2600 if ok else 4200)

    def preview(self):
        """Show a sample so a Settings / overlay switch visibly does something."""
        self.started("spotify.next")
        QTimer.singleShot(900, lambda: self.finished(True))
        QTimer.singleShot(1000, lambda: (self.sub.setText("Action notifications are on"), self._fit()))

    # ------------------------------------------------------------------ #
    def _icon(self, name: str):
        self.badge.setPixmap(icons.pixmap(name, current_palette().text, 16))

    def _rotate(self):
        self._spin = (self._spin + 12) % 360
        self.update()

    def _fit(self):
        want = max(self.title.fontMetrics().horizontalAdvance(self.title.text()) * 1.08,
                   self.sub.fontMetrics().horizontalAdvance(self.sub.text()))
        self.setFixedSize(int(max(260, min(560, want + 16 + 28 + 12 + 22))), 58)
        if self.isVisible() and not self._closing:
            self.move(self._home())

    def _home(self) -> QPoint:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        return QPoint(geo.center().x() - self.width() // 2, geo.bottom() - self.height() - BOTTOM_GAP)

    def appear(self):
        target = self._home()
        self._closing = False
        if self.isVisible():
            self.move(target)
            self.setWindowOpacity(1.0)
            self.update()
            return
        self.move(target + QPoint(0, 18))
        self.setWindowOpacity(0.0 if motion.enabled() else 1.0)
        self.show()
        self.raise_()
        motion.animate(self, b"pos", target + QPoint(0, 18), target, motion.SLOW, QEasingCurve.OutBack)
        motion.animate(self, b"windowOpacity", 0.0, 1.0, motion.BASE)

    def leave(self):
        if not self.isVisible() or self._closing:
            return
        self._closing = True

        def done():
            self.hide()
            self._closing = False
            self._count = 0
        motion.animate(self, b"windowOpacity", self.windowOpacity(), 0.0, motion.BASE, on_done=done)

    def paintEvent(self, _):
        p = current_palette()
        g = QPainter(self)
        g.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        g.fillPath(path, with_alpha(p.raised, 246))
        color = {"ok": p.success, "error": p.danger, "ask": p.warning}.get(self._state, p.accent)
        g.setPen(QPen(with_alpha(color, 120), 1))
        g.drawPath(path)
        # status ring around the icon
        b = self.badge.geometry()
        ring = QRectF(b).adjusted(-3, -3, 3, 3)
        g.setPen(Qt.NoPen)
        g.setBrush(with_alpha(color, 40))
        g.drawEllipse(ring)
        pen = QPen(QColor(color), 2.2)
        pen.setCapStyle(Qt.RoundCap)
        g.setPen(pen)
        g.setBrush(Qt.NoBrush)
        if self._state == "running":
            g.drawArc(ring.adjusted(1, 1, -1, -1), int(-self._spin * 16), 100 * 16)
        else:
            g.drawEllipse(ring.adjusted(1, 1, -1, -1))
            mark = {"ok": "check", "error": "x", "ask": "alert"}[self._state]
            s = 12
            g.drawPixmap(int(ring.right() - s + 3), int(ring.bottom() - s + 3), icons.pixmap(mark, color, s))
        g.end()


def _short(error: str) -> str:
    e = re.sub(r"\s+", " ", error or "").strip()
    return e if len(e) <= 90 else e[:87].rstrip() + "…"

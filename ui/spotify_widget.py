"""
ui/spotify_widget.py

The floating mini player: always on top, drag it anywhere (the position is
remembered). Album art, live progress and controls. While it's on screen,
music hot-words ("skip", "pause", "louder") work without the wake word, and
it flashes when it hears one.
"""

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from core.config import config
from core.events import EventType
from ui import actions, motion
from ui.pages.music import NowPlaying
from ui.reactive import ui_bus
from ui.theme import current_palette
from ui.widgets import IconButton, covers, with_alpha

SHADOW = 14


class SpotifyWidget(QWidget):
    def __init__(self, shell):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.shell = shell
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("SAINT mini player")
        self.setFixedSize(400 + 2 * SHADOW, 118 + 2 * SHADOW)
        self._drag = None
        self._flash = 0.0
        self._tint = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SHADOW + 14, SHADOW + 12, SHADOW + 10, SHADOW + 12)
        lay.setSpacing(6)
        self.player = NowPlaying(cover=76, show_hint=True, hint="Say “skip”")
        lay.addWidget(self.player, 1)
        side = QVBoxLayout()
        side.setSpacing(2)
        self.close_btn = IconButton("x", "Hide the mini player", 14)
        self.close_btn.clicked.connect(lambda: shell.set_widget(False))
        self.open_btn = IconButton("overlay", "Open the SAINT overlay", 14)
        self.open_btn.clicked.connect(shell.toggle_overlay)
        side.addWidget(self.close_btn)
        side.addWidget(self.open_btn)
        side.addStretch()
        lay.addLayout(side)
        self._fade = QTimer(self)
        self._fade.setInterval(33)
        self._fade.timeout.connect(self._decay)
        ui_bus.event.connect(self._on_event)

    # ------------------------------------------------------------------ #
    def place(self):
        pos = config.get("widgets.spotify_pos")
        screen = QGuiApplication.primaryScreen().availableGeometry()
        if pos and any(s.availableGeometry().contains(QPoint(*pos)) for s in QGuiApplication.screens()):
            self.move(*pos)
        else:
            self.move(screen.right() - self.width() - 12, screen.bottom() - self.height() - 12)

    def showEvent(self, e):
        super().showEvent(e)
        self._update_tint()

    def _on_event(self, ev):
        if ev.type == EventType.VOICE_HOTWORD:
            self._flash = 1.0
            self._fade.start()
        elif ev.type == EventType.SPOTIFY_PLAYBACK_CHANGED:
            self._update_tint()

    def _update_tint(self):
        url = ui_bus.spotify.get("image_large") or ui_bus.spotify.get("image") or ""
        c = covers.color(url)
        if c is None and url:
            covers.get(url, lambda _pm: self._update_tint())
        self._tint = c
        self.update()

    def _decay(self):
        self._flash *= 0.9
        if self._flash < 0.02:
            self._flash = 0.0
            self._fade.stop()
        self.update()

    # ------------------------------------------------------------------ #
    def paintEvent(self, _):
        p = current_palette()
        g = QPainter(self)
        g.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(SHADOW, SHADOW, -SHADOW, -SHADOW)
        for i in range(SHADOW, 0, -2):                                    # soft drop shadow
            g.setPen(Qt.NoPen)
            g.setBrush(QColor(0, 0, 0, int(5 * (SHADOW - i) / SHADOW * 3)))
            g.drawRoundedRect(r.adjusted(-i, -i + 4, i, i + 4), 18 + i, 18 + i)
        path = QPainterPath()
        path.addRoundedRect(r, 18, 18)
        g.fillPath(path, with_alpha(p.surface, 246))
        tint = self._tint or QColor(p.accent)
        grad = QLinearGradient(r.topLeft(), r.topRight())
        grad.setColorAt(0, with_alpha(tint, 70))
        grad.setColorAt(0.55, with_alpha(tint, 0))
        g.fillPath(path, grad)
        edge = QColor(p.accent) if self._flash else with_alpha("#ffffff" if p.dark else "#000000", 26)
        if self._flash:
            edge.setAlpha(int(90 + 165 * self._flash))
        g.setPen(edge)
        g.setBrush(Qt.NoBrush)
        g.drawPath(path)
        g.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            self._drag = None
            config.set("widgets.spotify_pos", [self.x(), self.y()])

    def mouseDoubleClickEvent(self, e):
        self.shell.show_normal()
        self.shell.navigate("Music")

    def appear(self):
        self.place()
        self.show()
        motion.fade_in(self.player, motion.SLOW)
        actions.refresh_spotify()

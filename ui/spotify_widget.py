"""
ui/spotify_widget.py

The floating mini player: always on top, drag it anywhere (the position is
remembered). Shows whatever is playing on the PC — Spotify, a YouTube video,
VLC, any app Windows knows about — with its art, live progress and controls.
While music plays, hot-words ("skip", "pause", "louder") work without the
wake word, and it flashes when it hears one.

Lyrics (the button, or "turn on the lyrics"): a panel under the player with
the line being sung, synced to the song (modules/spotify/lyrics.py).
"""

import time

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from core.config import config
from core.events import EventType
from ui import actions, motion
from ui.pages.music import NowPlaying
from ui.reactive import ui_bus
from ui.theme import current_palette
from ui.widgets import ElidedLabel, IconButton, covers, with_alpha

SHADOW = 14
PLAYER_H = 118
LYRICS_H = 92


class LyricsPanel(QWidget):
    """Previous / current / next line of what's playing, following the song."""

    _loaded = Signal(str, object)          # key, Lyrics | None (from a worker thread)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._key, self._lyrics, self._state, self._index = "", None, "idle", None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 6, 4, 0)
        lay.setSpacing(3)
        self.prev = ElidedLabel("")
        self.prev.setObjectName("Muted")
        self.cur = QLabel("")
        self.cur.setWordWrap(True)
        self.cur.setObjectName("SectionTitle")
        self.cur.setMaximumHeight(44)
        self.next = ElidedLabel("")
        self.next.setObjectName("Muted")
        for w in (self.prev, self.cur, self.next):
            w.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            lay.addWidget(w)
        lay.addStretch()
        self._loaded.connect(self._on_loaded)
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)

    def start(self):
        self._tick()
        self._timer.start()

    def stop(self):
        self._timer.stop()

    # ------------------------------------------------------------------ #
    def _tick(self):
        np = ui_bus.now_playing()
        title, artist = np.get("title", ""), np.get("artist", "")
        if not title:
            return self._show("", "Nothing is playing.", "")
        if np.get("source") != "spotify" and not np.get("is_spotify"):
            return self._show("", "Lyrics follow songs playing in Spotify.", "")
        from modules.spotify.lyrics import lyrics_service
        key = lyrics_service.key(title, artist)
        if key != self._key:
            self._key, self._lyrics, self._state, self._index = key, None, "loading", None
            self._show("", "Finding the lyrics…", "")
            lyrics_service.get_async(title, artist, np.get("album", ""), np.get("duration_ms") or 0,
                                     lambda ly, k=key: self._loaded.emit(k, ly))
            return
        if self._state != "ready" or self._lyrics is None:
            return
        ly = self._lyrics
        if ly.instrumental and not ly.lines():
            return self._show("", "♪ Instrumental ♪", "")
        pos = (np.get("position_ms") or 0) + ((time.time() - np.get("at", time.time())) * 1000
                                              if np.get("is_playing") else 0)
        i = ly.index_at(int(pos) + 250, np.get("duration_ms") or 0)     # a beat early reads better
        if i == self._index:
            return
        self._index = i
        lines = ly.lines()
        prev = lines[i - 1] if i >= 1 else ""
        cur = lines[i] if 0 <= i < len(lines) else ("♪" if i < 0 else "")
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        self._show(prev, cur or "♪", nxt)

    def _on_loaded(self, key: str, ly):
        if key != self._key:
            return                              # the song changed while we looked
        self._lyrics, self._state = ly, "ready" if ly else "missing"
        if ly is None:
            self._show("", "No lyrics found for this song.", "")
        self._index = None
        self._tick()

    def _show(self, prev: str, cur: str, nxt: str):
        if (self.prev._full, self.cur.text(), self.next._full) == (prev, cur, nxt):
            return
        self.prev.setText(prev)
        self.cur.setText(cur)
        self.next.setText(nxt)


class SpotifyWidget(QWidget):
    def __init__(self, shell):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.shell = shell
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("SAINT mini player")
        self._drag = None
        self._flash = 0.0
        self._tint = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW + 14, SHADOW + 12, SHADOW + 10, SHADOW + 12)
        outer.setSpacing(0)
        lay = QHBoxLayout()
        lay.setSpacing(6)
        outer.addLayout(lay)
        self.player = NowPlaying(cover=76, show_hint=True, hint="Say “skip”", any_media=True)
        lay.addWidget(self.player, 1)
        side = QVBoxLayout()
        side.setSpacing(2)
        self.close_btn = IconButton("x", "Hide the mini player", 14)
        self.close_btn.clicked.connect(lambda: shell.set_widget(False))
        self.open_btn = IconButton("overlay", "Open the SAINT overlay", 14)
        self.open_btn.clicked.connect(shell.toggle_overlay)
        self.lyrics_btn = IconButton("lyrics", "Show lyrics", 14, checkable=True)
        self.lyrics_btn.toggled.connect(lambda on: self.set_lyrics(on, save=True))
        side.addWidget(self.close_btn)
        side.addWidget(self.open_btn)
        side.addWidget(self.lyrics_btn)
        side.addStretch()
        lay.addLayout(side)
        self.lyrics = LyricsPanel(self)
        outer.addWidget(self.lyrics)
        self._fade = QTimer(self)
        self._fade.setInterval(33)
        self._fade.timeout.connect(self._decay)
        ui_bus.event.connect(self._on_event)
        self.set_lyrics(bool(config.get("widgets.lyrics", False)))

    # ------------------------------------------------------------------ #
    def set_lyrics(self, on: bool, save: bool = False):
        """Show or hide the lyrics panel (the window grows downwards)."""
        on = bool(on)
        if save or bool(config.get("widgets.lyrics", False)) != on:
            config.set("widgets.lyrics", on)
        if self.lyrics_btn.isChecked() != on:
            self.lyrics_btn.blockSignals(True)
            self.lyrics_btn.setChecked(on)
            self.lyrics_btn.blockSignals(False)
        self.lyrics_btn.setToolTip("Hide lyrics" if on else "Show lyrics")
        self.lyrics.setVisible(on)
        self.setFixedSize(400 + 2 * SHADOW, PLAYER_H + (LYRICS_H if on else 0) + 2 * SHADOW)
        if self.isVisible():
            # Grown past the bottom of the screen (it lives in the corner): move up to fit.
            screen = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
            area = screen.availableGeometry()
            if self.geometry().bottom() > area.bottom():
                self.move(self.x(), max(area.top(), area.bottom() - self.height()))
        if on and self.isVisible():
            self.lyrics.start()
        else:
            self.lyrics.stop()
        self.update()

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
        if self.lyrics.isVisible():
            self.lyrics.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.lyrics.stop()

    def _on_event(self, ev):
        if ev.type == EventType.VOICE_HOTWORD:
            self._flash = 1.0
            self._fade.start()
        elif ev.type in (EventType.SPOTIFY_PLAYBACK_CHANGED, EventType.MEDIA_CHANGED):
            self._update_tint()

    def _update_tint(self):
        url = ui_bus.now_playing().get("cover", "")
        c = covers.color(url)
        if c is None and url:
            covers.get(url, lambda pm: pm is not None and self._update_tint())
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
        if self.lyrics.isVisible():                                       # hairline above the lyrics
            y = self.lyrics.geometry().top() + 1
            g.setPen(with_alpha("#ffffff" if p.dark else "#000000", 22))
            g.drawLine(int(r.left() + 16), int(y), int(r.right() - 16), int(y))
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
        from modules.desktop.media import media
        media.start()

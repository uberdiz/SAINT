"""
ui/spotify_widget.py

The floating mini player: drag it anywhere (the position is remembered
separately for normal use and for Gaming Mode), drag its right edge to make it
wider, right-click for pin on top / opacity / size / which monitor / hide, or
ctrl+scroll to fade it. A status line shows what SAINT is doing (listening,
thinking, speaking, the task it's on) and whether Gaming Mode is on. Shows whatever is playing on the PC — Spotify, a YouTube video,
VLC, any app Windows knows about — with its art, live progress and controls.
While music plays, hot-words ("skip", "pause", "louder") work without the
wake word, and it flashes when it hears one.

Lyrics (the button, or "turn on the lyrics"): a panel under the player with
the line being sung, synced to the song (modules/spotify/lyrics.py).
"""

import time

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QGuiApplication, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QVBoxLayout, QWidget

from core.config import config
from core.events import EventType
from ui import actions, motion
from ui.pages.music import NowPlaying
from ui.reactive import ui_bus
from ui.theme import current_palette, state_color
from ui.widgets import ElidedLabel, IconButton, covers, with_alpha

SHADOW = 14
PLAYER_H = 118
STATUS_H = 20
LYRICS_H = 92
WIDTHS = {"compact": 340, "normal": 400, "wide": 520}
MIN_W, MAX_W = 320, 640
EDGE = 8                                  # px at the right edge that resize instead of move
OPACITIES = (1.0, 0.9, 0.75, 0.6)


class StatusLine(QWidget):
    """● Listening · Gaming Mode · opening Spotify (2 of 3)"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(STATUS_H)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(6)
        self.dot = QLabel("\u25cf")
        self.text = ElidedLabel("")
        self.text.setObjectName("Faint")
        lay.addWidget(self.dot)
        lay.addWidget(self.text, 1)

    def refresh(self):
        from core.activity import activity
        from core.game_mode import game_mode
        s = ui_bus.state or {}
        self.dot.setStyleSheet(f"color: {state_color(s.get('state', 'offline'), current_palette())};"
                               "font-size: 10px;")
        parts = [s.get("label") or "Ready"]
        if game_mode.active:
            parts.append("Gaming Mode")
        snap = activity.snapshot()
        if snap["label"]:
            steps, i = snap["steps"], snap["index"]
            parts.append(f"{steps[i]} ({i + 1} of {len(steps)})" if steps and 0 <= i < len(steps) else snap["label"])
        else:
            from modules.agent.task_memory import task_memory
            t = task_memory.current()
            if t and t.get("status") in ("failed", "stopped", "waiting"):
                parts.append(f"paused: {t.get('title', '')}")
        self.text.setText(" \u00b7 ".join(p for p in parts if p))


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
        self._resize = None
        self._flash = 0.0
        self._tint = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW + 14, SHADOW + 12, SHADOW + 10, SHADOW + 12)
        outer.setSpacing(0)
        self.status = StatusLine(self)
        outer.addWidget(self.status)
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
        self.setMouseTracking(True)
        self._apply_pin(bool(config.get("widgets.pinned", True)))
        self.setWindowOpacity(float(config.get("widgets.opacity", 1.0) or 1.0))
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
        self._apply_size()
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

    def _width(self) -> int:
        w = config.get("widgets.width", WIDTHS["normal"])
        try:
            return max(MIN_W, min(MAX_W, int(w)))
        except (TypeError, ValueError):
            return WIDTHS["normal"]

    def _apply_size(self, width: int = 0):
        h = STATUS_H + PLAYER_H + (LYRICS_H if self.lyrics.isVisible() else 0) + 2 * SHADOW
        self.setFixedSize((width or self._width()) + 2 * SHADOW, h)

    def set_width(self, width: int):
        width = max(MIN_W, min(MAX_W, int(width)))
        config.set("widgets.width", width)
        self._apply_size(width)

    def _apply_pin(self, on: bool):
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        if visible:
            self.show()

    def set_pinned(self, on: bool):
        config.set("widgets.pinned", bool(on))
        self._apply_pin(bool(on))

    def set_opacity(self, value: float):
        value = max(0.35, min(1.0, float(value)))
        config.set("widgets.opacity", round(value, 2))
        self.setWindowOpacity(value)

    @staticmethod
    def _pos_key() -> str:
        from core.game_mode import game_mode
        return "widgets.spotify_pos_game" if game_mode.active else "widgets.spotify_pos"

    def remember_position(self):
        config.set(self._pos_key(), [self.x(), self.y()])

    def place(self):
        """Its remembered spot for the current mode (normal / Gaming Mode), else a corner."""
        from core.game_mode import game_mode
        pos = config.get(self._pos_key())
        if pos and any(s.availableGeometry().contains(QPoint(*pos)) for s in QGuiApplication.screens()):
            self.move(*pos)
            return
        screen = None
        if game_mode.active:
            from ui import placement
            screen = placement.gaming_screen(game_mode.game_rect())
        area = (screen or QGuiApplication.primaryScreen()).availableGeometry()
        self.move(area.right() - self.width() - 12, area.bottom() - self.height() - 12)

    def showEvent(self, e):
        super().showEvent(e)
        self._update_tint()
        self.status.refresh()
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
        elif ev.type in (EventType.ASSISTANT_STATE, EventType.ACTIVITY_CHANGED, EventType.GAME_MODE,
                         EventType.TASK_STATE) and self.isVisible():
            self.status.refresh()

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

    def _on_edge(self, pos) -> bool:
        return self.width() - SHADOW - EDGE <= pos.x() <= self.width() - SHADOW + 2

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            if self._on_edge(e.position().toPoint()):
                self._resize = (e.globalPosition().toPoint().x(), self.width() - 2 * SHADOW)
            else:
                self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._resize is not None and e.buttons() & Qt.LeftButton:
            x0, w0 = self._resize
            self._apply_size(max(MIN_W, min(MAX_W, w0 + e.globalPosition().toPoint().x() - x0)))
        elif self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
        else:
            self.setCursor(Qt.SizeHorCursor if self._on_edge(e.position().toPoint()) else Qt.ArrowCursor)

    def mouseReleaseEvent(self, e):
        if self._resize is not None:
            self._resize = None
            config.set("widgets.width", self.width() - 2 * SHADOW)
        if self._drag is not None:
            self._drag = None
            self.remember_position()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:                 # ctrl+scroll fades it
            step = 0.05 if e.angleDelta().y() > 0 else -0.05
            self.set_opacity(self.windowOpacity() + step)
            e.accept()
        else:
            super().wheelEvent(e)

    def contextMenuEvent(self, e):
        from ui import placement
        menu = QMenu(self)
        pin = QAction("Keep on top", menu, checkable=True)
        pin.setChecked(bool(config.get("widgets.pinned", True)))
        pin.triggered.connect(self.set_pinned)
        menu.addAction(pin)
        op = menu.addMenu("Opacity")
        for v in OPACITIES:
            a = QAction(f"{int(v * 100)}%", op, checkable=True)
            a.setChecked(abs(self.windowOpacity() - v) < 0.03)
            a.triggered.connect(lambda _=False, v=v: self.set_opacity(v))
            op.addAction(a)
        size = menu.addMenu("Size")
        for name, w in WIDTHS.items():
            a = QAction(name.capitalize(), size, checkable=True)
            a.setChecked(self._width() == w)
            a.triggered.connect(lambda _=False, w=w: self.set_width(w))
            size.addAction(a)
        screens = placement.screens()
        if len(screens) > 1:
            mon = menu.addMenu("Move to monitor")
            for i, s in enumerate(screens, 1):
                a = QAction(f"Monitor {i}" + (" (main)" if s is QGuiApplication.primaryScreen() else ""), mon,
                            checkable=True)
                a.setChecked(self.screen() is s)
                a.triggered.connect(lambda _=False, s=s: (placement.move_to_screen(self, s, "bottom-right"),
                                                          self.remember_position()))
                mon.addAction(a)
        menu.addSeparator()
        menu.addAction("Open SAINT", self.shell.show_normal)
        menu.addAction("Hide mini player", lambda: self.shell.set_widget(False))
        menu.exec(e.globalPos())

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

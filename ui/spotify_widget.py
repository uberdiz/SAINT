"""
ui/spotify_widget.py

The floating mini player: drag it anywhere (the position is remembered
separately for normal use and for Gaming Mode), drag any edge or corner to
resize it freely — make it small or square and it becomes just the album art,
with the title, progress, controls and volume showing when you hover. Scroll
over it for volume, ctrl+scroll to fade it, right-click for pin on top /
opacity / size presets / lyrics / which monitor / hide. A status line shows
what SAINT is doing (listening, thinking, speaking, the task it's on) and
whether Gaming Mode is on. Shows whatever is playing on the PC — Spotify, a
YouTube video, VLC, any app Windows knows about — with its art, live progress
and controls. While music plays, hot-words ("skip", "pause", "louder") work
without the wake word, and it flashes when it hears one.

Lyrics (the button, or "turn on the lyrics"): a panel under the player with
the line being sung, synced to the song (modules/spotify/lyrics.py).
"""

import time

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QGuiApplication, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QSlider, QVBoxLayout, QWidget

from core.config import config
from core.events import EventType
from ui import actions, icons, motion
from ui.pages.music import NowPlaying
from ui.reactive import ui_bus
from ui.theme import current_palette, state_color
from ui.widgets import CoverArt, ElidedLabel, IconButton, ProgressLine, covers, with_alpha

SHADOW = 14
STATUS_H = 20
LYRICS_H = 92
# The card is the visible player (no shadow, no lyrics panel). Free resizing between these:
DEFAULT_W, DEFAULT_H = 340, 138
MIN_W, MIN_H = 96, 96
MAX_W, MAX_H = 900, 640
ART_W, FULL_MIN_H = 250, 132           # smaller than this -> album-art mode
ART_RADIUS = 16
SIZES = {"Album art": (150, 150), "Compact": (340, 138), "Normal": (400, 138), "Wide": (520, 150),
         "Large": (520, 240)}
EDGE = 7                               # px inside the card's border that resize instead of move
OPACITIES = (1.0, 0.9, 0.75, 0.6)


class StatusLine(QWidget):
    """● Listening · Gaming Mode · opening Spotify (2 of 3)"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(STATUS_H)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(6)
        self.dot = QLabel("●")
        self.text = ElidedLabel("")
        self.text.setObjectName("Faint")
        lay.addWidget(self.dot)
        lay.addWidget(self.text, 1)
        self._note, self._note_until = "", 0.0
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.timeout.connect(self.refresh)

    def note(self, text: str, ms: int = 1500):
        """Show a short note (a heard hot-word, the volume) instead of the hint for a moment."""
        self._note, self._note_until = text, time.time() + ms / 1000
        self._note_timer.start(ms + 20)
        self.refresh()

    def heard(self, word: str):
        self.note(f"Heard “{word}” ✓" if word else "Heard you ✓", 1800)

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
        if time.time() < self._note_until:
            parts.append(self._note)
        elif len(parts) == 1 and ui_bus.now_playing().get("is_playing") and actions.hotwords_on():
            parts.append("say “skip” — no wake word")
        self.text.setText(" · ".join(p for p in parts if p))


class LyricsPanel(QWidget):
    """Previous / current / next line of what's playing, following the song."""

    _loaded = Signal(str, object)          # key, Lyrics | None (from a worker thread)
    changed = Signal()                     # a new line is showing

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
        self.changed.emit()


class _LightButton(IconButton):
    """Icon button drawn light: it sits on the album art's dark scrim in any theme."""

    def refresh(self):
        self.setIcon(icons.icon(self._name, "#e6e6e6", self._size, active_color="#ffffff"))


class _Scrim(QWidget):
    """Dark fade over the bottom of the cover so the white text reads on any art."""

    def paintEvent(self, _):
        g = QPainter(self)
        g.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(r, ART_RADIUS, ART_RADIUS)
        grad = QLinearGradient(r.topLeft(), r.bottomLeft())
        grad.setColorAt(0.0, QColor(0, 0, 0, 120))
        grad.setColorAt(0.35, QColor(0, 0, 0, 40))
        grad.setColorAt(0.55, QColor(0, 0, 0, 110))
        grad.setColorAt(1.0, QColor(0, 0, 0, 215))
        g.fillPath(path, grad)
        g.end()


class ArtView(QWidget):
    """The album-art-only mini player: the cover fills the card; hovering (or a
    new track, for a moment) shows the title, progress and controls over it.
    It's a skin over ``NowPlaying``, which stays the one source of truth."""

    def __init__(self, player: NowPlaying, shell, parent=None):
        super().__init__(parent)
        self.player = player
        self.cover = CoverArt(120, radius=ART_RADIUS, parent=self)
        self.scrim = _Scrim(self)
        lay = QVBoxLayout(self.scrim)
        lay.setContentsMargins(12, 8, 10, 10)
        lay.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(0)
        top.addStretch()
        self.open_btn = _LightButton("overlay", "Open the SAINT overlay", 13)
        self.open_btn.clicked.connect(shell.toggle_overlay)
        self.close_btn = _LightButton("x", "Hide the mini player", 13)
        self.close_btn.clicked.connect(lambda: shell.set_widget(False))
        top.addWidget(self.open_btn)
        top.addWidget(self.close_btn)
        lay.addLayout(top)
        lay.addStretch()
        self.title = ElidedLabel("")
        self.title.setStyleSheet("color: #ffffff; font-weight: 600; background: transparent;")
        self.artist = ElidedLabel("")
        self.artist.setStyleSheet("color: rgba(255,255,255,190); font-size: 11px; background: transparent;")
        self.progress = ProgressLine(3)
        self.progress.color = "#ffffff"
        lay.addWidget(self.title)
        lay.addWidget(self.artist)
        lay.addSpacing(3)
        lay.addWidget(self.progress)
        ctl = QHBoxLayout()
        ctl.setSpacing(2)
        self.prev = _LightButton("prev", "Previous", 15)
        self.play = _LightButton("play", "Play / pause", 18)
        self.next = _LightButton("next", "Next", 15)
        self.vol_icon = _LightButton("volume", "Mute", 14)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setMaximumWidth(90)
        self.vol.setToolTip("Volume")
        self.prev.clicked.connect(player.prev.click)
        self.play.clicked.connect(player.play.click)
        self.next.clicked.connect(player.next.click)
        self.vol_icon.clicked.connect(player.vol_icon.click)
        self.vol.sliderReleased.connect(lambda: player._set_volume(self.vol.value()))
        for w in (self.prev, self.play, self.next):
            ctl.addWidget(w)
        ctl.addStretch()
        ctl.addWidget(self.vol_icon)
        ctl.addWidget(self.vol, 1)
        lay.addLayout(ctl)
        self._hovered = False
        self._peek = QTimer(self)                 # a new track: show its name for a moment
        self._peek.setSingleShot(True)
        self._peek.timeout.connect(self._update_scrim)
        self._last_title = None
        self.scrim.hide()

    def set_hover(self, on: bool):
        self._hovered = bool(on)
        self._update_scrim()

    def _update_scrim(self):
        self.scrim.setVisible(self._hovered or self._peek.isActive())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.cover.set_size(self.width(), self.height())
        self.scrim.setGeometry(self.rect())
        roomy = self.width() >= 170
        self.vol.setVisible(roomy)
        self.artist.setVisible(self.height() >= 120)

    def sync(self):
        """Copy what the player shows (called whenever it re-renders)."""
        p = self.player
        self.cover.set_url(p.cover._url or "")
        title = p.title.text()
        self.title.setText(title)
        self.artist.setText(p.artist.text())
        self.progress.set_value(p.progress._v)
        self.play.set_icon(p.play._name)
        self.prev.setEnabled(p.prev.isEnabled())
        self.next.setEnabled(p.next.isEnabled())
        self.vol_icon.set_icon(p.vol_icon._name)
        self.vol_icon.setToolTip(p.vol.toolTip() or "Volume")
        if not self.vol.isSliderDown() and self.vol.value() != p.vol.value():
            self.vol.blockSignals(True)
            self.vol.setValue(p.vol.value())
            self.vol.blockSignals(False)
        if self._last_title is not None and title != self._last_title and title:
            self._peek.start(3500)
            self._update_scrim()
        self._last_title = title


class SpotifyWidget(QWidget):
    def __init__(self, shell):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.shell = shell
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("SAINT mini player")
        self._drag = None
        self._resize = None                     # (edges, press point, window geometry, card size)
        self._flash = 0.0
        self._tint = None
        self._mode = "full"
        self._card = self._saved_card()
        self._flush_pending = False
        self.outer = QVBoxLayout(self)
        self.outer.setSpacing(0)
        self.status = StatusLine(self)
        self.outer.addWidget(self.status)
        # Full layout: art + title + progress + controls, with the side buttons.
        self.full = QWidget(self)
        lay = QHBoxLayout(self.full)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.player = NowPlaying(cover=76, show_hint=False, any_media=True, volume=True)
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
        self.outer.addWidget(self.full, 1)
        # Album-art layout (a small or square window).
        self.art = ArtView(self.player, shell, self)
        self.outer.addWidget(self.art, 1)
        self.lyrics = LyricsPanel(self)
        self.outer.addWidget(self.lyrics)
        self._lyrics_on = bool(config.get("widgets.lyrics", False))
        self._fade = QTimer(self)
        self._fade.setInterval(33)
        self._fade.timeout.connect(self._decay)
        # The player only ticks itself while it's on screen; in album-art mode it's hidden.
        self._tick = QTimer(self)
        self._tick.setInterval(500)
        self._tick.timeout.connect(self._tick_hidden_player)
        self._vol_send = QTimer(self)             # scroll-wheel volume: send once the wheel stops
        self._vol_send.setSingleShot(True)
        self._vol_send.setInterval(260)
        self._vol_send.timeout.connect(lambda: self.player._set_volume(self.player.vol.value()))
        self._save_size = QTimer(self)
        self._save_size.setSingleShot(True)
        self._save_size.setInterval(400)
        self._save_size.timeout.connect(self._remember_size)
        self.player.rendered.connect(self._content_changed)
        self.lyrics.changed.connect(self._content_changed)
        ui_bus.event.connect(self._on_event)
        self.setMouseTracking(True)
        for w in (self.full, self.art, self.player, self.status, self.lyrics):
            w.setMouseTracking(True)              # resize cursors show near the edges, over children too
        self._apply_pin(bool(config.get("widgets.pinned", True)))
        self.setWindowOpacity(float(config.get("widgets.opacity", 1.0) or 1.0))
        self.set_lyrics(self._lyrics_on)

    # ------------------------------------------------------------------ #
    # Size and layout
    # ------------------------------------------------------------------ #
    @staticmethod
    def _saved_card():
        size = config.get("widgets.size")
        try:
            w, h = int(size[0]), int(size[1])
        except (TypeError, ValueError, IndexError):
            try:
                w, h = int(config.get("widgets.width") or DEFAULT_W), DEFAULT_H
            except (TypeError, ValueError):
                w, h = DEFAULT_W, DEFAULT_H
        return max(MIN_W, min(MAX_W, w)), max(MIN_H, min(MAX_H, h))

    @staticmethod
    def mode_for(w: int, h: int) -> str:
        """"art" (just the album art, controls on hover) when it's too small for the full player."""
        return "art" if w < ART_W or h < FULL_MIN_H else "full"

    def set_card(self, w: int, h: int, save: bool = False):
        """Resize the card (the visible player, without its shadow or the lyrics panel)
        and pick the layout that fits it."""
        w, h = max(MIN_W, min(MAX_W, int(w))), max(MIN_H, min(MAX_H, int(h)))
        self._card = (w, h)
        mode = self.mode_for(w, h)
        if mode != self._mode:
            self._mode = mode
            self.art.set_hover(self.underMouse())
        art = mode == "art"
        self.full.setVisible(not art)
        self.status.setVisible(not art)
        self.art.setVisible(art)
        lyr = self._lyrics_on and not art
        self.lyrics.setVisible(lyr)
        if art:
            self.outer.setContentsMargins(SHADOW, SHADOW, SHADOW, SHADOW)
        else:
            self.outer.setContentsMargins(SHADOW + 14, SHADOW + 12, SHADOW + 10, SHADOW + 12)
            room = h - 24 - STATUS_H - 10              # height beside the status line
            self.player.set_cover_size(max(56, min(room, int(w * 0.42), 260)))
        if lyr and self.isVisible():
            self.lyrics.start()
        else:
            self.lyrics.stop()
        self.setFixedSize(w + 2 * SHADOW, h + (LYRICS_H if lyr else 0) + 2 * SHADOW)
        if art:
            self.art.sync()
        self._sync_ticker()
        if save:
            self._remember_size()
        self.update()

    def _remember_size(self):
        w, h = self._card
        config.set("widgets.size", [w, h])

    def set_width(self, width: int):
        self.set_card(width, self._card[1], save=True)

    def voice_size(self, size: str):
        """"Make the mini player bigger / smaller", "just show the album art", "normal size"."""
        w, h = self._card
        if size == "art":
            w, h = SIZES["Album art"]
        elif size == "normal":
            w, h = DEFAULT_W, DEFAULT_H
        elif size in ("bigger", "smaller"):
            f = 1.3 if size == "bigger" else 1 / 1.3
            w, h = int(w * f), int(h * f)
        geo = self.geometry()
        self.set_card(w, h, save=True)
        # Grow / shrink around the bottom-right corner it usually sits in, then stay on screen.
        self.move(geo.right() + 1 - self.width(), geo.bottom() + 1 - self.height())
        self._keep_on_screen()
        self.remember_position()

    def set_lyrics(self, on: bool, save: bool = False):
        """Show or hide the lyrics panel (the window grows downwards)."""
        on = bool(on)
        self._lyrics_on = on
        if save or bool(config.get("widgets.lyrics", False)) != on:
            config.set("widgets.lyrics", on)
        if self.lyrics_btn.isChecked() != on:
            self.lyrics_btn.blockSignals(True)
            self.lyrics_btn.setChecked(on)
            self.lyrics_btn.blockSignals(False)
        self.lyrics_btn.setToolTip("Hide lyrics" if on else "Show lyrics")
        if on and self._mode == "art":
            # Lyrics need the full player: grow back to it rather than silently doing nothing.
            self._card = (max(self._card[0], DEFAULT_W), max(self._card[1], DEFAULT_H))
        self.set_card(*self._card, save=on and save)
        if self.isVisible():
            self._keep_on_screen()

    def _keep_on_screen(self):
        screen = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = max(area.left(), min(self.x(), area.right() - self.width() + SHADOW))
        y = max(area.top(), min(self.y(), area.bottom() - self.height() + SHADOW))
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

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

    # ------------------------------------------------------------------ #
    # Keeping it live
    # ------------------------------------------------------------------ #
    def _sync_ticker(self):
        if self.isVisible() and self._mode == "art":
            self._tick.start()
        else:
            self._tick.stop()

    def _tick_hidden_player(self):
        if not self.player.isVisible():
            self.player._tick()

    def _content_changed(self):
        # Paint the new track / lyric line / progress right away. Child updates were
        # sometimes only reaching the screen after a click or a move (2026-10-05):
        # a synchronous repaint of this small window doesn't wait on Qt's low-priority
        # update queue, so it never shows stale content.
        if self._flush_pending or not self.isVisible():
            return
        self._flush_pending = True
        QTimer.singleShot(0, self._flush)

    def _flush(self):
        self._flush_pending = False
        if not self.isVisible():
            return
        if self._mode == "art":
            self.art.sync()
        self.repaint()

    def showEvent(self, e):
        super().showEvent(e)
        self._update_tint()
        self.status.refresh()
        if self.lyrics.isVisible():
            self.lyrics.start()
        self._sync_ticker()
        if self._mode == "art":
            self.art.sync()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.lyrics.stop()
        self._tick.stop()

    def _on_event(self, ev):
        if ev.type == EventType.VOICE_HOTWORD:
            self._flash = 1.0
            self._fade.start()
            self.status.heard((ev.payload or {}).get("text", ""))
        elif ev.type in (EventType.SPOTIFY_PLAYBACK_CHANGED, EventType.MEDIA_CHANGED):
            self._update_tint()
            if self.isVisible():
                self.status.refresh()
        elif ev.type in (EventType.ASSISTANT_STATE, EventType.ACTIVITY_CHANGED, EventType.GAME_MODE,
                         EventType.TASK_STATE) and self.isVisible():
            self.status.refresh()
            self._content_changed()

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
        radius = ART_RADIUS if self._mode == "art" else 18
        for i in range(SHADOW, 0, -2):                                    # soft drop shadow
            g.setPen(Qt.NoPen)
            g.setBrush(QColor(0, 0, 0, int(5 * (SHADOW - i) / SHADOW * 3)))
            g.drawRoundedRect(r.adjusted(-i, -i + 4, i, i + 4), radius + i, radius + i)
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        g.fillPath(path, with_alpha(p.surface, 246))
        if self._mode != "art":
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

    # ------------------------------------------------------------------ #
    # Mouse: drag to move, drag any edge or corner to resize
    # ------------------------------------------------------------------ #
    def edges_at(self, pos) -> Qt.Edge:
        """Which card edges ``pos`` (window coordinates) is on — the resize grips."""
        r = QRect(self.rect()).adjusted(SHADOW, SHADOW, -SHADOW, -SHADOW)
        x, y = pos.x(), pos.y()
        edges = Qt.Edge(0)
        if not r.adjusted(-3, -3, 3, 3).contains(pos):
            return edges
        if x <= r.left() + EDGE:
            edges |= Qt.LeftEdge
        elif x >= r.right() - EDGE:
            edges |= Qt.RightEdge
        if y <= r.top() + EDGE:
            edges |= Qt.TopEdge
        elif y >= r.bottom() - EDGE:
            edges |= Qt.BottomEdge
        return edges

    @staticmethod
    def _cursor_for(edges) -> Qt.CursorShape:
        lt, rb = Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge
        if edges in (lt, rb):
            return Qt.SizeFDiagCursor
        if edges in (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge):
            return Qt.SizeBDiagCursor
        if edges & (Qt.LeftEdge | Qt.RightEdge):
            return Qt.SizeHorCursor
        if edges & (Qt.TopEdge | Qt.BottomEdge):
            return Qt.SizeVerCursor
        return Qt.ArrowCursor

    def resize_by(self, edges, dx: int, dy: int, start_geo: QRect = None, start_card=None):
        """Grow / shrink from ``edges`` by (dx, dy) px; the opposite edges stay put."""
        geo = QRect(start_geo or self.geometry())
        w0, h0 = start_card or self._card
        w = w0 + (dx if edges & Qt.RightEdge else -dx if edges & Qt.LeftEdge else 0)
        h = h0 + (dy if edges & Qt.BottomEdge else -dy if edges & Qt.TopEdge else 0)
        self.set_card(w, h)
        x = geo.x() + geo.width() - self.width() if edges & Qt.LeftEdge else geo.x()
        y = geo.y() + geo.height() - self.height() if edges & Qt.TopEdge else geo.y()
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        pos = e.position().toPoint()
        edges = self.edges_at(pos)
        if edges:
            self._resize = (edges, e.globalPosition().toPoint(), QRect(self.geometry()), self._card)
        else:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        gp = e.globalPosition().toPoint()
        if self._resize is not None and e.buttons() & Qt.LeftButton:
            edges, p0, geo0, card0 = self._resize
            self.resize_by(edges, gp.x() - p0.x(), gp.y() - p0.y(), geo0, card0)
        elif self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(gp - self._drag)
        else:
            self.setCursor(self._cursor_for(self.edges_at(self.mapFromGlobal(gp))))

    def mouseReleaseEvent(self, e):
        if self._resize is not None:
            self._resize = None
            self._remember_size()
            self.remember_position()
        if self._drag is not None:
            self._drag = None
            self.remember_position()

    def enterEvent(self, e):
        super().enterEvent(e)
        self.art.set_hover(True)

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.art.set_hover(False)
        if self._resize is None:
            self.unsetCursor()

    def wheelEvent(self, e):
        step = 0.05 if e.angleDelta().y() > 0 else -0.05
        if e.modifiers() & Qt.ControlModifier:                 # ctrl+scroll fades it
            self.set_opacity(self.windowOpacity() + step)
        elif self.player.vol_icon.isEnabled():                 # scroll = volume of what's playing
            vol = self.player.vol
            vol.blockSignals(True)
            vol.setValue(max(0, min(100, vol.value() + int(step * 100))))
            vol.blockSignals(False)
            self.player.vol_icon.set_icon("volume-x" if vol.value() == 0 else "volume")
            self.status.note(f"Volume {vol.value()}%")
            self._vol_send.start()
            self._content_changed()
        e.accept()

    def contextMenuEvent(self, e):
        from ui import placement
        menu = QMenu(self)
        pin = QAction("Keep on top", menu, checkable=True)
        pin.setChecked(bool(config.get("widgets.pinned", True)))
        pin.triggered.connect(self.set_pinned)
        menu.addAction(pin)
        lyr = QAction("Lyrics", menu, checkable=True)
        lyr.setChecked(self._lyrics_on)
        lyr.triggered.connect(lambda on: self.set_lyrics(on, save=True))
        menu.addAction(lyr)
        op = menu.addMenu("Opacity")
        for v in OPACITIES:
            a = QAction(f"{int(v * 100)}%", op, checkable=True)
            a.setChecked(abs(self.windowOpacity() - v) < 0.03)
            a.triggered.connect(lambda _=False, v=v: self.set_opacity(v))
            op.addAction(a)
        size = menu.addMenu("Size")
        for name, (w, h) in SIZES.items():
            a = QAction(name, size, checkable=True)
            a.setChecked(self._card == (w, h))
            a.triggered.connect(lambda _=False, w=w, h=h: (self.set_card(w, h, save=True), self._keep_on_screen()))
            size.addAction(a)
        size.addSeparator()
        size.addAction("Drag any edge or corner to resize").setEnabled(False)
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
        self._keep_on_screen()
        if self._mode == "full":
            motion.fade_in(self.player, motion.SLOW)
        actions.refresh_spotify()
        from modules.desktop.media import media
        media.start()
        self._content_changed()

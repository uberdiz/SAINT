"""
ui/pages/music.py

Spotify: the big now-playing hero (album art, live progress, controls),
hands-free hot-words, the queue and quick actions. ``NowPlaying`` is the
player reused by Home, the overlay and the floating widget.
"""

import time

from PySide6.QtCore import QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPainterPath, QRadialGradient
from PySide6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QVBoxLayout, QWidget)

from core.config import config
from core.events import EventType
from ui import actions, motion
from ui.reactive import ui_bus
from ui.theme import current_palette
from ui.widgets import (Card, CoverArt, ElidedLabel, IconButton, Page, ProgressLine, chip, covers,
                        run_async, set_chip, with_alpha)


def _mmss(ms) -> str:
    s = int((ms or 0) / 1000)
    return f"{s // 60}:{s % 60:02d}"


class NowPlaying(QWidget):
    """Cover + title + artist + progress + controls, live from ui_bus.

    ``any_media``: show whatever is playing on the PC (a YouTube video, VLC,
    ...), not only Spotify — the controls then follow that app."""

    def __init__(self, cover: int = 64, big: bool = False, show_hint: bool = True,
                 hint: str = "Say “skip” — no wake word", parent=None, any_media: bool = False):
        super().__init__(parent)
        self._big, self._show_hint, self._hint_text = big, show_hint, hint
        self._any, self._sp_ok, self._source = any_media, False, "spotify"
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(28 if big else 14)
        self.cover = CoverArt(cover, radius=14 if big else max(8, cover // 7))
        lay.addWidget(self.cover, 0, Qt.AlignTop if not big else Qt.AlignVCenter)

        col = QVBoxLayout()
        col.setSpacing(4 if not big else 6)
        lay.addLayout(col, 1)
        self.state_chip = chip("Now playing", "accent")
        self.state_chip.setVisible(big)
        top = QHBoxLayout()
        top.addWidget(self.state_chip)
        top.addStretch()
        if big:
            col.addLayout(top)
        self.title = ElidedLabel("—")
        self.title.setObjectName("Display" if big else "SectionTitle")
        self.artist = ElidedLabel("")
        self.artist.setObjectName("Muted")
        if big:
            self.artist.setStyleSheet("font-size: 16px;")
        self.album = ElidedLabel("")
        self.album.setObjectName("Faint")
        self.album.setVisible(big)
        for w in (self.title, self.artist, self.album):
            col.addWidget(w)
        if big:
            col.addStretch()
        prog = QHBoxLayout()
        prog.setSpacing(10)
        self.pos_label = QLabel("0:00")
        self.pos_label.setObjectName("Faint")
        self.progress = ProgressLine(4 if big else 3)
        self.dur_label = QLabel("0:00")
        self.dur_label.setObjectName("Faint")
        prog.addWidget(self.pos_label)
        prog.addWidget(self.progress, 1)
        prog.addWidget(self.dur_label)
        self.pos_label.setVisible(big)
        self.dur_label.setVisible(big)
        col.addSpacing(4)
        col.addLayout(prog)

        ctl = QHBoxLayout()
        ctl.setSpacing(4 if not big else 10)
        size = 20 if big else 16
        self.shuffle = IconButton("shuffle", "Shuffle", size, checkable=True)
        self.prev = IconButton("prev", "Previous", size)
        self.play = IconButton("play", "Play / pause", size + (4 if big else 0), role="inverse")
        self.play.setObjectName("PlayButton")
        d = 50 if big else 32
        self.play.setFixedSize(d, d)
        self.play.setStyleSheet(f"QToolButton#PlayButton {{ border-radius: {d // 2}px; }}")
        self.next = IconButton("next", "Next", size)
        self.like = IconButton("heart", "I like this — SAINT remembers", size)
        self.vol_down = IconButton("volume-down", "Quieter", size)
        self.vol_up = IconButton("volume", "Louder", size)
        self.shuffle.clicked.connect(lambda: actions.spotify("spotify.shuffle", self._err,
                                                             state=not ui_bus.spotify.get("shuffle")))
        self.prev.clicked.connect(lambda: actions.transport("previous", self._err) if self._any
                                  else actions.spotify("spotify.previous", self._err))
        self.play.clicked.connect(lambda: actions.transport("play_pause", self._err) if self._any
                                  else actions.play_pause(self._err))
        self.next.clicked.connect(lambda: actions.transport("next", self._err) if self._any
                                  else actions.spotify("spotify.next", self._err))
        self.like.clicked.connect(self._like)
        self.vol_down.clicked.connect(lambda: actions.spotify("spotify.volume_step", self._err, direction="down"))
        self.vol_up.clicked.connect(lambda: actions.spotify("spotify.volume_step", self._err, direction="up"))
        self._controls = [self.shuffle, self.prev, self.play, self.next, self.like, self.vol_down, self.vol_up]
        if big:
            ctl.addWidget(self.shuffle)
        ctl.addWidget(self.prev)
        ctl.addWidget(self.play)
        ctl.addWidget(self.next)
        if big:
            ctl.addWidget(self.like)
        ctl.addStretch()
        self.hint = chip(hint, "accent")
        self.hint.setToolTip("While music plays, “skip”, “pause”, “go back”, “louder” and “quieter” "
                             "work without saying “Hey SAINT”.")
        ctl.addWidget(self.hint, 0, Qt.AlignVCenter)
        if big:
            ctl.addWidget(self.vol_down)
            ctl.addWidget(self.vol_up)
        else:
            for w in (self.shuffle, self.like, self.vol_down, self.vol_up):
                w.hide()
        col.addLayout(ctl)
        self.status = QLabel("")
        self.status.setObjectName("Faint")
        self.status.setWordWrap(True)
        self.status.hide()
        col.addWidget(self.status)
        if not big:
            col.addStretch()

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._hint_timer = QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.timeout.connect(self._reset_hint)
        ui_bus.event.connect(self._on_event)
        ui_bus.demo_changed.connect(lambda _on: self.check())
        self.check()

    # ------------------------------------------------------------------ #
    def check(self):
        # availability() may refresh the Spotify token over the network — never on the GUI thread.
        if ui_bus.demo:
            self._apply(True, "")
        else:
            run_async(actions.spotify_status, lambda r: self._apply(*r), lambda e: self._apply(False, e))

    def _apply(self, ok, reason):
        ok = ok or ui_bus.demo
        self._sp_ok = ok
        if self._any:
            self.setToolTip("" if ok else reason)
            self.render_any()
            return
        for b in self._controls:
            b.setEnabled(ok)
        if ok:
            self._err("")
            self.render(ui_bus.spotify)
        else:
            self.title.setText("Spotify isn't connected")
            self.artist.setText("Connect it in Settings › Spotify")
            self.setToolTip(reason)
            self._err("")
            self.cover.set_url("")
            self.progress.set_value(0)
            self.hint.hide()

    def render(self, st):
        if not st.get("track"):
            self.title.setText("Nothing playing")
            self.artist.setText(st.get("device") or "Say “Hey SAINT, play something”")
            self.album.setText("")
            self.cover.set_url("")
            self.play.set_icon("play")
            set_chip(self.state_chip, "Idle")
            self.hint.hide()
            self._tick()
            return
        self.title.setText(st["track"])
        self.artist.setText(st.get("artists", ""))
        self.album.setText(st.get("album", "") or "")
        self.cover.set_url(st.get("image_large") or st.get("image") or "")
        playing = bool(st.get("is_playing"))
        self.play.set_icon("pause" if playing else "play")
        self.shuffle.setChecked(bool(st.get("shuffle")))
        set_chip(self.state_chip, ("Playing" if playing else "Paused")
                 + (f" on {st['device']}" if st.get("device") else ""), "accent" if playing else "")
        self.hint.setVisible(self._show_hint and playing and actions.hotwords_on())
        self._tick()

    def render_any(self):
        """Any-media mode: the video / app / song Windows says is playing."""
        np = ui_bus.now_playing()
        if not np or (np["source"] == "spotify" and not self._sp_ok):
            np = ui_bus._from_media(ui_bus.media) if ui_bus.media.get("title") else {}
        self._source = np.get("source", "")
        for b in self._controls:
            b.setEnabled(bool(np))
        if not np:
            self.title.setText("Nothing playing")
            self.artist.setText("Play something — Spotify, YouTube, anything")
            self.album.setText("")
            self.cover.set_url("")
            self.play.set_icon("play")
            set_chip(self.state_chip, "Idle")
            self.hint.hide()
            self._tick()
            return
        if np["source"] == "spotify":
            self.render(ui_bus.spotify)
            return
        self.title.setText(np["title"])
        by = np.get("artist") or ""
        app = np.get("app") or ""
        self.artist.setText(f"{by} · {app}" if by and app and by != app else (by or app))
        self.album.setText(np.get("album", ""))
        self.cover.set_url(np.get("cover", ""))
        playing = np.get("is_playing")
        self.play.set_icon("pause" if playing else "play")
        self.prev.setEnabled(np.get("can_previous", False))
        self.next.setEnabled(np.get("can_next", False))
        set_chip(self.state_chip, ("Playing" if playing else "Paused") + (f" in {app}" if app else ""),
                 "accent" if playing else "")
        self.hint.hide()
        self._tick()

    def _tick(self):
        if self._any and self._source == "media":
            np = ui_bus.now_playing() or ui_bus._from_media(ui_bus.media)
            frac, dur = ui_bus.progress_of(np), np.get("duration_ms") or 0
        else:
            frac, dur = ui_bus.progress(), ui_bus.spotify.get("duration_ms") or 0
        self.progress.set_value(frac)
        self.pos_label.setText(_mmss(frac * dur))
        self.dur_label.setText(_mmss(dur))

    def _err(self, msg):
        self.status.setText(msg or "")
        self.status.setVisible(bool(msg))

    def _like(self):
        actions.spotify("spotify.feedback", self._err, signal=1, reason="liked in SAINT")

    def _on_event(self, ev):
        t, p = ev.type, ev.payload or {}
        if t == EventType.SPOTIFY_PLAYBACK_CHANGED:
            self._err("")
            self.render_any() if self._any else self.render(p)
        elif t == EventType.MEDIA_CHANGED and self._any:
            self._err("")
            self.render_any()
        elif t == EventType.SPOTIFY_ERROR:
            self._err(p.get("error") or "Spotify error")
        elif t in (EventType.SPOTIFY_CONNECTED, EventType.SPOTIFY_DISCONNECTED, EventType.SETTINGS_CHANGED):
            self.check()
        elif t == EventType.VOICE_HOTWORD and self._show_hint:
            set_chip(self.hint, f"Heard “{p.get('text', '')}” ✓", "ok")
            self.hint.show()
            self._hint_timer.start(1800)

    def _reset_hint(self):
        set_chip(self.hint, self._hint_text, "accent")
        self.render_any() if self._any else self.render(ui_bus.spotify)

    def showEvent(self, e):
        super().showEvent(e)
        self._timer.start()
        self.check()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()


class GlowCard(QFrame):
    """Card with a soft glow taken from the album cover's colour."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self._color = QColor(current_palette().accent)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(motion.SLOW * 2)
        self._anim.valueChanged.connect(self._set)

    def set_color(self, color: QColor):
        if color is None or color == self._color:
            return
        if not motion.enabled():
            self._set(color)
            return
        self._anim.stop()
        self._anim.setStartValue(QColor(self._color))
        self._anim.setEndValue(QColor(color))
        self._anim.start()

    def _set(self, c):
        self._color = QColor(c)
        self.update()

    def paintEvent(self, e):
        super().paintEvent(e)
        g = QPainter(self)
        g.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, 12, 12)
        g.setClipPath(path)
        grad = QRadialGradient(r.left() + r.width() * 0.18, r.top() + r.height() * 0.5, r.width() * 0.6)
        grad.setColorAt(0, with_alpha(self._color, 70))
        grad.setColorAt(1, with_alpha(self._color, 0))
        g.fillRect(r, grad)
        g.end()


class MusicPage(Page):
    def __init__(self, shell):
        super().__init__("Music", "Spotify with live covers, your queue, and hands-free control.")
        self.shell = shell
        self.widget_btn = QPushButton("Floating widget")
        self.widget_btn.setCheckable(True)
        self.widget_btn.setToolTip("An always-on-top mini player that stays on screen")
        self.widget_btn.toggled.connect(lambda on: shell.set_widget(on))
        self.actions.addWidget(self.widget_btn)

        self.hero = GlowCard()
        hl = QVBoxLayout(self.hero)
        hl.setContentsMargins(28, 28, 28, 24)
        self.player = NowPlaying(cover=232, big=True, show_hint=True)
        hl.addWidget(self.player)
        self.root.addWidget(self.hero)

        row = QHBoxLayout()
        row.setSpacing(16)
        self.root.addLayout(row)

        free = Card("Hands-free")
        free.body.addWidget(QLabel("While music plays, just say it — no “Hey SAINT” needed."))
        words = QGridLayout()
        words.setSpacing(6)
        for i, w in enumerate(("skip", "go back", "pause", "resume", "louder", "quieter", "I love this")):
            words.addWidget(chip(f"“{w}”"), i // 4, i % 4)
        free.body.addLayout(words)
        self.hot_check = QCheckBox("Music hot-words")
        self.hot_check.setChecked(actions.hotwords_on())
        self.hot_check.toggled.connect(lambda on: config.set("voice.music_hotwords", bool(on)))
        free.body.addWidget(self.hot_check)
        self.last_heard = QLabel("")
        self.last_heard.setObjectName("Faint")
        free.body.addWidget(self.last_heard)
        free.body.addStretch()
        row.addWidget(free, 2)

        queue = Card("Up next")
        self.queue = QListWidget()
        self.queue.setObjectName("Flat")
        self.queue.setFocusPolicy(Qt.NoFocus)
        queue.body.addWidget(self.queue)
        row.addWidget(queue, 3)

        quick = Card("Quick")
        grid = QGridLayout()
        grid.setSpacing(8)
        for i, (label, tool, kw) in enumerate((
                ("Recommend for me", "spotify.play_recommended", {}),
                ("More like this", "spotify.play_recommended", {"similar_to_current": True}),
                ("Liked songs", "spotify.play_liked", {}),
                ("Replay track", "spotify.replay", {}),
                ("Smart Shuffle on", "spotify.smart_shuffle", {"state": True}),
                ("Smart Shuffle off", "spotify.smart_shuffle", {"state": False}))):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, t=tool, k=kw: actions.spotify(t, self.player._err, **k))
            grid.addWidget(b, i // 2, i % 2)
        quick.body.addLayout(grid)
        quick.body.addStretch()
        row.addWidget(quick, 2)
        self.root.addStretch()

        self._queue_timer = QTimer(self)
        self._queue_timer.setInterval(12000)
        self._queue_timer.timeout.connect(self._refresh_queue)
        ui_bus.event.connect(self._on_event)

    def apply_theme(self):
        self.widget_btn.blockSignals(True)
        self.widget_btn.setChecked(bool(config.get("widgets.spotify", False)))
        self.widget_btn.blockSignals(False)
        self.hot_check.blockSignals(True)
        self.hot_check.setChecked(actions.hotwords_on())
        self.hot_check.blockSignals(False)

    def _on_event(self, ev):
        t, p = ev.type, ev.payload or {}
        if t == EventType.SPOTIFY_PLAYBACK_CHANGED:
            url = p.get("image_large") or p.get("image") or ""
            self.hero.set_color(covers.color(url))
            if url and covers.color(url) is None:
                covers.get(url, lambda _pm, u=url: self.hero.set_color(covers.color(u)))
            if self.isVisible() and not ui_bus.demo:
                QTimer.singleShot(600, self._refresh_queue)
        elif t == EventType.VOICE_HOTWORD:
            self.last_heard.setText(f"Last heard: “{p.get('text', '')}” · {time.strftime('%H:%M')}")

    def _refresh_queue(self):
        if ui_bus.demo:
            return

        def fetch():
            try:
                items = actions.fetch_queue(14)
            except RuntimeError as e:
                return [str(e)]
            return [f"{name}   ·   {artists}" for name, artists in items] or ["The queue is empty."]
        run_async(fetch, self._render_queue, lambda e: self._render_queue([f"Couldn't load the queue: {e}"]))

    def _render_queue(self, lines):
        self.queue.clear()
        for line in lines:
            self.queue.addItem(QListWidgetItem(line))

    def set_queue(self, lines):
        self._render_queue(lines)

    def showEvent(self, e):
        super().showEvent(e)
        self.apply_theme()
        self._refresh_queue()
        self._queue_timer.start()
        actions.refresh_spotify()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._queue_timer.stop()

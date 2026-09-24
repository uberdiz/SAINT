"""
ui/spotify_ui.py

Dedicated Spotify tab: full now-playing card, playback controls,
current queue, and quick-access shortcuts. Reuses the same runtime tools as
voice/agent commands so the UI and voice paths never disagree.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PySide6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)

from core.events import event_bus, EventType
from ui.widgets import Card, run_async


class SpotifyUI(QWidget):
    """Spotify page: album art + track + controls + queue."""

    def __init__(self):
        super().__init__()
        self._current_image_url = ""
        self._net = QNetworkAccessManager(self)
        self._net.finished.connect(self._on_image_loaded)
        self._build()
        event_bus.event_occurred.connect(self._on_event)
        # Poll cheaply for state so the UI updates even when no event fires.
        self._timer = QTimer(self)
        self._timer.setInterval(4000)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        QTimer.singleShot(300, self._refresh)

    # ------------------------------------------------------------------ #
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)

        # Now playing card ------------------------------------------------
        self.hero = Card("Now playing")
        row = QHBoxLayout()
        self.art = QLabel()
        self.art.setFixedSize(180, 180)
        self.art.setAlignment(Qt.AlignCenter)
        self.art.setStyleSheet("border-radius:8px; background:#151515;")
        self.art.setText("♫")
        row.addWidget(self.art)

        meta = QVBoxLayout()
        meta.setSpacing(6)
        self.title = QLabel("—")
        self.title.setObjectName("SectionTitle")
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-size: 20px; font-weight: 700;")
        self.artist = QLabel("")
        self.artist.setObjectName("Muted")
        self.artist.setWordWrap(True)
        self.album = QLabel("")
        self.album.setObjectName("Faint")
        self.album.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.meta = QLabel("Not connected")
        self.meta.setObjectName("Faint")
        for w in (self.title, self.artist, self.album, self.progress, self.meta):
            meta.addWidget(w)
        meta.addStretch()
        row.addLayout(meta, 1)
        self.hero.body.addLayout(row)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.b_prev = self._button("◀◀", "spotify.previous")
        self.b_play = self._button("▶ ❚❚", None)     # toggles based on state
        self.b_next = self._button("▶▶", "spotify.next")
        self.b_shuffle = self._button("⤨ Shuffle", None)
        self.b_shuffle.clicked.disconnect()
        self.b_shuffle.clicked.connect(self._toggle_shuffle)
        self.b_liked = self._button("♥ Liked", None)
        self.b_liked.clicked.disconnect()
        self.b_liked.clicked.connect(lambda: self._run("spotify.play_liked"))
        for b in (self.b_prev, self.b_play, self.b_next, self.b_shuffle, self.b_liked):
            controls.addWidget(b)
        controls.addStretch()
        self.hero.body.addLayout(controls)
        root.addWidget(self.hero)

        # Queue card ------------------------------------------------------
        self.queue_card = Card("Up next")
        self.queue_list = QListWidget()
        self.queue_list.setMaximumHeight(260)
        self.queue_card.body.addWidget(self.queue_list)
        root.addWidget(self.queue_card)

        # Quick actions grid ---------------------------------------------
        actions_card = Card("Quick actions")
        grid = QGridLayout()
        grid.setSpacing(8)
        quick = [
            ("Recommend for me", "spotify.play_recommended", {}),
            ("Similar to this", "spotify.play_recommended", {"similar_to_current": True}),
            ("Smart Shuffle on", "spotify.smart_shuffle", {"state": True}),
            ("Smart Shuffle off", "spotify.smart_shuffle", {"state": False}),
            ("Volume up", "spotify.volume_step", {"direction": "up"}),
            ("Volume down", "spotify.volume_step", {"direction": "down"}),
        ]
        for i, (label, tool, kwargs) in enumerate(quick):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, t=tool, k=kwargs: self._run(t, **k))
            grid.addWidget(btn, i // 3, i % 3)
        actions_card.body.addLayout(grid)
        root.addWidget(actions_card)
        root.addStretch()

    def _button(self, label: str, tool):
        b = QPushButton(label)
        b.setMinimumWidth(60)
        b.clicked.connect(lambda _=False: self._toggle_play() if tool is None else self._run(tool))
        return b

    # ------------------------------------------------------------------ #
    # Runtime
    # ------------------------------------------------------------------ #
    def _run(self, tool: str, **kwargs):
        from modules.automation.tools import get_tool_registry

        def done(res):
            if not res.success:
                self.meta.setText(res.error or "Spotify action failed")
        run_async(lambda: get_tool_registry().execute(tool, **kwargs), done)

    def _toggle_play(self):
        state = getattr(self, "_state", {}) or {}
        tool = "spotify.pause" if state.get("is_playing") else "spotify.play"
        self._run(tool)

    def _toggle_shuffle(self):
        state = getattr(self, "_state", {}) or {}
        self._run("spotify.shuffle", state=not state.get("shuffle"))

    def _refresh(self):
        from core.module_manager import module_manager
        sp = module_manager.get("spotify")
        if sp is None:
            self.meta.setText("Spotify module is disabled — enable it in Settings > Spotify.")
            return
        ok, reason = sp.availability()
        if not ok:
            self.meta.setText(reason)
            return
        run_async(lambda: sp.tools.current(), None, lambda e: self.meta.setText(str(e)[:120]))
        run_async(lambda: self._fetch_queue(sp), self._render_queue,
                  lambda e: self.queue_list.clear())

    @staticmethod
    def _fetch_queue(sp):
        try:
            data = sp.tools.client.get_queue() or {}
        except Exception:
            return []
        out = []
        for item in (data.get("queue") or [])[:12]:
            if not item:
                continue
            artists = ", ".join(a.get("name", "") for a in (item.get("artists") or [])[:2])
            out.append(f"{item.get('name', '?')}  ·  {artists}")
        return out

    def _render_queue(self, items):
        self.queue_list.clear()
        if not items:
            self.queue_list.addItem(QListWidgetItem("The queue is empty."))
            return
        for line in items:
            self.queue_list.addItem(QListWidgetItem(line))

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    def _on_event(self, ev):
        t = ev.type
        p = ev.payload or {}
        if t == EventType.SPOTIFY_PLAYBACK_CHANGED:
            self._render(p)
        elif t == EventType.SPOTIFY_ERROR:
            self.meta.setText((p.get("error") or "Spotify error")[:160])
        elif t in (EventType.SPOTIFY_CONNECTED, EventType.SPOTIFY_DISCONNECTED):
            QTimer.singleShot(200, self._refresh)

    def _render(self, st):
        self._state = st
        if not st.get("track"):
            self.title.setText("Nothing playing")
            self.artist.setText(st.get("device") or "")
            self.album.setText("")
            self.progress.setValue(0)
            self.meta.setText("Ready when you are.")
            self._set_art("")
            return
        self.title.setText(st["track"])
        self.artist.setText(st.get("artists", ""))
        self.album.setText(st.get("album", ""))
        dur = st.get("duration_ms") or 1
        self.progress.setRange(0, 1000)
        self.progress.setValue(int(1000 * st.get("progress_ms", 0) / dur))
        vol = st.get("volume")
        parts = ["▶ Playing" if st.get("is_playing") else "⏸ Paused"]
        if st.get("device"):
            parts.append(f"on {st['device']}")
        if vol is not None:
            parts.append(f"volume {vol}%")
        if st.get("shuffle"):
            parts.append("shuffle on")
        self.meta.setText(" · ".join(parts))
        self._set_art(st.get("image") or "")

    # ------------------------------------------------------------------ #
    # Album art
    # ------------------------------------------------------------------ #
    def _set_art(self, url: str):
        if url == self._current_image_url:
            return
        self._current_image_url = url
        if not url:
            self.art.clear()
            self.art.setText("♫")
            return
        self._net.get(QNetworkRequest(url))     # signal handled in _on_image_loaded

    def _on_image_loaded(self, reply):
        try:
            if reply.error():
                return
            data = reply.readAll()
            pm = QPixmap()
            if pm.loadFromData(data):
                self.art.setPixmap(pm.scaled(self.art.width(), self.art.height(),
                                             Qt.KeepAspectRatio, Qt.SmoothTransformation))
        finally:
            reply.deleteLater()

"""
ui/reactive.py

The UI's view of SAINT. ``ui_bus`` re-emits every core event on the GUI
thread and caches the latest assistant state, Spotify playback and mic level,
so any window (main app, overlay, halo, widget) can render the same truth the
moment it opens.

Demo mode swaps the source: real events are muted and ``inject()`` feeds
scripted ones. Scripted events never reach core subscribers (conversation,
voice, analytics, history), so a demo can't trigger real actions or pollute
your data.
"""

import time

from PySide6.QtCore import QObject, Signal

from core.assistant_state import assistant_state
from core.events import Event, event_bus, EventType


class UIBus(QObject):
    event = Signal(object)
    demo_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.state = assistant_state.snapshot()
        self.spotify = {}
        self.spotify_at = 0.0
        self.level = 0.0
        self.demo = False
        self._saved = None
        event_bus.event_occurred.connect(self._forward)

    def _forward(self, ev):
        if self.demo:
            return
        self._track(ev)
        self.event.emit(ev)

    def inject(self, type_, payload=None):
        ev = Event(type_, payload or {})
        self._track(ev)
        self.event.emit(ev)

    def _track(self, ev):
        t, p = ev.type, ev.payload or {}
        if t == EventType.ASSISTANT_STATE:
            self.state = dict(p)
        elif t == EventType.SPOTIFY_PLAYBACK_CHANGED:
            self.spotify = dict(p)
            self.spotify_at = time.time()
        elif t == EventType.VOICE_AUDIO_LEVEL:
            self.level = float(p.get("level", 0.0))

    # ------------------------------------------------------------------ #
    def progress(self) -> float:
        """Spotify position 0..1, interpolated between polls."""
        st = self.spotify
        dur = st.get("duration_ms") or 0
        if not dur:
            return 0.0
        pos = st.get("progress_ms", 0) + ((time.time() - self.spotify_at) * 1000 if st.get("is_playing") else 0)
        return max(0.0, min(1.0, pos / dur))

    def music_active(self) -> bool:
        return bool(self.spotify.get("track"))

    def set_demo(self, on: bool):
        if on == self.demo:
            return
        if on:
            self._saved = (dict(self.spotify), self.spotify_at)
            self.demo = True
        else:
            self.demo = False
            spotify, at = self._saved or ({}, 0.0)
            self.state = assistant_state.snapshot()
            self.event.emit(Event(EventType.ASSISTANT_STATE, self.state))
            self.spotify, self.spotify_at = spotify, at
            self.event.emit(Event(EventType.SPOTIFY_PLAYBACK_CHANGED, spotify))
        self.demo_changed.emit(on)


ui_bus = UIBus()

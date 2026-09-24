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
    # A setting that should show its effect right away (Halo, action notices, ...)
    setting_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.state = assistant_state.snapshot()
        self.spotify = {}
        self.spotify_at = 0.0
        self.media = {}               # Windows media session (any app) — modules/desktop/media.py
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
        elif t == EventType.MEDIA_CHANGED:
            self.media = dict(p)

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

    def now_playing(self) -> dict:
        """What the mini player shows: a video or another app that's playing,
        else Spotify (richer data from its API), else whatever Windows has.
        Same shape for every source."""
        m, sp = self.media, self.spotify
        if self.demo:
            m = {}
        if m.get("title") and m.get("is_playing") and not m.get("is_spotify"):
            return self._from_media(m)
        if sp.get("track"):
            return {"source": "spotify", "title": sp["track"], "artist": sp.get("artists", ""),
                    "album": sp.get("album", "") or "", "cover": sp.get("image_large") or sp.get("image") or "",
                    "is_playing": bool(sp.get("is_playing")), "position_ms": sp.get("progress_ms", 0),
                    "duration_ms": sp.get("duration_ms") or 0, "at": self.spotify_at, "app": "Spotify",
                    "device": sp.get("device", ""), "can_next": True, "can_previous": True}
        if m.get("title"):
            return self._from_media(m)
        return {}

    @staticmethod
    def _from_media(m: dict) -> dict:
        return {"source": "media", "title": m.get("title", ""), "artist": m.get("artist", "") or m.get("app", ""),
                "album": m.get("album", ""), "cover": m.get("thumb", ""), "is_playing": bool(m.get("is_playing")),
                "position_ms": m.get("position_ms", 0), "duration_ms": m.get("duration_ms") or 0,
                "at": m.get("at", time.time()), "app": m.get("app", ""), "app_id": m.get("app_id", ""),
                "can_next": bool(m.get("can_next")), "can_previous": bool(m.get("can_previous")),
                "is_spotify": bool(m.get("is_spotify"))}

    @staticmethod
    def progress_of(np: dict) -> float:
        dur = np.get("duration_ms") or 0
        if not dur:
            return 0.0
        pos = (np.get("position_ms") or 0) + ((time.time() - np.get("at", time.time())) * 1000
                                              if np.get("is_playing") else 0)
        return max(0.0, min(1.0, pos / dur))

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

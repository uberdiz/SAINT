"""
modules/agent/context.py

Short-term desktop context for resolving references across turns:
"move the browser to my second monitor" ... "make it bigger" ... "close it".

Tracks the window SAINT last acted on, the UI element it last found or
clicked, the list of results it last saw ("click the first one") and the
domain of the last command (music vs. desktop), so "turn the volume down"
can mean Spotify after a music command and the system volume after a video.
Entries expire so an old reference never hijacks a new command.
"""

import threading
import time
from typing import Optional

REFERENCE_TTL = 180.0     # seconds a remembered "it" stays valid


class DesktopContext:
    def __init__(self):
        self._lock = threading.Lock()
        self._window = None          # (hwnd, title, time)
        self._element = None         # (name, x, y, time)
        self._domain = ("", 0.0)
        self._spotify_playing = (False, 0.0)
        try:
            from core.events import event_bus, EventType
            event_bus.subscribe(self._on_event)
        except Exception:
            pass

    def _on_event(self, ev):
        from core.events import EventType
        if ev.type == EventType.SPOTIFY_PLAYBACK_CHANGED:
            with self._lock:
                self._spotify_playing = (bool((ev.payload or {}).get("is_playing")), time.time())

    def music_is_context(self) -> bool:
        """Does an ambiguous "turn it down" / "pause" refer to the music?
        Yes after a music command; with no recent context, yes if Spotify is playing."""
        d = self.domain()
        if d:
            return d == "spotify"
        with self._lock:
            playing, t = self._spotify_playing
        return playing and time.time() - t < 120

    def note_window(self, hwnd: int, title: str = ""):
        if hwnd:
            try:
                import win32gui
                fg = win32gui.GetForegroundWindow()
            except Exception:
                fg = None
            with self._lock:
                self._window = (int(hwnd), title, time.time())
                self._fg_at_note = fg

    def foreground_at_note(self):
        """Which window was in front when SAINT last noted its working window
        (to tell whether the user has switched away since)."""
        with self._lock:
            return getattr(self, "_fg_at_note", None)

    def window(self) -> Optional[int]:
        with self._lock:
            w = self._window
        if not w or time.time() - w[2] > REFERENCE_TTL:
            return None
        try:
            import win32gui
            if not win32gui.IsWindow(w[0]) or not win32gui.IsWindowVisible(w[0]):
                return None
        except Exception:
            pass
        return w[0]

    def note_element(self, name: str, x: int, y: int):
        with self._lock:
            self._element = (name, int(x), int(y), time.time())

    def element(self):
        with self._lock:
            e = self._element
        if not e or time.time() - e[3] > REFERENCE_TTL:
            return None
        return e[:3]

    def note_domain(self, domain: str):
        with self._lock:
            self._domain = (domain, time.time())

    def domain(self, max_age: float = 300.0) -> str:
        with self._lock:
            d, t = self._domain
        return d if time.time() - t <= max_age else ""

    def clear(self):
        with self._lock:
            self._window = self._element = None
            self._domain = ("", 0.0)


desktop_context = DesktopContext()

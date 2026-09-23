"""Spotify integration module with playback, taste memory, and recommendations."""

import logging
import threading
import time

from modules.base import BaseModule
from modules.spotify.auth import SpotifyAuth, DEFAULT_REDIRECT_URI
from modules.spotify.client import SpotifyClient, SpotifyAPIError
from modules.spotify.tools import SpotifyTools
from core.config import config
from core.events import event_bus, EventType

log = logging.getLogger("saint.spotify")


class SpotifyModule(BaseModule):
    name = "Spotify"
    description = "Spotify playback, personalized listening memory, and recommendations."

    SCOPES = (
        "user-read-playback-state "
        "user-modify-playback-state "
        "user-read-currently-playing "
        "user-read-recently-played "
        "user-top-read "
        "user-library-read "
        "playlist-read-private "
        "playlist-read-collaborative "
        "playlist-modify-public "
        "playlist-modify-private"
    ).split()

    def __init__(self):
        super().__init__()
        self.subtasks = {
            "OAuth PKCE": True,
            "API Client": True,
            "Tool Registry": True,
            "Entity Resolution": True,
            "Device Auto-Activation": True,
            "Playback": True,
            "Playlist Access": True,
            "Listening Memory": True,
            "Skip / Request Tracking": True,
            "Personalized Recommendations": True,
        }
        self.auth = SpotifyAuth()
        self.client = SpotifyClient(self.auth)
        self.tools = SpotifyTools(self.client)
        self._poll_thread = None
        self._poll_stop = threading.Event()
        self._last_error_code = ""

    # ------------------------------------------------------------------ #
    def availability(self):
        if not self.enabled:
            return False, "Spotify isn't enabled. Turn it on in Settings > Spotify."
        if not self.auth.is_configured():
            return False, "Spotify isn't set up yet — add your Client ID in Settings > Spotify."
        if not self.is_connected():
            return False, "Spotify isn't connected. Connect it in Settings > Spotify."
        return True, ""

    def enable(self):
        super().enable()
        self.tools.register(availability=self.availability)
        self.start_poller()

    def disable(self):
        super().disable()
        self.stop_poller()

    def is_connected(self):
        try:
            return self.auth.get_access_token() is not None
        except Exception:
            return False

    def connect(self):
        ok = self.auth.authenticate(self.SCOPES)
        event_bus.emit_event(EventType.SPOTIFY_CONNECTED, {"connected": ok})
        if ok:
            self.start_poller()
        return ok

    def disconnect(self):
        self.auth.clear()
        event_bus.emit_event(EventType.SPOTIFY_DISCONNECTED, {})

    def settings(self):
        return {
            "client_id_configured": bool(config.get("spotify.client_id", "")),
            "redirect_uri": config.get("spotify.redirect_uri", DEFAULT_REDIRECT_URI),
            "connected": self.is_connected(),
            "memory_enabled": config.get("spotify.track_history", True),
        }

    # ------------------------------------------------------------------ #
    # Background poller: keeps the dashboard's "now playing" card real and
    # builds listening memory, without the UI being open.
    # ------------------------------------------------------------------ #
    def start_poller(self):
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="spotify-poller")
        self._poll_thread.start()

    def stop_poller(self):
        self._poll_stop.set()

    def _poll_loop(self):
        while not self._poll_stop.is_set():
            interval = max(5, int(config.get("spotify.poll_interval_sec", 15)))
            if self.enabled and self.auth.is_configured() and self.is_connected():
                try:
                    self.tools.poll_once()
                    if self._last_error_code:
                        log.info("spotify.poller.recovered")
                    self._last_error_code = ""
                except SpotifyAPIError as e:
                    if e.code != self._last_error_code:   # log each distinct failure once
                        log.warning("spotify.poller.error code=%s %s", e.code, e)
                        event_bus.emit_event(EventType.SPOTIFY_ERROR, {"code": e.code, "error": e.user_message})
                    self._last_error_code = e.code
                    if e.code == "RATE_LIMITED":
                        interval = 60
                except Exception:
                    log.exception("spotify.poller.crash")
            self._poll_stop.wait(interval)

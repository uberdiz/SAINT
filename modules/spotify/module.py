"""Spotify integration module with playback, taste memory, and recommendations."""

from modules.base import BaseModule
from modules.spotify.auth import SpotifyAuth, DEFAULT_REDIRECT_URI
from modules.spotify.client import SpotifyClient
from modules.spotify.tools import SpotifyTools
from core.config import config
from core.events import event_bus, EventType


class SpotifyModule(BaseModule):
    name = "Spotify"
    description = "Spotify playback, personalized memory, listening context, and recommendation tools."

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
            "Playback": True,
            "Search": True,
            "Playlist Access": True,
            "Listening History": True,
            "Taste Memory": True,
            "Recommendations": True,
            "Playlist Aliases": True,
        }
        self.auth = SpotifyAuth()
        self.client = SpotifyClient(self.auth)
        self.tools = SpotifyTools(self.client)

    def enable(self):
        super().enable()
        self.tools.register()

    def disable(self):
        super().disable()

    def is_connected(self):
        try:
            return self.auth.get_access_token() is not None
        except Exception:
            return False

    def connect(self):
        ok = self.auth.authenticate(self.SCOPES)
        event_bus.emit_event(EventType.SPOTIFY_CONNECTED, {"connected": ok})
        return ok

    def disconnect(self):
        self.auth.clear()
        event_bus.emit_event(EventType.SPOTIFY_DISCONNECTED, {})

    def settings(self):
        return {
            "client_id_configured": bool(config.get("spotify.client_id", "")),
            "redirect_uri": config.get("spotify.redirect_uri", DEFAULT_REDIRECT_URI),
            "connected": self.is_connected(),
            "memory_enabled": True,
        }

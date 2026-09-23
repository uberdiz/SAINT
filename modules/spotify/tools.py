"""Spotify capabilities registered in SAINT's shared tool registry.

Tool results are structured: control actions return
``{"success": True, "action": "next"}`` and failures come back as a
``ToolResult`` with a clean, speakable ``error`` plus a machine-readable
``error_code`` — never a raw Python exception string.
"""

import logging

from modules.automation.tools import Tool, PermissionLevel, ToolResult, get_tool_registry
from core.permissions import permission_manager
from modules.spotify.client import SpotifyClient, SpotifyAPIError, friendly_error

logger = logging.getLogger("saint.spotify")


class SpotifyTools:
    def __init__(self, client: SpotifyClient):
        self.client = client

    def register(self):
        registry = get_tool_registry()
        tools = [
            Tool("spotify.current", "Get current Spotify playback state", {}, PermissionLevel.LOW, self.current),
            Tool("spotify.search", "Search Spotify for tracks, artists, albums, or playlists", {"query": "string", "types": "string (optional)"}, PermissionLevel.LOW, self.search),
            Tool("spotify.play", "Start or resume Spotify playback", {"uri": "string (optional)"}, PermissionLevel.LOW, self.play),
            Tool("spotify.pause", "Pause Spotify playback", {}, PermissionLevel.LOW, self.pause),
            Tool("spotify.next", "Skip to the next Spotify item", {}, PermissionLevel.LOW, self.next),
            Tool("spotify.previous", "Skip to the previous Spotify item", {}, PermissionLevel.LOW, self.previous),
            Tool("spotify.volume", "Set Spotify playback volume", {"percent": "int"}, PermissionLevel.LOW, self.volume),
            Tool("spotify.shuffle", "Turn Spotify shuffle on or off", {"state": "bool"}, PermissionLevel.LOW, self.shuffle),
            Tool("spotify.repeat", "Set Spotify repeat mode", {"state": "off|context|track"}, PermissionLevel.LOW, self.repeat),
            Tool("spotify.devices", "List Spotify playback devices", {}, PermissionLevel.LOW, self.devices),
            Tool("spotify.playlists", "List the user's Spotify playlists", {}, PermissionLevel.LOW, self.playlists),
            Tool("spotify.queue", "Add a Spotify item to the playback queue", {"uri": "string"}, PermissionLevel.MEDIUM, self.queue),
            Tool("spotify.add_to_playlist", "Add Spotify tracks to a playlist", {"playlist_id": "string", "uris": "string[]"}, PermissionLevel.MEDIUM, self.add_to_playlist),
        ]
        for tool in tools:
            registry.register(tool)

    def _run(self, name, fn, default_policy=permission_manager.ALLOW, **kwargs):
        policy = permission_manager.get_policy(name, default_policy)
        if policy == permission_manager.DENY:
            return ToolResult(False, error=f"Permission denied for {name}.", error_code="DENIED")
        if policy == permission_manager.CONFIRM:
            return ToolResult(False, error=f"Confirmation required for {name}.", error_code="CONFIRM_REQUIRED")
        try:
            return ToolResult(True, result=fn(**kwargs))
        except SpotifyAPIError as exc:
            # Log the technical detail; hand the AI a clean, speakable message.
            logger.info("spotify.tool.failed name=%s code=%s detail=%s",
                        name, getattr(exc, "code", None), exc)
            return ToolResult(False, error=friendly_error(exc), error_code=getattr(exc, "code", None))
        except Exception as exc:
            logger.exception("spotify.tool.error name=%s", name)
            return ToolResult(False, error="Spotify couldn't complete that request.", error_code="ERROR")

    # --- data-returning ---------------------------------------------------
    def current(self):
        return self.client.playback()

    def search(self, query, types="track,artist,album,playlist"):
        return self.client.search(query, types)

    def devices(self):
        return self.client.devices()

    def playlists(self):
        return self.client.playlists()

    # --- control actions (return structured confirmations) ----------------
    def play(self, uri=None):
        if uri:
            self.client.play(context_uri=uri)
            return {"success": True, "action": "play", "uri": uri}
        self.client.play()
        return {"success": True, "action": "resume"}

    def pause(self):
        self.client.pause()
        return {"success": True, "action": "pause"}

    def next(self):
        self.client.next()
        return {"success": True, "action": "next"}

    def previous(self):
        self.client.previous()
        return {"success": True, "action": "previous"}

    def volume(self, percent):
        self.client.volume(percent)
        return {"success": True, "action": "volume", "percent": int(percent)}

    def shuffle(self, state):
        self.client.shuffle(bool(state))
        return {"success": True, "action": "shuffle", "state": bool(state)}

    def repeat(self, state):
        self.client.repeat(state)
        return {"success": True, "action": "repeat", "state": state}

    def queue(self, uri):
        self.client.queue(uri)
        return {"success": True, "action": "queue", "uri": uri}

    def add_to_playlist(self, playlist_id, uris):
        self.client.add_to_playlist(playlist_id, uris)
        return {"success": True, "action": "add_to_playlist", "playlist_id": playlist_id}

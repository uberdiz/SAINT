"""Spotify capabilities registered in SAINT's shared tool registry."""

from modules.automation.tools import Tool, PermissionLevel, ToolResult, get_tool_registry
from core.permissions import permission_manager
from modules.spotify.client import SpotifyClient, SpotifyAPIError


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
            Tool("spotify.devices", "List Spotify playback devices", {}, PermissionLevel.LOW, self.devices),
            Tool("spotify.queue", "Add a Spotify item to the playback queue", {"uri": "string"}, PermissionLevel.MEDIUM, self.queue),
            Tool("spotify.add_to_playlist", "Add Spotify tracks to a playlist", {"playlist_id": "string", "uris": "string[]"}, PermissionLevel.MEDIUM, self.add_to_playlist),
        ]
        for tool in tools:
            registry.register(tool)

    def _run(self, name, fn, default_policy=permission_manager.ALLOW, **kwargs):
        policy = permission_manager.get_policy(name, default_policy)
        if policy == permission_manager.DENY:
            return ToolResult(False, error=f"Permission denied for {name}.")
        if policy == permission_manager.CONFIRM:
            return ToolResult(False, error=f"Confirmation required for {name}.")
        try:
            return ToolResult(True, result=fn(**kwargs))
        except SpotifyAPIError as exc:
            return ToolResult(False, error=str(exc))
        except Exception as exc:
            return ToolResult(False, error=str(exc))

    def current(self):
        return self.client.playback()

    def search(self, query, types="track,artist,album,playlist"):
        return self.client.search(query, types)

    def play(self, uri=None):
        if uri:
            return self.client.play(context_uri=uri)
        return self.client.play()

    def pause(self):
        return self.client.pause()

    def next(self):
        return self.client.next()

    def previous(self):
        return self.client.previous()

    def volume(self, percent):
        return self.client.volume(percent)

    def devices(self):
        return self.client.devices()

    def queue(self, uri):
        return self.client.queue(uri)

    def add_to_playlist(self, playlist_id, uris):
        return self.client.add_to_playlist(playlist_id, uris)

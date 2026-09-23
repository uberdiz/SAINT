"""Spotify capabilities registered in SAINT's shared tool registry.

Tool results are structured: control actions return
``{"success": True, "action": "next"}`` and failures come back as a
``ToolResult`` with a clean, speakable ``error`` plus a machine-readable
``error_code`` — never a raw Python exception string.
"""

import logging

from datetime import datetime
from modules.automation.tools import Tool, PermissionLevel, ToolResult, get_tool_registry
from core.permissions import permission_manager
from modules.spotify.client import SpotifyClient, SpotifyAPIError, friendly_error
from modules.spotify.memory import SpotifyMemory

logger = logging.getLogger("saint.spotify")


class SpotifyTools:
    def __init__(self, client: SpotifyClient):
        self.client = client
        self.memory = SpotifyMemory()

    def register(self):
        registry = get_tool_registry()
        tools = [
            Tool("spotify.current", "Get current Spotify playback state and remember the current track", {}, PermissionLevel.LOW, self.current),
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
            Tool("spotify.recent", "Get recently played tracks and update SAINT listening memory", {"limit": "int (optional)"}, PermissionLevel.LOW, self.recent),
            Tool("spotify.taste", "Get recent and long-term Spotify taste signals", {}, PermissionLevel.LOW, self.taste),
            Tool("spotify.recommend", "Recommend personalized Spotify tracks using listening history, taste, context, and feedback", {"context": "string (optional)", "limit": "int (optional)"}, PermissionLevel.LOW, self.recommend),
            Tool("spotify.feedback", "Record positive or negative recommendation feedback", {"track_id": "string (optional)", "track_name": "string", "artist": "string", "signal": "float", "reason": "string (optional)"}, PermissionLevel.LOW, self.feedback),
            Tool("spotify.playlist_alias", "Remember a natural alias for a user's playlist", {"alias": "string", "playlist_id": "string", "playlist_name": "string"}, PermissionLevel.LOW, self.playlist_alias),
            Tool("spotify.resolve_playlist", "Resolve a natural playlist alias or playlist name", {"name": "string"}, PermissionLevel.LOW, self.resolve_playlist),
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
        data = self.client.playback()
        item = (data or {}).get("item") if isinstance(data, dict) else None
        if item:
            self.memory.record_listening(item, context_uri=(data or {}).get("context", {}).get("uri", ""))
        return data

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

    def recent(self, limit=20):
        data = self.client.recently_played(limit)
        for entry in (data or {}).get("items", []):
            track = entry.get("track") or {}
            played_at = entry.get("played_at")
            timestamp = datetime.fromisoformat(played_at.replace("Z", "+00:00")).timestamp() if played_at else None
            self.memory.record_listening(track, played_at=timestamp)
        return data

    def taste(self):
        return {
            "recent": self.recent(20),
            "top_tracks": self.client.top_tracks("medium_term", 20),
            "top_artists": self.client.top_artists("medium_term", 20),
            "memory": self.memory.snapshot(),
        }

    def recommend(self, context="", limit=5):
        limit = max(1, min(10, int(limit)))
        recent = self.recent(20)
        top_artists = self.client.top_artists("medium_term", 20).get("items", [])
        top_tracks = self.client.top_tracks("medium_term", 20).get("items", [])
        feedback = self.memory.feedback_scores()
        recent_ids = {x.get("track", {}).get("id") for x in (recent or {}).get("items", [])}

        candidates = {}
        seeds = [a.get("name") for a in top_artists[:6] if a.get("name")]
        seeds += [f'{t.get("name")} {t.get("artists", [{}])[0].get("name", "")}' for t in top_tracks[:8] if t.get("name")]

        for query in seeds:
            data = self.client.search(query, "track", limit=10)
            for track in (data or {}).get("tracks", {}).get("items", []):
                track_id = track.get("id")
                if not track_id:
                    continue
                artist = (track.get("artists") or [{}])[0].get("name", "")
                score = 1.0 + max(-2.0, min(2.0, feedback.get(artist, 0.0) * 0.2))
                if track_id in recent_ids:
                    score -= 1.5
                if context:
                    words = {w for w in context.lower().split() if len(w) > 3}
                    haystack = (track.get("name", "") + " " + artist).lower()
                    score += 0.25 * sum(1 for w in words if w in haystack)
                candidates[track_id] = max(score, candidates.get(track_id, (-999, None))[0]), track

        ranked = sorted(candidates.values(), key=lambda x: x[0], reverse=True)[:limit]
        return {
            "context": context,
            "recommendations": [
                {
                    "id": track.get("id"), "uri": track.get("uri"),
                    "name": track.get("name"),
                    "artists": [a.get("name") for a in track.get("artists", [])],
                    "album": (track.get("album") or {}).get("name"),
                    "score": round(score, 3),
                }
                for score, track in ranked
            ],
        }

    def feedback(self, track_id="", track_name="", artist="", signal=0.0, reason=""):
        track = {"id": track_id, "name": track_name, "artists": [{"name": artist}]}
        self.memory.record_feedback(track, float(signal), reason)
        return {"recorded": True, "signal": float(signal), "artist": artist}

    def playlist_alias(self, alias, playlist_id, playlist_name):
        self.memory.set_playlist_alias(alias, playlist_id, playlist_name)
        return {"alias": alias, "playlist_id": playlist_id, "playlist_name": playlist_name}

    def resolve_playlist(self, name):
        alias = self.memory.resolve_playlist_alias(name)
        if alias:
            return alias
        normalized = name.lower().strip()
        playlists = self.client.playlists().get("items", [])
        exact = next((p for p in playlists if p.get("name", "").lower() == normalized), None)
        partial = next((p for p in playlists if normalized in p.get("name", "").lower()), None)
        chosen = exact or partial
        if chosen:
            return {"id": chosen.get("id"), "uri": chosen.get("uri"), "name": chosen.get("name"),
                    "matched_by": "exact" if exact else "partial"}
        return {"id": None, "name": name, "matches": [p.get("name") for p in playlists[:10]]}


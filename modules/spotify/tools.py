"""Spotify capabilities registered in SAINT's shared tool registry.

Every action resolves a real Spotify entity (track / artist / album /
playlist / genre) through the Web API and actually executes it; results are
structured dicts describing what really happened, so SAINT's spoken
confirmation always reflects the real outcome. Failures raise
``SpotifyAPIError`` with a machine-readable code, which the registry turns
into a clean, speakable error.

Personalization uses SpotifyMemory (listening history observed by the
background poller, direct requests, skips, feedback) plus Spotify's own
top-items endpoints where the account allows.
"""

import difflib
import logging
import random
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.config import config
from core.events import event_bus, EventType
from modules.automation.tools import Tool, PermissionLevel, P, get_tool_registry
from modules.spotify.client import SpotifyClient, SpotifyAPIError, friendly_error
from modules.spotify.memory import SpotifyMemory

logger = logging.getLogger("saint.spotify")

GENRES = {
    "jazz", "blues", "rock", "classic rock", "hard rock", "indie", "indie rock", "indie pop", "pop", "k-pop",
    "kpop", "j-pop", "hip hop", "hip-hop", "rap", "trap", "r&b", "rnb", "soul", "funk", "disco", "house",
    "deep house", "techno", "trance", "edm", "electronic", "dubstep", "drum and bass", "dnb", "lofi", "lo-fi",
    "lo fi", "chill", "chillhop", "ambient", "classical", "piano", "orchestral", "opera", "country", "folk",
    "bluegrass", "metal", "heavy metal", "death metal", "punk", "pop punk", "emo", "grunge", "reggae",
    "reggaeton", "latin", "salsa", "bossa nova", "afrobeats", "afrobeat", "gospel", "christian", "worship",
    "synthwave", "synth pop", "synthpop", "vaporwave", "phonk", "drill", "grime", "garage", "shoegaze",
    "post rock", "alternative", "alt rock", "ska", "swing", "big band", "soundtrack", "movie scores",
    "video game music", "study music", "focus music", "workout", "sleep", "instrumental", "acoustic",
    "christmas", "hyperpop", "city pop", "anime", "musicals", "dance", "80s", "90s", "70s", "60s", "2000s",
}


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)            # (feat. x) [remastered]
    s = re.sub(r"\s-\s.*$", "", s)                     # " - 2011 Remaster"
    s = re.sub(r"[^\w\s&']", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _pnorm(s: str) -> str:
    """Playlist-name normaliser: unlike _norm it keeps "(...)" and " - ..." parts."""
    s = re.sub(r"[^\w\s&']", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _artists(item: Dict[str, Any]) -> str:
    return ", ".join(a.get("name", "") for a in (item or {}).get("artists", []) if a and a.get("name"))


def _items(data: Dict[str, Any], kind: str) -> List[Dict[str, Any]]:
    # Search results can contain null entries (removed items).
    return [x for x in ((data or {}).get(kind + "s") or {}).get("items", []) if x]


class SpotifyTools:
    def __init__(self, client: SpotifyClient):
        self.client = client
        self._memory: Optional[SpotifyMemory] = None
        self._lock = threading.RLock()
        self._last: Optional[Dict[str, Any]] = None          # last observed track (poller)
        self._rec_ids: Dict[str, float] = {}                 # recommended track id -> time played
        self._saint_skip_at = 0.0

    @property
    def memory(self) -> SpotifyMemory:
        if self._memory is None:
            self._memory = SpotifyMemory()
        return self._memory

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #
    def register(self, availability=None):
        registry = get_tool_registry()
        kinds = ["auto", "track", "artist", "album", "playlist", "genre"]
        L, M = PermissionLevel.LOW, PermissionLevel.MEDIUM
        tools = [
            Tool("spotify.current", "What is playing on Spotify right now", {}, L, self.current,
                 parameters={}, llm_exposed=True),
            Tool("spotify.play_query", "Find and play music on Spotify: a song, artist, album, playlist or genre",
                 {"query": "string", "kind": "string"}, L, self.play_query,
                 parameters={"query": P("string", "what to play, e.g. 'Blinding Lights by The Weeknd', 'jazz'"),
                             "kind": P("string", "what the query refers to", required=False, default="auto",
                                       enum=kinds)},
                 llm_exposed=True),
            Tool("spotify.play", "Resume playback, or play a Spotify URI", {"uri": "string (optional)"}, L, self.play,
                 parameters={"uri": P("string", "spotify: URI", required=False)}, llm_exposed=True),
            Tool("spotify.play_liked", "Play the user's Liked Songs", {}, L, self.play_liked,
                 parameters={"shuffle": P("boolean", required=False, default=True)}, llm_exposed=True),
            Tool("spotify.play_recommended", "Play personalized picks: from listening history, similar to "
                 "what's playing, or similar to a named song/album/artist ('something like DAMN by Kendrick Lamar')",
                 {"context": "string (optional)"}, L, self.play_recommended,
                 parameters={"context": P("string", "mood/genre hint", required=False, default=""),
                             "similar_to_current": P("boolean", required=False, default=False),
                             "seed": P("string", "song/album/artist to base it on", required=False, default="")},
                 llm_exposed=True),
            Tool("spotify.seek", "Jump within the current track (seconds from the start, or relative)",
                 {"seconds": "int"}, L, self.seek,
                 parameters={"seconds": P("integer", "target or offset in seconds"),
                             "relative": P("boolean", "true = move forward/back by that many seconds",
                                           required=False, default=False)}, llm_exposed=True),
            Tool("spotify.replay", "Start the current track again from the beginning", {}, L, self.replay,
                 parameters={}, llm_exposed=True),
            Tool("spotify.smart_shuffle", "Turn Spotify Smart Shuffle on or off (via the Spotify app)",
                 {"state": "bool"}, L, self.smart_shuffle, parameters={"state": P("boolean")},
                 llm_exposed=True),
            Tool("spotify.pause", "Pause Spotify playback", {}, L, self.pause, parameters={}, llm_exposed=True),
            Tool("spotify.next", "Skip to the next track", {}, L, self.next, parameters={}, llm_exposed=True),
            Tool("spotify.previous", "Go back to the previous track", {}, L, self.previous,
                 parameters={}, llm_exposed=True),
            Tool("spotify.volume", "Set Spotify volume (0-100)", {"percent": "int"}, L, self.volume,
                 parameters={"percent": P("integer", minimum=0, maximum=100)}, llm_exposed=True),
            Tool("spotify.volume_step", "Turn Spotify volume up or down", {"direction": "string"}, L,
                 self.volume_step, parameters={"direction": P("string", enum=["up", "down"]),
                                               "step": P("integer", required=False, minimum=1, maximum=100)},
                 llm_exposed=True),
            Tool("spotify.shuffle", "Turn shuffle on or off", {"state": "bool"}, L, self.shuffle,
                 parameters={"state": P("boolean")}, llm_exposed=True),
            Tool("spotify.repeat", "Set repeat mode", {"state": "off|context|track"}, L, self.repeat,
                 parameters={"state": P("string", enum=["off", "context", "track"])}, llm_exposed=True),
            Tool("spotify.queue", "Add a song to the play queue", {"query": "string"}, L, self.queue,
                 parameters={"query": P("string", "song to queue, or a spotify:track URI")}, llm_exposed=True),
            Tool("spotify.add_current_to_playlist", "Add the currently playing song to one of the user's playlists",
                 {"playlist": "string"}, M, self.add_current_to_playlist,
                 parameters={"playlist": P("string", "playlist name")}, llm_exposed=True),
            Tool("spotify.add_to_playlist", "Add Spotify track URIs to a playlist",
                 {"playlist_id": "string", "uris": "string[]"}, M, self.add_to_playlist,
                 parameters={"playlist_id": P("string"), "uris": P("array", items="string")}),
            Tool("spotify.search", "Search Spotify", {"query": "string"}, L, self.search,
                 parameters={"query": P("string"),
                             "types": P("string", required=False, default="track,artist,album,playlist")}),
            Tool("spotify.devices", "List Spotify playback devices", {}, L, self.devices, parameters={}),
            Tool("spotify.playlists", "List the user's playlists", {}, L, self.playlists, parameters={},
                 llm_exposed=True),
            Tool("spotify.recent", "Recently played tracks (also updates listening memory)",
                 {"limit": "int (optional)"}, L, self.recent,
                 parameters={"limit": P("integer", required=False, default=20, minimum=1, maximum=50)}),
            Tool("spotify.history", "What the user listened to (today or recently) according to SAINT's memory",
                 {"period": "string"}, L, self.history,
                 parameters={"period": P("string", required=False, default="today", enum=["today", "week", "month"])},
                 llm_exposed=True),
            Tool("spotify.taste", "Summarize the user's music taste from listening memory", {}, L, self.taste,
                 parameters={}, llm_exposed=True),
            Tool("spotify.recommend", "Recommend tracks (without playing) from listening history",
                 {"context": "string (optional)"}, L, self.recommend,
                 parameters={"context": P("string", required=False, default=""),
                             "limit": P("integer", required=False, default=5, minimum=1, maximum=20),
                             "similar_to_current": P("boolean", required=False, default=False),
                             "seed": P("string", "song/album/artist to base it on", required=False, default="")}),
            Tool("spotify.feedback", "Record that the user likes or dislikes the current track",
                 {"signal": "float"}, L, self.feedback,
                 parameters={"signal": P("number", "1 = like, -1 = dislike", minimum=-1, maximum=1),
                             "reason": P("string", required=False, default="")}, llm_exposed=True),
            Tool("spotify.playlist_alias", "Remember a nickname for a playlist",
                 {"alias": "string", "playlist": "string"}, L, self.playlist_alias,
                 parameters={"alias": P("string"), "playlist": P("string", "playlist name")}),
            Tool("spotify.resolve_playlist", "Find one of the user's playlists by name or alias",
                 {"name": "string"}, L, self.resolve_playlist, parameters={"name": P("string")}),
        ]
        for tool in tools:
            tool.category = "spotify"
            tool.availability = availability
            registry.register(tool)

    # ------------------------------------------------------------------ #
    # Devices
    # ------------------------------------------------------------------ #
    def _pick_device(self, devices: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        devices = [d for d in devices if d and not d.get("is_restricted")]
        if not devices:
            return None
        active = next((d for d in devices if d.get("is_active")), None)
        if active:
            return active
        pref = (config.get("spotify.preferred_device", "") or "").strip().lower()
        if pref:
            for d in devices:
                if pref == (d.get("id") or "").lower() or pref in (d.get("name") or "").lower():
                    return d
        return next((d for d in devices if d.get("type") == "Computer"), devices[0])

    def _ensure_device(self) -> str:
        devices = (self.client.devices() or {}).get("devices", [])
        dev = self._pick_device(devices)
        if dev is None and config.get("spotify.auto_device", True):
            # Nothing is running Spotify: open the desktop app and wait for it.
            try:
                from modules.desktop.apps import app_catalog, launch
                entry = app_catalog.resolve("spotify")
                if entry:
                    logger.info("spotify.device.launching_app")
                    launch(entry)
                    for _ in range(24):
                        time.sleep(0.5)
                        devices = (self.client.devices() or {}).get("devices", [])
                        dev = self._pick_device(devices)
                        if dev:
                            break
            except Exception as e:
                logger.warning("spotify.device.launch_failed %s", e)
        if dev is None:
            raise SpotifyAPIError("No Spotify devices available.", status=404, code="NO_DEVICES")
        if not dev.get("is_active"):
            logger.info("spotify.device.transfer name=%r", dev.get("name"))
            self.client.transfer(dev["id"], play=False)
            time.sleep(0.4)
        return dev["id"]

    def _with_device(self, fn):
        """Run a player command; if no device is active, activate one and retry."""
        try:
            return fn(None)
        except SpotifyAPIError as e:
            if e.code not in ("NO_ACTIVE_DEVICE", "NOT_FOUND"):
                raise
            logger.info("spotify.no_active_device — activating a device")
            return fn(self._ensure_device())

    # ------------------------------------------------------------------ #
    # Playback state
    # ------------------------------------------------------------------ #
    _state_cache = (0.0, None)              # (fetched_at, state_dict)
    _STATE_TTL = 2.0                        # seconds

    def _state(self, force: bool = False) -> Dict[str, Any]:
        # Back-to-back reads within one turn (router → verify → reply) don't
        # need a fresh API call. Playback changes emitted by the poller /
        # explicit actions invalidate the cache.
        now = time.time()
        ts, cached = self._state_cache
        if not force and cached is not None and now - ts < self._STATE_TTL:
            return cached
        data = self.client.playback() or {}
        item = data.get("item") or {}
        images = ((item.get("album") or {}).get("images") or [])
        st = self._build_state(data, item, images)
        self._state_cache = (now, st)
        return st

    def invalidate_state_cache(self):
        self._state_cache = (0.0, None)

    def _build_state(self, data: Dict[str, Any], item: Dict[str, Any], images: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "is_playing": bool(data.get("is_playing")),
            "track": item.get("name"),
            "artists": _artists(item),
            "album": (item.get("album") or {}).get("name"),
            "id": item.get("id"),
            "uri": item.get("uri"),
            "progress_ms": data.get("progress_ms") or 0,
            "duration_ms": item.get("duration_ms") or 0,
            "device": (data.get("device") or {}).get("name"),
            "volume": (data.get("device") or {}).get("volume_percent"),
            "shuffle": data.get("shuffle_state"),
            "repeat": data.get("repeat_state"),
            "context_uri": (data.get("context") or {}).get("uri", ""),
            "image": images[-1]["url"] if images else "",
            "item": item or None,
        }

    def current(self):
        st = self._state()
        if st["item"] and config.get("spotify.track_history", True):
            self.memory.record_listening(st["item"], context_uri=st["context_uri"])
        self._publish(st)
        return st

    def _publish(self, st: Dict[str, Any]):
        event_bus.emit_event(EventType.SPOTIFY_PLAYBACK_CHANGED,
                             {k: v for k, v in st.items() if k != "item"})

    def _refresh_soon(self, delay: float = 0.8):
        self.invalidate_state_cache()
        def run():
            time.sleep(delay)
            try:
                self._publish(self._state(force=True))
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------ #
    # Resolution
    # ------------------------------------------------------------------ #
    @staticmethod
    def _split_by(q: str):
        m = re.match(r"^(.*?)\s+by\s+(.+)$", q, re.I)
        return (m.group(1).strip(), m.group(2).strip()) if m else (q, "")

    def _resolve_track(self, q: str) -> Optional[Dict[str, Any]]:
        title, artist = self._split_by(q)
        search = f"track:{title} artist:{artist}" if artist else q
        tracks = _items(self.client.search(search, "track", limit=8), "track")
        if not tracks and artist:
            tracks = _items(self.client.search(f"{title} {artist}", "track", limit=8), "track")
        if not tracks:
            return None

        def score(t):
            s = _sim(title, t.get("name", "")) * 2
            if artist:
                s += max((_sim(artist, a.get("name", "")) for a in t.get("artists", []) if a), default=0) * 1.5
            return s + (t.get("popularity") or 0) / 400

        best = max(tracks, key=score)
        return {"kind": "track", "uri": best["uri"], "name": best["name"], "artist": _artists(best),
                "id": best.get("id"), "item": best}

    def _resolve_artist(self, q: str) -> Optional[Dict[str, Any]]:
        artists = _items(self.client.search(q, "artist", limit=5), "artist")
        if not artists:
            return None
        best = max(artists, key=lambda a: _sim(q, a.get("name", "")) * 3 + (a.get("popularity") or 0) / 100)
        return {"kind": "artist", "uri": best["uri"], "name": best["name"], "artist": best["name"],
                "id": best.get("id"), "context": True}

    def _resolve_album(self, q: str) -> Optional[Dict[str, Any]]:
        title, artist = self._split_by(q)
        search = f"album:{title} artist:{artist}" if artist else q
        albums = _items(self.client.search(search, "album", limit=5), "album")
        if not albums:
            return None
        best = max(albums, key=lambda a: _sim(title, a.get("name", "")) * 2 + (
            max((_sim(artist, x.get("name", "")) for x in a.get("artists", []) if x), default=0) if artist else 0))
        return {"kind": "album", "uri": best["uri"], "name": best["name"], "artist": _artists(best),
                "id": best.get("id"), "context": True}

    def _user_playlists(self) -> List[Dict[str, Any]]:
        # /me/playlists is paged (50 max); users often have more than 50.
        data = self.client.playlists(50) or {}
        items = [p for p in data.get("items", []) if p]
        while data.get("next") and len(items) < 250:
            data = self.client.playlists(50, len(items)) or {}
            page = [p for p in data.get("items", []) if p]
            if not page:
                break
            items += page
        return items

    def _my_user_id(self) -> str:
        if getattr(self, "_me_id", None) is None:
            try:
                self._me_id = (self.client.me() or {}).get("id") or ""
            except Exception:
                self._me_id = ""
        return self._me_id

    def _resolve_playlist(self, q: str, allow_public: bool = True) -> Optional[Dict[str, Any]]:
        q_clean = re.sub(r"\b(my|the|playlist)\b", " ", q, flags=re.I)
        q_clean = re.sub(r"\s+", " ", q_clean).strip()
        # Only edge words stripped, so names like "Songs for the Road" still match exactly.
        q_core = re.sub(r"^(?:my|the)\s+|^playlist\s+|\s+playlist$", "", q.strip(), flags=re.I).strip()
        alias = self.memory.resolve_playlist_alias(q_clean) if q_clean else None
        if alias:
            return {"kind": "playlist", "uri": f"spotify:playlist:{alias['playlist_id']}",
                    "name": alias["playlist_name"], "id": alias["playlist_id"], "context": True, "owned": True}
        mine = self._user_playlists()
        if mine:
            me = self._my_user_id()

            def owned(p):
                return not me or (p.get("owner") or {}).get("id") == me

            chosen = None
            if not q_clean or q_clean.lower() in {"top", "favorite", "favourite", "usual", "main"}:
                chosen = next((p for p in mine if "top" in p.get("name", "").lower()), mine[0])
            else:
                cands = {c for c in (_pnorm(q_core), _pnorm(q_clean)) if c}

                def pscore(p):
                    name = _pnorm(p.get("name", ""))
                    best = 0.0
                    for c in cands:
                        if name == c:
                            sc = 3.0                                  # exact name
                        elif name and (re.search(rf"\b{re.escape(c)}\b", name)
                                       or re.search(rf"\b{re.escape(name)}\b", c)):
                            sc = 1.0 + difflib.SequenceMatcher(None, c, name).ratio()   # whole-word containment
                        else:
                            sc = difflib.SequenceMatcher(None, c, name).ratio()          # fuzzy
                        best = max(best, sc)
                    # Prefer the user's own playlists when names collide.
                    return best + (0.05 if owned(p) else 0.0)

                best = max(mine, key=pscore)
                if pscore(best) >= 0.75:
                    chosen = best
            if chosen:
                return {"kind": "playlist", "uri": chosen["uri"], "name": chosen["name"], "id": chosen["id"],
                        "context": True, "owned": owned(chosen)}
        if not allow_public or not q_clean:
            return None
        pls = _items(self.client.search(q_clean, "playlist", limit=5), "playlist")
        if not pls:
            return None
        best = max(pls, key=lambda p: _sim(q_clean, p.get("name", "")))
        return {"kind": "playlist", "uri": best["uri"], "name": best["name"], "id": best.get("id"),
                "context": True, "owned": False,
                "artist": (best.get("owner") or {}).get("display_name", "")}

    def _resolve_genre(self, genre: str) -> Optional[Dict[str, Any]]:
        g = re.sub(r"\s+music$", "", genre.strip(), flags=re.I)
        pls = _items(self.client.search(g, "playlist", limit=8), "playlist")
        good = [p for p in pls if _norm(g) in _norm(p.get("name", "")) or _norm(g) in _norm(p.get("description", ""))]
        if good:
            p = good[0]
            return {"kind": "genre", "uri": p["uri"], "name": p["name"], "genre": g, "id": p.get("id"),
                    "context": True}
        tracks = _items(self.client.search(f'genre:"{g}"', "track", limit=10), "track")
        if tracks:
            random.shuffle(tracks)
            return {"kind": "genre", "uris": [t["uri"] for t in tracks], "name": f"{g} tracks", "genre": g,
                    "first": f"{tracks[0]['name']} by {_artists(tracks[0])}"}
        return None

    def resolve(self, query: str, kind: str = "auto") -> Optional[Dict[str, Any]]:
        q = re.sub(r"\s+on spotify$", "", query.strip(), flags=re.I).strip(" .!?")
        if kind == "track":
            return self._resolve_track(q)
        if kind == "artist":
            return self._resolve_artist(q)
        if kind == "album":
            return self._resolve_album(q)
        if kind == "playlist":
            return self._resolve_playlist(q)
        if kind == "genre":
            return self._resolve_genre(q)
        # auto: "X by Y" is a track; otherwise compare the best artist,
        # track and album matches and pick the strongest.
        title, artist = self._split_by(q)
        if artist:
            return self._resolve_track(q) or self._resolve_album(q)
        data = self.client.search(q, "track,artist,album", limit=5)
        artists, tracks, albums = _items(data, "artist"), _items(data, "track"), _items(data, "album")
        a = max(artists, key=lambda x: _sim(q, x.get("name", "")), default=None)
        t = max(tracks, key=lambda x: _sim(q, x.get("name", "")), default=None)
        al = max(albums, key=lambda x: _sim(q, x.get("name", "")), default=None)
        sa = _sim(q, a["name"]) if a else 0
        st = _sim(q, t["name"]) if t else 0
        sal = _sim(q, al["name"]) if al else 0
        # Same name for an artist and a track ("Blinding Lights"): the far more
        # popular one is what people mean.
        if a and t and sa >= 0.88 and st >= 0.95 and (t.get("popularity") or 0) > (a.get("popularity") or 0) + 20:
            sa = 0.0
        if a and sa >= 0.88 and sa >= st:
            return {"kind": "artist", "uri": a["uri"], "name": a["name"], "artist": a["name"],
                    "id": a.get("id"), "context": True}
        if _norm(q) in GENRES and sa < 0.95:
            g = self._resolve_genre(q)
            if g:
                return g
        if t and st >= max(0.6, sal):
            return {"kind": "track", "uri": t["uri"], "name": t["name"], "artist": _artists(t),
                    "id": t.get("id"), "item": t}
        if al and sal >= 0.8:
            return {"kind": "album", "uri": al["uri"], "name": al["name"], "artist": _artists(al),
                    "id": al.get("id"), "context": True}
        if tracks:
            t = tracks[0]    # Spotify's own relevance ranking
            return {"kind": "track", "uri": t["uri"], "name": t["name"], "artist": _artists(t),
                    "id": t.get("id"), "item": t}
        if a:
            return {"kind": "artist", "uri": a["uri"], "name": a["name"], "artist": a["name"],
                    "id": a.get("id"), "context": True}
        return None

    # ------------------------------------------------------------------ #
    # Playback actions
    # ------------------------------------------------------------------ #
    def _play_entity(self, ent: Dict[str, Any]):
        if ent.get("uris"):
            self._with_device(lambda d: self.client.play(uris=ent["uris"], device_id=d))
        elif ent.get("context"):
            self._with_device(lambda d: self.client.play(context_uri=ent["uri"], device_id=d))
        else:
            self._with_device(lambda d: self.client.play(uris=[ent["uri"]], device_id=d))

    def play_query(self, query, kind="auto"):
        ent = self.resolve(query, kind)
        if ent is None:
            raise SpotifyAPIError(f"Nothing found for {query}", status=404, code="NO_MATCH")
        self._play_entity(ent)
        if config.get("spotify.track_history", True):
            self.memory.record_request(query, ent["kind"], ent.get("name", ""), ent.get("uri", ""),
                                       ent.get("artist", ""))
        logger.info("spotify.play kind=%s name=%r artist=%r query=%r", ent["kind"], ent.get("name"),
                    ent.get("artist"), query)
        self._refresh_soon()
        return {"success": True, "action": "play", "kind": ent["kind"], "name": ent.get("name"),
                "artist": ent.get("artist", ""), "uri": ent.get("uri"), "genre": ent.get("genre"),
                "first": ent.get("first"), "owned": ent.get("owned")}

    def play(self, uri=None):
        if uri:
            ctx = not uri.startswith("spotify:track:")
            self._play_entity({"uri": uri, "context": ctx})
            self._refresh_soon()
            return {"success": True, "action": "play", "uri": uri}
        self._with_device(lambda d: self.client.play(device_id=d))
        self._refresh_soon()
        return {"success": True, "action": "resume"}

    def play_liked(self, shuffle=True):
        items = (self.client.saved_tracks(50) or {}).get("items", [])
        uris = [i["track"]["uri"] for i in items if i.get("track") and i["track"].get("uri")]
        if not uris:
            raise SpotifyAPIError("No liked songs.", status=404, code="NO_MATCH")
        if shuffle:
            random.shuffle(uris)
        self._with_device(lambda d: self.client.play(uris=uris, device_id=d))
        self.memory.record_request("liked songs", "liked", "Liked Songs")
        self._refresh_soon()
        return {"success": True, "action": "play", "kind": "liked", "count": len(uris)}

    def pause(self):
        self._with_device(lambda d: self.client.pause(device_id=d))
        self._refresh_soon(0.5)
        return {"success": True, "action": "pause"}

    def next(self):
        try:
            st = self._state()
            if st["item"] and config.get("spotify.track_history", True):
                dur = st["duration_ms"] or 1
                if st["progress_ms"] < 0.8 * dur:
                    self.memory.record_skip(st["item"], st["progress_ms"])
                    self._note_rec_outcome(st["id"], st["progress_ms"], dur, skipped=True)
        except SpotifyAPIError:
            pass
        self._saint_skip_at = time.time()
        self._with_device(lambda d: self.client.next(device_id=d))
        self._refresh_soon()
        return {"success": True, "action": "next"}

    def previous(self):
        self._with_device(lambda d: self.client.previous(device_id=d))
        self._refresh_soon()
        return {"success": True, "action": "previous"}

    def volume(self, percent):
        percent = max(0, min(100, int(percent)))
        self._with_device(lambda d: self.client.volume(percent, device_id=d))
        return {"success": True, "action": "volume", "percent": percent}

    def volume_step(self, direction, step=None):
        step = int(step or config.get("spotify.volume_step", 15))
        st = self._state()
        cur = st.get("volume")
        if cur is None:
            raise SpotifyAPIError("This device doesn't report its volume.", status=403, code="VOLUME_UNSUPPORTED")
        new = max(0, min(100, int(cur) + (step if direction == "up" else -step)))
        self.client.volume(new)
        return {"success": True, "action": "volume", "percent": new, "previous": cur}

    def shuffle(self, state):
        self._with_device(lambda d: self.client.shuffle(bool(state), device_id=d))
        return {"success": True, "action": "shuffle", "state": bool(state)}

    def repeat(self, state):
        self._with_device(lambda d: self.client.repeat(state, device_id=d))
        return {"success": True, "action": "repeat", "state": state}

    def queue(self, query):
        if query.startswith("spotify:track:"):
            ent = {"uri": query, "name": query, "artist": ""}
        else:
            ent = self._resolve_track(query)
            if not ent:
                raise SpotifyAPIError(f"Nothing found for {query}", status=404, code="NO_MATCH")
        self._with_device(lambda d: self.client.queue(ent["uri"], device_id=d))
        return {"success": True, "action": "queue", "name": ent["name"], "artist": ent.get("artist", ""),
                "uri": ent["uri"]}

    def add_to_playlist(self, playlist_id, uris):
        self.client.add_to_playlist(playlist_id, uris)
        return {"success": True, "action": "add_to_playlist", "playlist_id": playlist_id, "added": len(uris)}

    def add_current_to_playlist(self, playlist):
        st = self._state()
        if not st["uri"]:
            raise SpotifyAPIError("Nothing is playing.", status=404, code="NO_PLAYBACK")
        ent = self._resolve_playlist(playlist, allow_public=False)
        if not ent:
            names = ", ".join(p.get("name", "") for p in self._user_playlists()[:6])
            msg = f"I couldn't find a playlist called {playlist}."
            if names:
                msg += f" Your playlists include {names}."
            raise SpotifyAPIError(msg, status=404, code="NO_PLAYLIST")
        self.client.add_to_playlist(ent["id"], [st["uri"]])
        if config.get("spotify.track_history", True):
            self.memory.record_feedback(st["item"], 0.5, f"added to playlist {ent['name']}")
        return {"success": True, "action": "add_to_playlist", "track": st["track"], "artist": st["artists"],
                "playlist": ent["name"]}

    # ------------------------------------------------------------------ #
    # Data
    # ------------------------------------------------------------------ #
    def search(self, query, types="track,artist,album,playlist"):
        return self.client.search(query, types)

    def devices(self):
        return self.client.devices()

    def playlists(self):
        return {"playlists": [{"name": p.get("name"), "id": p.get("id"),
                               "tracks": (p.get("tracks") or p.get("items") or {}).get("total")}
                              for p in self._user_playlists()]}

    def recent(self, limit=20):
        data = self.client.recently_played(limit)
        for entry in (data or {}).get("items", []):
            track = entry.get("track") or {}
            played_at = entry.get("played_at")
            ts = datetime.fromisoformat(played_at.replace("Z", "+00:00")).timestamp() if played_at else None
            self.memory.record_listening(track, played_at=ts, source="recently_played")
        return data

    def history(self, period="today"):
        if period == "today":
            rows = self.memory.today()
            days = 1
        else:
            days = 7 if period == "week" else 30
            rows = self.memory.recent(time.time() - days * 86400, 300)
        return {"period": period, "count": len(rows),
                "tracks": [{"track": r["track_name"], "artist": r["artist"],
                            "played_at": r["played_at"]} for r in rows[:50]],
                "top_artists": self.memory.top_artists(days, 5)}

    def taste(self):
        snap = self.memory.snapshot()
        spotify_top = []
        try:
            spotify_top = [a.get("name") for a in (self.client.top_artists("medium_term", 10) or {}).get("items", [])]
        except SpotifyAPIError:
            pass
        return {"top_artists": [a["artist"] for a in snap["top_artists"][:8]],
                "top_tracks": [f"{t['track_name']} by {t['artist']}" for t in snap["top_tracks"][:8]],
                "top_genres": [g["genre"] for g in snap["top_genres"][:6]],
                "spotify_top_artists": spotify_top,
                "skips_7d": snap["skips_7d"],
                "recommendations": snap["recommendations"],
                "plays_today": len(snap["today"])}

    # ------------------------------------------------------------------ #
    # Personalization
    # ------------------------------------------------------------------ #
    def _seed_artists(self) -> Dict[str, float]:
        seeds: Dict[str, float] = {}
        for a in self.memory.top_artists(30, 15):
            seeds[a["artist"]] = seeds.get(a["artist"], 0) + a["plays"] - 2 * a.get("skips", 0)
        try:
            for i, a in enumerate((self.client.top_artists("short_term", 10) or {}).get("items", [])):
                seeds[a["name"]] = seeds.get(a["name"], 0) + max(1.0, 6 - i * 0.5)
        except SpotifyAPIError:
            pass
        for artist, score in self.memory.feedback_scores().items():
            if artist:
                seeds[artist] = seeds.get(artist, 0) + 2 * score
        return {k: v for k, v in seeds.items() if v > 0}

    def _seed_from(self, seed: str):
        """(main_artist {id,name}, exclude_track_id, label) for 'something like <seed>'."""
        title, by = self._split_by(seed)
        ent = None
        if by:
            cands = [e for e in (self._resolve_album(seed), self._resolve_track(seed)) if e]
            if cands:
                ent = max(cands, key=lambda e: (_sim(title, e.get("name", "")), e["kind"] == "album"))
        ent = ent or self.resolve(seed, "auto")
        if ent is None:
            raise SpotifyAPIError(f"I couldn't find {seed} on Spotify.", status=404, code="NO_MATCH")
        if ent["kind"] == "artist":
            return {"id": ent.get("id"), "name": ent["name"]}, None, ent["name"]
        if ent.get("item"):
            main = (ent["item"].get("artists") or [{}])[0]
            return main, ent.get("id"), f"{ent['name']} by {ent.get('artist', '')}".strip(" by")
        artist = ent.get("artist", "")
        found = _items(self.client.search(artist, "artist", limit=1), "artist") if artist else []
        main = {"id": found[0]["id"], "name": found[0]["name"]} if found else {"id": None, "name": artist}
        return main, None, f"{ent['name']} by {artist}".strip(" by")

    def recommend(self, context="", limit=5, similar_to_current=False, seed=""):
        limit = max(1, min(20, int(limit)))
        recent_ids = {r["track_id"] for r in self.memory.recent(time.time() - 3 * 3600, 200)}
        skipped = self.memory.skipped_track_ids(30)
        candidates: Dict[str, tuple] = {}
        basis = ""

        def add(track, score, why):
            tid = track.get("id")
            if not tid or tid in recent_ids or tid in skipped:
                return
            if tid not in candidates or candidates[tid][0] < score:
                candidates[tid] = (score, track, why)

        if similar_to_current or seed:
            if seed:
                main, exclude, label = self._seed_from(seed)
                if exclude:
                    recent_ids.add(exclude)
                basis = f"similar to {label}"
            else:
                st = self._state()
                if not st["item"]:
                    raise SpotifyAPIError("Nothing is playing to base that on.", status=404, code="NO_PLAYBACK")
                recent_ids.add(st["id"])
                main = (st["item"].get("artists") or [{}])[0]
                basis = f"similar to {st['track']} by {st['artists']}"
            genres = self._genres_for(main.get("id"), main.get("name", ""))
            gset = set(genres)
            # 1. The artist's own catalogue (the most reliable "like X").
            for t in _items(self.client.search(f'artist:"{main.get("name", "")}"', "track", limit=10), "track"):
                add(t, 2.0 + random.random() * 0.4, f"more {main.get('name')}")
            # 2. Artists featured alongside them.
            featured = {}
            for t in list(v[1] for v in candidates.values()):
                for a in (t.get("artists") or [])[1:]:
                    if a and a.get("id") != main.get("id") and a.get("name"):
                        featured[a["name"]] = a
            for name in list(featured)[:3]:
                for t in _items(self.client.search(f'artist:"{name}"', "track", limit=5), "track"):
                    add(t, 1.9 + random.random() * 0.4, f"features with {main.get('name')}")
            # 3. Artists the user already plays who share a genre (personal + similar).
            if gset:
                for name, weight in sorted(self._seed_artists().items(), key=lambda kv: kv[1], reverse=True)[:12]:
                    if name == main.get("name"):
                        continue
                    found = _items(self.client.search(name, "artist", limit=1), "artist")
                    if not found or not gset & set(self._genres_for(found[0]["id"], found[0]["name"])):
                        continue
                    for t in _items(self.client.search(f'artist:"{name}"', "track", limit=5), "track"):
                        add(t, 2.2 + random.random() * 0.4, f"you like {name}, similar style")
            # 4. Genre search only as a weak fallback (it surfaces obscure uploads).
            if len(candidates) < 8:
                for g in genres[:2]:
                    for t in _items(self.client.search(f'genre:"{g}"', "track", limit=10), "track"):
                        if all((a or {}).get("id") != main.get("id") for a in t.get("artists", [])) \
                                and (t.get("popularity") or 0) >= 40:
                            add(t, 1.0 + random.random() * 0.4, g)
            if not genres:
                logger.info("spotify.recommend no genre data for %s — using the artist and collaborators", main.get("name"))
        else:
            seeds = self._seed_artists()
            if not seeds:
                return {"recommendations": [], "reason": "no_history", "context": context}
            top = sorted(seeds.items(), key=lambda kv: kv[1], reverse=True)[:6]
            basis = "your listening history (" + ", ".join(a for a, _ in top[:3]) + ")"
            ctx_words = {w for w in re.findall(r"[a-z]+", (context or "").lower()) if len(w) > 3}
            for name, weight in top:
                for t in _items(self.client.search(f'artist:"{name}"', "track", limit=10), "track"):
                    s = weight / max(1.0, top[0][1]) * 2 + random.random() * 0.6
                    hay = (t.get("name", "") + " " + (t.get("album") or {}).get("name", "")).lower()
                    s += 0.3 * sum(1 for w in ctx_words if w in hay)
                    add(t, s, f"you listen to {name}")
            if context and _norm(context) in GENRES:
                for t in _items(self.client.search(f'genre:"{_norm(context)}"', "track", limit=10), "track"):
                    add(t, 1.5 + random.random(), context)
        ranked = sorted(candidates.values(), key=lambda x: x[0], reverse=True)
        per_artist: Dict[str, int] = {}
        out, names = [], set()
        for score, t, why in ranked:      # variety: at most 2 tracks per artist, no re-releases
            a = _artists(t)
            key = (_norm(t.get("name", "")), a.split(",")[0])
            if key in names:
                continue
            names.add(key)
            if per_artist.get(a, 0) >= 2:
                continue
            per_artist[a] = per_artist.get(a, 0) + 1
            out.append({"id": t.get("id"), "uri": t.get("uri"), "name": t.get("name"), "artists": a,
                        "album": (t.get("album") or {}).get("name"), "why": why, "score": round(score, 2)})
            if len(out) >= limit:
                break
        return {"recommendations": out, "basis": basis, "context": context}

    def play_recommended(self, context="", similar_to_current=False, seed=""):
        rec = self.recommend(context=context, limit=10, similar_to_current=similar_to_current, seed=seed)
        recs = rec["recommendations"]
        if not recs:
            if rec.get("reason") == "no_history":
                raise SpotifyAPIError("Not enough listening history yet.", status=404, code="NO_HISTORY")
            raise SpotifyAPIError("No recommendations found.", status=404, code="NO_MATCH")
        uris = [r["uri"] for r in recs if r.get("uri")]
        self._with_device(lambda d: self.client.play(uris=uris, device_id=d))
        now = time.time()
        with self._lock:
            for r in recs:
                self._rec_ids[r["id"]] = now
        self.memory.record_request(context or (f"like {seed}" if seed else
                                               "similar" if similar_to_current else "something I like"),
                                   "recommendation", recs[0]["name"], recs[0]["uri"], recs[0]["artists"])
        self._refresh_soon()
        return {"success": True, "action": "play", "kind": "recommendation", "name": recs[0]["name"],
                "artist": recs[0]["artists"], "count": len(uris), "basis": rec.get("basis", "")}

    def _genres_for(self, artist_id: Optional[str], name: str) -> List[str]:
        if not artist_id:
            return []
        cached = self.memory.artist_genres(artist_id)
        if cached is not None:
            return cached
        try:
            genres = (self.client.artist(artist_id) or {}).get("genres") or []
        except SpotifyAPIError:
            genres = []
        self.memory.cache_artist_genres(artist_id, name, genres)
        return genres

    def _note_rec_outcome(self, track_id, progress_ms, duration_ms, skipped: bool):
        with self._lock:
            started = self._rec_ids.pop(track_id, None) if track_id else None
        if started is None:
            return
        listened = progress_ms / max(1, duration_ms)
        if skipped and listened < 0.35:
            self.memory.record_feedback({"id": track_id, "name": "", "artists": []}, -0.5,
                                        "recommendation rejected (skipped)")
        elif listened >= 0.6:
            self.memory.record_feedback({"id": track_id, "name": "", "artists": []}, 0.5,
                                        "recommendation accepted (listened)")

    def seek(self, seconds, relative=False):
        st = self._state()
        if not st["item"]:
            raise SpotifyAPIError("Nothing is playing.", status=404, code="NO_PLAYBACK")
        pos = int(seconds) * 1000
        if relative:
            pos += int(st.get("progress_ms") or 0)
        dur = int((st["item"] or {}).get("duration_ms") or 0)
        pos = max(0, min(pos, dur - 1000 if dur else pos))
        self._with_device(lambda d: self.client.seek(pos, device_id=d))
        self._refresh_soon(0.5)
        return {"position_s": pos // 1000, "track": st["track"]}

    def replay(self):
        r = self.seek(0)
        return {"track": r["track"]}

    def smart_shuffle(self, state=True):
        """Smart Shuffle isn't in Spotify's Web API. The Spotify desktop app
        exposes it on its shuffle button (Off -> Shuffle -> Smart Shuffle), so
        drive that button through UI Automation and verify its label.
        Falls back to normal shuffle when the app isn't open."""
        def mode(lbl):
            # Label names the NEXT action: "Enable shuffle" (off) -> "Enable Smart
            # Shuffle" (shuffle on) -> "Disable shuffle" (smart shuffle on).
            if lbl.startswith("enable smart"):
                return "shuffle"
            if lbl.startswith("disable"):
                return "smart"
            return "off"

        label = self._spotify_shuffle_label()
        if label is None:
            self.shuffle(bool(state))
            return {"smart": False, "shuffle": bool(state), "fallback": True}
        want = "smart" if state else "off"
        for _ in range(3):
            if mode(label) == want:
                break
            label = self._spotify_shuffle_label(click=True) or ""
        got = mode(label)
        self._refresh_soon()
        return {"smart": got == "smart", "shuffle": got != "off", "fallback": False, "verified": got == want}

    def _spotify_shuffle_label(self, click: bool = False) -> Optional[str]:
        """Accessible name of the Spotify app's shuffle button (lower case),
        optionally clicking it first. None if the app/button isn't available."""
        try:
            from modules.desktop.controller import desktop
            from modules.desktop import uia
            wins = desktop.app_windows("spotify")
            if not wins:
                return None
            auto = uia._auto()
            top = auto.ControlFromHandle(wins[0].hwnd)
            btn = None
            for c in uia._walk(top, limit=4000):
                if uia._safe_type(c) == "ButtonControl" and "shuffle" in (c.Name or "").lower():
                    btn = c
                    break
            if btn is None:
                return None
            if click:
                try:
                    btn.GetInvokePattern().Invoke()
                except Exception:
                    btn.Click(simulateMove=False)
                time.sleep(0.6)
                return self._spotify_shuffle_label(click=False)
            return (btn.Name or "").lower()
        except Exception as e:
            logger.debug("spotify.smart_shuffle_uia_failed %s", e)
            return None

    def feedback(self, signal, reason=""):
        st = self._state()
        if not st["item"]:
            raise SpotifyAPIError("Nothing is playing.", status=404, code="NO_PLAYBACK")
        self.memory.record_feedback(st["item"], float(signal), reason or "explicit")
        return {"recorded": True, "signal": float(signal), "track": st["track"], "artist": st["artists"]}

    def playlist_alias(self, alias, playlist):
        ent = self._resolve_playlist(playlist, allow_public=False)
        if not ent:
            raise SpotifyAPIError(f"No playlist named {playlist}.", status=404, code="NO_PLAYLIST")
        self.memory.set_playlist_alias(alias, ent["id"], ent["name"])
        return {"alias": alias, "playlist": ent["name"]}

    def resolve_playlist(self, name):
        ent = self._resolve_playlist(name, allow_public=False)
        if not ent:
            return {"id": None, "name": name, "matches": [p.get("name") for p in self._user_playlists()[:10]]}
        return ent

    # ------------------------------------------------------------------ #
    # Background poller (listening memory + live UI state)
    # ------------------------------------------------------------------ #
    def poll_once(self):
        # Poller must see fresh state or it can't detect skips.
        st = self._state(force=True)
        self._publish(st)
        if not config.get("spotify.track_history", True):
            return st
        with self._lock:
            last = self._last
            if st["id"] and (last is None or last.get("id") != st["id"]):
                if last and last.get("id"):
                    elapsed = (time.time() - last["seen_at"]) * 1000 + last["progress_ms"]
                    frac = elapsed / max(1, last["duration_ms"])
                    user_skip = frac < 0.5 and time.time() - self._saint_skip_at > 5
                    if user_skip and last.get("item"):
                        self.memory.record_skip(last["item"], int(elapsed))
                    self._note_rec_outcome(last["id"], min(elapsed, last["duration_ms"]), last["duration_ms"],
                                           skipped=user_skip)
                self.memory.record_listening(st["item"], context_uri=st["context_uri"])
            if st["id"]:
                self._last = {"id": st["id"], "item": st["item"], "progress_ms": st["progress_ms"],
                              "duration_ms": st["duration_ms"] or 1, "seen_at": time.time()}
        # Slowly fill in genre data for artists we've seen (one lookup per poll).
        for artist_id in self.memory.uncached_artist_ids(1):
            self._genres_for(artist_id, "")
        return st


def speakable_error(exc) -> str:
    return friendly_error(exc)

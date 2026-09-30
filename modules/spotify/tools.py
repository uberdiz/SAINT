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
import math
import random
import re
import threading
import time
import unicodedata
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.config import config
from core.events import event_bus, EventType
from modules.automation.tools import Tool, PermissionLevel, P, get_tool_registry
from modules.spotify.client import SpotifyClient, SpotifyAPIError, friendly_error
from modules.spotify.memory import SpotifyMemory

logger = logging.getLogger("saint.spotify")

# A skip is a track changed before this share of it played (voice and app alike).
SKIP_FRACTION = 0.5
# Auto-queue ("radio"): songs are added to Spotify's queue this many at a time,
# and topped up when only this many of them are left to play.
RADIO_BATCH = 5
RADIO_LOW_WATER = 1

# DJ mode: mood -> genre words matched against the artists' Spotify genres.
MOODS = {
    "energetic": ("trap", "rage", "drill", "hip hop", "edm", "dance", "rock", "punk", "hyperpop", "phonk"),
    "darker": ("horrorcore", "dark", "phonk", "emo", "drill", "industrial", "metal", "underground", "goth"),
    "chill": ("lo-fi", "lofi", "chill", "r&b", "soul", "indie", "bedroom", "acoustic", "ambient", "jazz"),
    "sad": ("sad", "emo", "indie", "singer-songwriter", "acoustic", "slowcore"),
    "happy": ("pop", "dance", "funk", "disco", "afrobeats", "house"),
    "focus": ("lo-fi", "lofi", "ambient", "instrumental", "jazz", "classical", "chill", "study", "piano"),
    "party": ("dance", "pop", "reggaeton", "trap", "edm", "house", "club", "hip hop", "afrobeats", "latin"),
    "romantic": ("r&b", "soul", "love", "bedroom", "neo soul", "bolero", "ballad"),
    "angry": ("rage", "metal", "punk", "drill", "hardcore", "industrial", "horrorcore", "phonk"),
}

# Languages / regions people ask for as if they were genres ("play Spanish music").
LANGUAGE_GENRES = {
    "spanish", "latin", "latino", "french", "german", "italian", "portuguese", "brazilian", "japanese", "korean",
    "chinese", "mandarin", "cantonese", "arabic", "turkish", "russian", "hindi", "bollywood", "punjabi", "tamil",
    "african", "afro", "mexican", "regional mexican", "dominican", "puerto rican", "colombian", "cuban",
    "caribbean", "jamaican", "irish", "greek", "polish", "dutch", "swedish", "filipino", "vietnamese", "thai",
    "indonesian", "persian", "hebrew", "ukrainian",
}

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
    "latin pop", "latin trap", "trap latino", "corridos", "corridos tumbados", "dembow", "bachata", "merengue",
    "cumbia", "banda", "mariachi", "flamenco", "samba", "funk carioca", "baile funk", "amapiano", "afropop",
    "dancehall", "soca", "zouk", "kizomba", "rock en espanol", "rock en español", "urbano", "urbano latino",
    "j-rock", "c-pop", "mandopop", "cantopop", "bhangra", "chanson", "schlager", "neo soul", "motown",
    "old school", "old school hip hop", "boom bap", "cloud rap", "emo rap", "rage", "plugg", "jersey club",
} | LANGUAGE_GENRES
# "Spanish music", "latin hits", "chill vibes": a description of music, not a title.
_DESCRIPTOR_TAIL = re.compile(r"^(?P<what>.+?)\s+(?:music|songs|tracks|hits|tunes|vibes|jams|bangers|mix|radio|"
                              r"playlist|stuff)$", re.I)


def _fold(s: str) -> str:
    """No accents: "Tití Me Preguntó" -> "Titi Me Pregunto" (speech-to-text never has them)."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def _norm(s: str) -> str:
    s = _fold(s or "").lower()
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


def _title_score(heard: str, name: str) -> float:
    """How well a heard title matches a real one, spelling *or* sound:
    "TT May Praguntha" is "Tití Me Preguntó" to the ear."""
    from modules.desktop.window_match import sounds_like
    s = _sim(heard, name)
    a, b = _norm(heard), _norm(name)
    if a and b and len(a) >= 3:
        s = max(s, sounds_like(a, b) * 0.95)
        if len(a) >= 4 and (re.search(rf"\b{re.escape(a)}\b", b) or re.search(rf"\b{re.escape(b)}\b", a)):
            s = max(s, 0.85)
    return s


def _pnorm(s: str) -> str:
    """Playlist-name normaliser: unlike _norm it keeps "(...)" and " - ..." parts."""
    s = re.sub(r"[^\w\s&']", " ", _fold(s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _playlist_size(p: Dict[str, Any]) -> Optional[int]:
    total = ((p or {}).get("tracks") or (p or {}).get("items") or {})
    total = total.get("total") if isinstance(total, dict) else None
    return int(total) if isinstance(total, (int, float)) else None


def _artists(item: Dict[str, Any]) -> str:
    return ", ".join(a.get("name", "") for a in (item or {}).get("artists", []) if a and a.get("name"))


def _items(data: Dict[str, Any], kind: str) -> List[Dict[str, Any]]:
    # Search results can contain null entries (removed items).
    return [x for x in ((data or {}).get(kind + "s") or {}).get("items", []) if x]


def _spread_artists(recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the ranking but avoid the same artist twice in a row."""
    out, rest = [], list(recs)
    while rest:
        last = out[-1]["artists"].split(",")[0] if out else None
        i = next((k for k, r in enumerate(rest) if r["artists"].split(",")[0] != last), 0)
        out.append(rest.pop(i))
    return out


class SpotifyTools:
    def __init__(self, client: SpotifyClient):
        self.client = client
        self._memory: Optional[SpotifyMemory] = None
        self._lock = threading.RLock()
        self._last: Optional[Dict[str, Any]] = None          # last observed track (poller)
        self._rec_ids: Dict[str, float] = {}                 # recommended track id -> time played
        self._saint_skip_at = 0.0
        self._skip_handled_id = None     # a skip SAINT already recorded (voice / mini player)
        self._backfilled = False
        self._radio: Optional[Dict[str, Any]] = None       # auto-queue state (see _radio_start)
        self._radio_gen = 0
        # Songs an earlier auto-queue put in Spotify's queue that never played: the
        # Web API can't remove them, so when one comes up after the user moved on it is skipped.
        self._radio_orphans: Dict[str, float] = {}

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
                                       enum=kinds),
                             "own_only": P("boolean", "only the user's own playlists ('my X playlist')",
                                           required=False, default=False)},
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
                             "seed": P("string", "song/album/artist to base it on", required=False, default=""),
                             "mood": P("string", "DJ mood ('auto' = read it from recent listening)",
                                       required=False, default="", enum=[""] + list(MOODS) + ["auto"]),
                             "novel": P("boolean", "only songs the user hasn't heard", required=False,
                                        default=False),
                             "seed_last": P("boolean", "similar to the previous song", required=False,
                                            default=False)},
                 llm_exposed=True),
            Tool("spotify.queue_similar", "Add songs like the one playing to the queue without interrupting it "
                 "('queue more songs like this'); SAINT keeps the queue topped up", {"count": "int"}, L,
                 self.queue_similar,
                 parameters={"count": P("integer", "how many to add now", required=False, default=RADIO_BATCH,
                                        minimum=1, maximum=10)}, llm_exposed=True),
            Tool("spotify.stop_autoqueue", "Stop adding songs to the queue automatically", {}, L,
                 self.stop_autoqueue, parameters={}),
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
            Tool("spotify.next", "Skip to the next track", {}, L, self.next,
                 parameters={"source": P("string", "who skipped", required=False, default="voice",
                                         enum=["voice", "ui"])}, llm_exposed=True),
            Tool("spotify.ban_artist", "Stop recommending an artist ('no more of this artist')",
                 {"name": "string (optional)"}, L, self.ban_artist,
                 parameters={"name": P("string", "artist; blank = the one playing", required=False, default="")}),
            Tool("spotify.unban_artist", "Allow a banned artist in recommendations again",
                 {"name": "string"}, L, self.unban_artist, parameters={"name": P("string")}),
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
                 {"context": "string (optional)"}, L, self.recommend_tool,
                 parameters={"context": P("string", required=False, default=""),
                             "limit": P("integer", required=False, default=5, minimum=1, maximum=20),
                             "similar_to_current": P("boolean", required=False, default=False),
                             "seed": P("string", "song/album/artist to base it on", required=False, default="")}),
            Tool("spotify.feedback", "Record that the user likes or dislikes the current track",
                 {"signal": "float"}, L, self.feedback,
                 parameters={"signal": P("number", "1 = like, -1 = dislike", minimum=-1, maximum=1),
                             "reason": P("string", required=False, default="")}),
            # Bans and feedback change what SAINT plays for good: only from the
            # user's own words (the router), never guessed by the model.
            Tool("spotify.ban_playlist", "Never play the playlist that's playing again", {}, L, self.ban_playlist,
                 parameters={}),
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
            "image_large": images[0]["url"] if images else "",      # Spotify lists 640px first
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
        if st.get("track"):
            # Lyrics for the mini player and for telling the song apart from the user.
            try:
                from modules.spotify.lyrics import lyrics_service
                lyrics_service.note_playing(
                    st["track"], st.get("artists") or "", st.get("album") or "", st.get("duration_ms") or 0,
                    st.get("progress_ms") or 0, bool(st.get("is_playing")),
                    prefetch=bool(config.get("widgets.lyrics", False) or config.get("voice.lyrics_filter", True)))
            except Exception as e:
                logger.debug("spotify.lyrics_note_failed %s", e)

    def _refresh_soon(self, delay: float = 0.8):
        self.invalidate_state_cache()
        # SAINT changed playback: the next track change isn't the user skipping.
        self._saint_skip_at = time.time()
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
        # Speech-to-text writes "by" as "bye" / "buy" ("Play TT May Praguntha, Bye Bad Bunny!").
        m = re.match(r"^(.*?)(?:,\s*|\s+)(?:by|bye|buy)\s+(.+)$", q, re.I)
        if not m:
            return (q, "")
        title = re.sub(r"^(?:the\s+)?(?:song|track)\s+", "", m.group(1).strip(" ,"), flags=re.I)
        return (title, m.group(2).strip(" ,.!?"))

    def _resolve_track(self, q: str) -> Optional[Dict[str, Any]]:
        title, artist = self._split_by(q)
        tracks: Dict[str, Dict[str, Any]] = {}

        def collect(items):
            for t in items:
                if t and t.get("id") and t.get("uri"):
                    tracks.setdefault(t["id"], t)

        if artist:
            collect(_items(self.client.search(f"track:{title} artist:{artist}", "track", limit=8), "track"))
            collect(_items(self.client.search(f"{title} {artist}", "track", limit=8), "track"))
        else:
            collect(_items(self.client.search(q, "track", limit=8), "track"))

        def artist_score(t):
            if not artist:
                return 0.0
            return max((max(_sim(artist, a.get("name", "")), _title_score(artist, a.get("name", "")))
                        for a in t.get("artists", []) if a), default=0.0)

        def score(t):
            return _title_score(title, t.get("name", "")) * 2 + artist_score(t) * 1.5 + \
                (t.get("popularity") or 0) / 400

        best = max(tracks.values(), key=score, default=None)
        if artist and (best is None or _title_score(title, best.get("name", "")) < 0.8):
            # A misheard title by a known artist: look through that artist's own songs
            # and pick the one it sounds like, instead of some other artist's track.
            found = self._artist_named(artist)
            if found:
                try:
                    collect(self.client.artist_top_tracks(found["id"]))
                except SpotifyAPIError:
                    pass
                for offset in (0, 10, 20):
                    if best is not None and _title_score(title, best.get("name", "")) >= 0.8:
                        break
                    collect(_items(self.client.search(f'artist:"{found["name"]}"', "track", limit=10,
                                                      offset=offset), "track"))
                    best = max(tracks.values(), key=score, default=None)
                own = [t for t in tracks.values() if any((a or {}).get("id") == found["id"] for a in t.get("artists", []))]
                if own:
                    best_own = max(own, key=score)
                    if best is None or artist_score(best) < 0.7 or \
                            _title_score(title, best_own["name"]) >= _title_score(title, best.get("name", "")):
                        best = best_own
        if best is None:
            return None
        logger.info("spotify.resolve_track heard=%r artist=%r -> %r by %r (title %.2f)", title, artist,
                    best.get("name"), _artists(best), _title_score(title, best.get("name", "")))
        return {"kind": "track", "uri": best["uri"], "name": best["name"], "artist": _artists(best),
                "id": best.get("id"), "item": best, "title_score": _title_score(title, best.get("name", "")),
                "artist_score": artist_score(best) if artist else 1.0}

    def _artist_named_exactly(self, name: str) -> Optional[Dict[str, Any]]:
        artists = _items(self.client.search(name, "artist", limit=3), "artist")
        return next((a for a in artists if _sim(name, a.get("name", "")) >= 0.92), None)

    def _artist_named(self, name: str) -> Optional[Dict[str, Any]]:
        """The artist a (possibly misheard) name refers to, if one clearly matches."""
        artists = _items(self.client.search(name, "artist", limit=5), "artist")
        if not artists:
            return None
        best = max(artists, key=lambda a: max(_sim(name, a.get("name", "")), _title_score(name, a.get("name", "")))
                   * 3 + (a.get("popularity") or 0) / 100)
        return best if max(_sim(name, best.get("name", "")), _title_score(name, best.get("name", ""))) >= 0.7 else None

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
        # "a playlist called Dominican Dembow", "a Spanish playlist", "some chill playlist"
        q = re.sub(r"^(?:(?:a|an|some|any|one)\s+)?(?:(?:playlist|one)\s+)?(?:called|named|titled)\s+", "",
                   q.strip(), flags=re.I)
        q = re.sub(r"^(?:a|an|some|any)\s+", "", q, flags=re.I).strip()
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
            remembered = self._playlist_from_memory(q_clean, mine)
            if remembered:
                return remembered
            # Misheard short names: "my MO playlist" is "moe", "my what playlist" may be "wut".
            if q_core and len(q_core.split()) <= 2:
                from modules.desktop.window_match import sounds_like
                alike = [p for p in mine if owned(p) and len(p.get("name", "").split()) <= 3 and
                         sounds_like(q_core, p.get("name", "")) >= 0.9]
                if len(alike) == 1:
                    p = alike[0]
                    return {"kind": "playlist", "uri": p["uri"], "name": p["name"], "id": p["id"], "context": True,
                            "owned": True}
        if not allow_public or not q_clean:
            return None
        banned = self.memory.banned_playlists()
        pls = [p for p in _items(self.client.search(q_clean, "playlist", limit=8), "playlist")
               if p.get("uri") not in banned and _playlist_size(p) != 0]
        if not pls and len(q_clean.split()) >= 2:
            # A misheard word ("Dominican Dembeau"): search without it, keep names that sound right.
            wider = _items(self.client.search(" ".join(q_clean.split()[:-1]), "playlist", limit=10), "playlist")
            pls = [p for p in wider if p.get("uri") not in banned and _playlist_size(p) != 0
                   and _title_score(q_clean, p.get("name", "")) >= 0.6]
        if not pls:
            return None

        def public_score(p):
            name = p.get("name", "")
            s = max(_sim(q_clean, name), _title_score(q_clean, name))
            if re.search(rf"\b{re.escape(_pnorm(q_clean))}\b", _pnorm(name)):
                s += 0.5                                   # the words asked for are in the name
            size = _playlist_size(p)
            return s + (0.15 if size is None or size >= 20 else -0.2)
        best = max(pls, key=public_score)
        return {"kind": "playlist", "uri": best["uri"], "name": best["name"], "id": best.get("id"),
                "context": True, "owned": False,
                "artist": (best.get("owner") or {}).get("display_name", "")}

    def _playlist_from_memory(self, q: str, mine: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """'play my gym playlist' when SAINT was told "moe playlist is my GYM
        playlist": find the other name in memory, match it to one of the
        user's playlists and remember the nickname for next time."""
        try:
            from modules.memory.service import memory_service
            facts = [r.entry.content for r in memory_service.recall(f"{q} playlist", limit=5, min_score=0.3,
                                                                     strict=False)]
        except Exception:
            return None
        qn = _pnorm(q)
        for fact in facts:
            if qn not in _pnorm(fact):
                continue
            names = re.findall(r"[\"“']([^\"”']{1,60})[\"”']", fact)
            names += [m.group(1) for m in re.finditer(r"(?:^|\bmy\s+|\bthe\s+|\bis\s+)([\w'&.\- ]{1,40}?)\s+playlist\b",
                                                      fact, re.I)]
            for name in names:
                n = _pnorm(re.sub(r"^(?:my|the)\s+", "", name.strip(), flags=re.I))
                if not n or n == qn:
                    continue
                hit = next((p for p in mine if _pnorm(p.get("name", "")) == n), None) or next(
                    (p for p in mine if difflib.SequenceMatcher(None, n, _pnorm(p.get("name", ""))).ratio() >= 0.85),
                    None)
                if hit:
                    self.memory.set_playlist_alias(q, hit["id"], hit["name"], confidence=0.9)
                    logger.info("spotify.playlist_from_memory %r -> %r (fact %r)", q, hit["name"], fact[:60])
                    return {"kind": "playlist", "uri": hit["uri"], "name": hit["name"], "id": hit["id"],
                            "context": True, "owned": True}
        return None

    def ban_playlist(self):
        """'Never play that playlist again': the playlist playing now."""
        st = self._state(force=True)
        uri = st.get("context_uri") or ""
        if not uri.startswith("spotify:playlist:"):
            raise SpotifyAPIError("What's playing isn't from a playlist.", status=404, code="NO_MATCH")
        name = ""
        try:
            name = (self.client.request("GET", f"/playlists/{uri.split(':')[-1]}", params={"fields": "name"})
                    or {}).get("name", "")
        except Exception:
            pass
        self.memory.ban_playlist(uri, name)
        if st.get("is_playing"):
            try:
                self.pause()
            except Exception:
                pass
        return {"banned": name or "that playlist", "uri": uri}

    def _resolve_genre(self, genre: str) -> Optional[Dict[str, Any]]:
        g = re.sub(r"^(?:some|more|a bit of|a little|good|the best|best|new|popular|top)\s+", "", genre.strip(),
                   flags=re.I)
        g = re.sub(r"\s+(?:music|songs|tracks|hits|tunes|vibes|jams|bangers|mix|radio|playlist|stuff)$", "", g,
                   flags=re.I).strip()
        banned = self.memory.banned_playlists()
        # "spanish" alone finds playlists about anything Spanish; "spanish music" finds music.
        pls = _items(self.client.search(f"{g} music" if len(g.split()) == 1 else g, "playlist", limit=10), "playlist")
        ng = _norm(g)
        good = [p for p in pls if p.get("uri") not in banned and _playlist_size(p) != 0 and
                (re.search(rf"\b{re.escape(ng)}", _norm(p.get("name", "")))
                 or re.search(rf"\b{re.escape(ng)}", _norm(p.get("description", ""))))]
        if good:
            def gscore(p):
                name = _norm(p.get("name", ""))
                size = _playlist_size(p)
                s = 1.0 if re.search(rf"\b{re.escape(ng)}\b", name) else 0.5
                s += 0.3 if size is None or size >= 25 else (-0.4 if size < 10 else 0.0)
                s += 0.2 if re.search(r"\b(hits|best|top|mix|essentials|classics|music)\b", name) else 0.0
                return s + random.random() * 0.15        # a little variety between equally good lists
            p = max(good, key=gscore)
            return {"kind": "genre", "uri": p["uri"], "name": p["name"], "genre": g, "id": p.get("id"),
                    "context": True}
        tracks = _items(self.client.search(f'genre:"{g}"', "track", limit=10), "track")
        if tracks:
            random.shuffle(tracks)
            return {"kind": "genre", "uris": [t["uri"] for t in tracks], "name": f"{g} tracks", "genre": g,
                    "first": f"{tracks[0]['name']} by {_artists(tracks[0])}"}
        return None

    def resolve(self, query: str, kind: str = "auto", own_only: bool = False) -> Optional[Dict[str, Any]]:
        q = re.sub(r"\s+(?:on|in|from|with) spotify$", "", query.strip(), flags=re.I).strip(" .!?")
        q = re.sub(r"(?<!\w)['\"“”]|['\"“”](?!\w)", "", q).strip()        # "'moe'" -> "moe"
        if kind == "track":
            return self._resolve_track(q)
        if kind == "artist":
            return self._resolve_artist(q)
        if kind == "album":
            return self._resolve_album(q)
        if kind == "playlist":
            return self._resolve_playlist(q, allow_public=not own_only)
        if kind == "genre":
            return self._resolve_genre(q)
        # auto. "a playlist called X" / "a Spanish playlist" name a playlist.
        pm = re.match(r"^(?:(?:a|an|the|my|some)\s+)?playlist\s+(?:called|named|titled)?\s*(.+)$", q, re.I) or \
            re.match(r"^(?:(?:a|an|the|my|some)\s+)?(.+?)\s+playlist$", q, re.I)
        if pm:
            return self._resolve_playlist(pm.group(1), allow_public=not own_only)
        q = re.sub(r"^(?:some\s+more|more|some)\s+(?=\w)", "", q, flags=re.I)       # "more Spanish music"
        # "Spanish music", "latin hits", "sad songs": a kind of music, played as a
        # playlist, not the one track whose title happens to contain those words.
        dm = _DESCRIPTOR_TAIL.match(q)
        if dm:
            what = dm.group("what").strip()
            if _norm(what) in GENRES or not self._artist_named_exactly(what):
                g = self._resolve_genre(q)
                if g:
                    return g
            else:
                return self._resolve_artist(what)
        if _norm(q) in LANGUAGE_GENRES:
            g = self._resolve_genre(q)
            if g:
                return g
        # "X by Y" is a track (or an album) — unless the whole phrase is a title ("Stand By Me").
        title, artist = self._split_by(q)
        if artist:
            hit = self._resolve_track(q) or self._resolve_album(q)
            if hit is None or hit.get("kind") == "track" and (hit.get("title_score", 1.0) < 0.75
                                                              or hit.get("artist_score", 1.0) < 0.75):
                whole = _items(self.client.search(q, "track", limit=5), "track")
                w = max(whole, key=lambda t: _title_score(q, t.get("name", "")), default=None)
                if w is not None and _sim(q, w.get("name", "")) >= 0.9:
                    return {"kind": "track", "uri": w["uri"], "name": w["name"], "artist": _artists(w),
                            "id": w.get("id"), "item": w}
            return hit
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
        # Whatever played before is replaced: so is its auto-queue.
        self._radio_stop("new playback")
        for u in ([ent.get("uri")] if not ent.get("context") else []) + list(ent.get("uris") or []):
            self._radio_orphans.pop((u or "").split(":")[-1], None)     # asked for by name: never skip it
        if ent.get("uris"):
            self._with_device(lambda d: self.client.play(uris=ent["uris"], device_id=d))
        elif ent.get("context"):
            self._with_device(lambda d: self.client.play(context_uri=ent["uri"], device_id=d))
        else:
            self._with_device(lambda d: self.client.play(uris=[ent["uri"]], device_id=d))

    def play_query(self, query, kind="auto", own_only=False):
        ent = self.resolve(query, kind, own_only=bool(own_only))
        if ent is None and own_only:
            # "My gym playlist": never swap in a stranger's playlist without asking.
            raise SpotifyAPIError(f"You don't have a playlist called {query}.", status=404, code="NOT_MINE")
        if ent is None:
            raise SpotifyAPIError(f"Nothing found for {query}", status=404, code="NO_MATCH")
        self._play_entity(ent)
        if config.get("spotify.track_history", True):
            self.memory.record_request(query, ent["kind"], ent.get("name", ""), ent.get("uri", ""),
                                       ent.get("artist", ""))
        logger.info("spotify.play kind=%s name=%r artist=%r query=%r", ent["kind"], ent.get("name"),
                    ent.get("artist"), query)
        radio = False
        if ent["kind"] == "track" and ent.get("item") and self._autoqueue_on():
            # One song on its own stops when it ends: keep going with songs like it.
            self._radio_start(seed_track=ent["item"], played_id=ent.get("id"))
            radio = True
        self._refresh_soon()
        return {"success": True, "action": "play", "kind": ent["kind"], "name": ent.get("name"),
                "artist": ent.get("artist", ""), "uri": ent.get("uri"), "genre": ent.get("genre"),
                "first": ent.get("first"), "owned": ent.get("owned"), "radio": radio}

    def play(self, uri=None):
        if uri:
            ctx = not uri.startswith("spotify:track:")
            self._play_entity({"uri": uri, "context": ctx})
            if not ctx and self._autoqueue_on():
                try:
                    item = self.client.request("GET", f"/tracks/{uri.split(':')[-1]}")[0]
                except SpotifyAPIError:
                    item = None
                if item:
                    self._radio_start(seed_track=item, played_id=item.get("id"))
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
        self._radio_stop("liked songs")
        self._with_device(lambda d: self.client.play(uris=uris, device_id=d))
        self.memory.record_request("liked songs", "liked", "Liked Songs")
        self._refresh_soon()
        return {"success": True, "action": "play", "kind": "liked", "count": len(uris)}

    def pause(self):
        self._with_device(lambda d: self.client.pause(device_id=d))
        self._refresh_soon(0.5)
        return {"success": True, "action": "pause"}

    def next(self, source="voice"):
        try:
            st = self._state()
            if st["item"] and config.get("spotify.track_history", True):
                dur = st["duration_ms"] or 1
                if st["progress_ms"] < SKIP_FRACTION * dur:
                    self.memory.record_skip(st["item"], st["progress_ms"], source=source or "voice")
                self._note_rec_outcome(st["id"], st["progress_ms"], dur, skipped=st["progress_ms"] < SKIP_FRACTION * dur)
                with self._lock:
                    # The poller must not count this track change as a second (app) skip.
                    self._skip_handled_id = st["id"]
                    self._last = None
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
        with self._lock:
            self._radio_orphans.pop(ent["uri"].split(":")[-1], None)
            if self._radio is not None and ent["uri"].startswith("spotify:track:"):
                # The user's own pick coming up is not "moved on from the auto-queue".
                self._radio["known"].add(ent["uri"].split(":")[-1])
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
        for artist, score in self._spotify_taste_artists().items():
            seeds[artist] = seeds.get(artist, 0) + score
        for artist, score in self.memory.feedback_scores().items():
            if artist:
                seeds[artist] = seeds.get(artist, 0) + 2 * score
        banned = self.memory.banned_artists()
        return {k: v for k, v in seeds.items() if v > 0 and k.lower() not in banned}

    def _spotify_taste_artists(self) -> Dict[str, float]:
        """Artists from Spotify's own data — your top tracks (last month) and
        saved songs — cached for 6 hours so recommendations stay fast."""
        cached = self.memory.cached_preference("cache.spotify_taste", 6 * 3600)
        if isinstance(cached, dict):
            return {str(k): float(v) for k, v in cached.items()}
        scores: Dict[str, float] = {}
        try:
            for i, t in enumerate((self.client.top_tracks("short_term", 20) or {}).get("items", [])):
                for a in (t.get("artists") or [])[:1]:
                    scores[a["name"]] = scores.get(a["name"], 0) + max(0.5, 3 - i * 0.15)
        except Exception:
            pass
        try:
            for entry in (self.client.saved_tracks(50) or {}).get("items", []):
                for a in ((entry.get("track") or {}).get("artists") or [])[:1]:
                    scores[a["name"]] = scores.get(a["name"], 0) + 0.5
        except Exception:
            pass
        if scores:
            self.memory.set_preference("cache.spotify_taste", scores, "cache")
        return scores

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

    def recommend(self, context="", limit=5, similar_to_current=False, seed="", mood="", novel=False,
                  seed_track=None, exclude_ids=None, per_artist=2):
        limit = max(1, min(30, int(limit)))
        recent_ids = {r["track_id"] for r in self.memory.recent(time.time() - 3 * 3600, 200)}
        recent_ids |= set(exclude_ids or ())
        skipped = self.memory.skipped_track_ids(30) | self.memory.disliked_track_ids()
        banned = self.memory.banned_artists()
        if novel:       # "something I haven't heard": nothing from your history or saved songs
            recent_ids |= self.memory.all_track_ids()
            try:
                recent_ids |= {(e.get("track") or {}).get("id") for e in
                               (self.client.saved_tracks(50) or {}).get("items", [])}
            except SpotifyAPIError:
                pass
        mood = (mood or "").lower()
        mood_words = MOODS.get(mood, ())
        candidates: Dict[str, tuple] = {}
        basis = ""

        def add(track, score, why):
            tid = track.get("id")
            if not tid or tid in recent_ids or tid in skipped or not track.get("uri"):
                return
            if any((a or {}).get("name", "").lower() in banned for a in track.get("artists") or []):
                return
            if tid not in candidates or candidates[tid][0] < score:
                candidates[tid] = (score, track, why)

        if similar_to_current or seed or seed_track:
            if seed_track:
                main = (seed_track.get("artists") or [{}])[0]
                if seed_track.get("id"):
                    recent_ids.add(seed_track["id"])
                basis = f"similar to {seed_track.get('name', '')} by {_artists(seed_track)}"
            elif seed:
                main, exclude, label = self._seed_from(seed)
                if exclude:
                    recent_ids.add(exclude)
                basis = f"similar to {label}"
                seed_track = None
            else:
                st = self._state()
                if not st["item"]:
                    raise SpotifyAPIError("Nothing is playing to base that on.", status=404, code="NO_PLAYBACK")
                recent_ids.add(st["id"])
                seed_track = st["item"]
                main = (st["item"].get("artists") or [{}])[0]
                basis = f"similar to {st['track']} by {st['artists']}"
            self._similar_candidates(main, seed_track, add, set(recent_ids), have=lambda: len(candidates),
                                     want=limit * 2)
        else:
            seeds = self._seed_artists()
            if not seeds and not mood_words:
                return {"recommendations": [], "reason": "no_history", "context": context}
            if mood_words:
                # DJ mode: favour the artists you play whose genres fit the mood.
                fitted = {}
                for name, weight in sorted(seeds.items(), key=lambda kv: kv[1], reverse=True)[:25]:
                    genres = " ".join(self.memory.artist_genres_by_name(name) or [])
                    fit = sum(1 for w in mood_words if w in genres)
                    fitted[name] = weight * (1.0 + fit) if fit else weight * 0.3
                seeds = fitted
                # Built from scratch out of what you actually play: your own songs that fit the mood...
                self._mood_favourites(mood, add)
            top = sorted(seeds.items(), key=lambda kv: kv[1], reverse=True)[:6]
            basis = "your listening history (" + ", ".join(a for a, _ in top[:3]) + ")"
            if mood_words:
                basis = f"your {mood} side (" + ", ".join(a for a, _ in top[:3]) + ")"
            ctx_words = {w for w in re.findall(r"[a-z]+", (context or "").lower()) if len(w) > 3}
            # ...and, for discovery, more from the artists you play that fit it.
            for name, weight in top:
                for t in _items(self.client.search(f'artist:"{name}"', "track", limit=10), "track"):
                    s = weight / max(1.0, top[0][1]) * 2 + random.random() * 0.6
                    hay = (t.get("name", "") + " " + (t.get("album") or {}).get("name", "")).lower()
                    s += 0.3 * sum(1 for w in ctx_words if w in hay)
                    add(t, s, f"you listen to {name}")
            if context and _norm(context) in GENRES:
                for t in _items(self.client.search(f'genre:"{_norm(context)}"', "track", limit=10), "track"):
                    add(t, 1.5 + random.random(), context)
            for g in mood_words[:2]:
                if len(candidates) >= 20:
                    break
                for t in _items(self.client.search(f'genre:"{g}"', "track", limit=10), "track"):
                    if (t.get("popularity") or 0) >= 45:
                        add(t, 1.2 + random.random() * 0.5, g)
        ranked = sorted(candidates.values(), key=lambda x: x[0], reverse=True)
        counts: Dict[str, int] = {}
        out, names = [], set()
        for score, t, why in ranked:      # variety: a few tracks per artist at most, no re-releases
            a = _artists(t)
            lead = a.split(",")[0].strip().lower()
            key = (_norm(t.get("name", "")), lead)
            if key in names:
                continue
            names.add(key)
            if counts.get(lead, 0) >= per_artist:
                continue
            counts[lead] = counts.get(lead, 0) + 1
            out.append({"id": t.get("id"), "uri": t.get("uri"), "name": t.get("name"), "artists": a,
                        "album": (t.get("album") or {}).get("name"), "why": why, "score": round(score, 2),
                        "item": t})
            if len(out) >= limit:
                break
        return {"recommendations": _spread_artists(out), "basis": basis, "context": context}

    def recommend_tool(self, context="", limit=5, similar_to_current=False, seed=""):
        rec = self.recommend(context=context, limit=limit, similar_to_current=similar_to_current, seed=seed)
        rec["recommendations"] = [{k: v for k, v in r.items() if k != "item"} for r in rec["recommendations"]]
        return rec

    def _similar_candidates(self, main: Dict[str, Any], seed_track: Optional[Dict[str, Any]], add, exclude: set,
                            have=lambda: 0, want: int = 20):
        """Songs like ``seed_track`` / ``main`` artist, best evidence first; the slower,
        weaker sources only run when the stronger ones didn't find enough."""
        taste = {k.lower(): v for k, v in self._seed_artists().items()}
        top_taste = max(taste.values(), default=1.0) or 1.0

        def personal(t):
            name = ((t.get("artists") or [{}])[0] or {}).get("name", "").lower()
            return 0.5 * min(1.0, taste.get(name, 0.0) / top_taste)

        # 1. What people actually put next to it: songs sharing public playlists with it.
        for tid, (n, t, contains) in self._playlist_neighbours(seed_track, main.get("name", ""), exclude).items():
            add(t, 1.7 + 0.35 * min(n, 3) + (0.3 if contains else 0.0) + personal(t) + random.random() * 0.3,
                "often played alongside it")
        # 2. The artist's own best-known songs.
        top = []
        if main.get("id"):
            try:
                top = self.client.artist_top_tracks(main["id"])
            except SpotifyAPIError:
                top = []
        if not top:
            top = _items(self.client.search(f'artist:"{main.get("name", "")}"', "track", limit=10), "track")
        for t in top[:10]:
            add(t, 2.0 + random.random() * 0.4, f"more {main.get('name')}")
        # 3. Artists featured alongside them.
        featured = {}
        for t in top:
            for a in (t.get("artists") or [])[1:]:
                if a and a.get("id") != main.get("id") and a.get("name"):
                    featured[a["name"]] = a
        if have() >= want:
            return
        for name in list(featured)[:3]:
            for t in _items(self.client.search(f'artist:"{name}"', "track", limit=5), "track"):
                add(t, 1.9 + personal(t) + random.random() * 0.4, f"features with {main.get('name')}")
        # 4. Artists the user already plays who share a genre (personal + similar).
        genres = self._genres_for(main.get("id"), main.get("name", ""))
        gset = set(genres)
        if gset and have() < want:
            for name, weight in sorted(self._seed_artists().items(), key=lambda kv: kv[1], reverse=True)[:8]:
                if have() >= want:
                    break
                if name == main.get("name"):
                    continue
                found = _items(self.client.search(name, "artist", limit=1), "artist")
                if not found or not gset & set(self._genres_for(found[0]["id"], found[0]["name"])):
                    continue
                for t in _items(self.client.search(f'artist:"{name}"', "track", limit=5), "track"):
                    add(t, 2.1 + random.random() * 0.4, f"you like {name}, similar style")
        # 5. Genre search only as a weak fallback (it surfaces obscure uploads).
        for g in (genres[:2] if have() < want // 2 else []):
            for t in _items(self.client.search(f'genre:"{g}"', "track", limit=10), "track"):
                if all((a or {}).get("id") != main.get("id") for a in t.get("artists", [])) \
                        and (t.get("popularity") or 0) >= 40:
                    add(t, 1.0 + random.random() * 0.4, g)

    def _playlist_neighbours(self, seed_track: Optional[Dict[str, Any]], artist: str, exclude: set) -> Dict[str, tuple]:
        """track id -> (playlists it shares with the seed, track, shared a list that has the seed itself)."""
        name = (seed_track or {}).get("name", "")
        query = f"{_norm(name)} {artist}".strip() if name else artist
        if not query:
            return {}
        try:
            pls = _items(self.client.search(query, "playlist", limit=6), "playlist")
        except SpotifyAPIError:
            return {}
        me, banned = self._my_user_id(), self.memory.banned_playlists()
        seed_id = (seed_track or {}).get("id")
        found: Dict[str, tuple] = {}
        chosen = [p for p in pls if p.get("uri") not in banned and (p.get("owner") or {}).get("id") != me
                  and not (_playlist_size(p) is not None and _playlist_size(p) < 8)][:3]

        def fetch(p):
            try:
                return self.client.playlist_items(p["id"], 50) or {}
            except SpotifyAPIError:
                return {}
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as pool:
            pages = list(pool.map(fetch, chosen))
        for data in pages:
            tracks = [(e or {}).get("track") or (e or {}).get("item") or {} for e in data.get("items", [])]
            tracks = [t for t in tracks if t.get("id") and t.get("uri", "").startswith("spotify:track:")]
            contains = bool(seed_id) and any(t["id"] == seed_id for t in tracks)
            for t in tracks:
                if t["id"] in exclude or t["id"] == seed_id:
                    continue
                n, _t, c = found.get(t["id"], (0, t, False))
                found[t["id"]] = (n + 1, t, c or contains)
        # A playlist that never had the seed in it only counts when songs repeat across lists.
        return {k: v for k, v in found.items() if v[2] or v[0] >= 2}

    def _mood_favourites(self, mood: str, add):
        """Your own songs that fit ``mood``: played, not skipped away, not disliked."""
        words = MOODS.get(mood, ())
        for r in self.memory.track_stats(60, 400):
            plays, skips = int(r.get("plays") or 0), int(r.get("skips") or 0)
            if r.get("feedback", 0) < -0.3 or skips > plays // 2:
                continue
            genres = " ".join(self.memory.artist_genres(r["artist_id"]) or []) if r.get("artist_id") else ""
            fit = sum(1 for w in words if w in genres)
            if words and genres and not fit:
                continue                                  # clearly another mood
            s = 1.2 + 0.45 * math.log1p(max(0, plays - skips)) + 0.5 * max(-1.0, min(1.0, r.get("feedback", 0)))
            s += 0.35 * min(fit, 2) if fit else -0.4      # unknown genres: only if nothing better
            track = {"id": r["track_id"], "uri": r["track_uri"], "name": r.get("track_name") or "",
                     "artists": [{"name": r.get("artist") or "", "id": r.get("artist_id")}],
                     "album": {"name": r.get("album") or ""}}
            add(track, s + random.random() * 0.7, f"one of your {mood} favourites")

    def infer_mood(self) -> str:
        """The mood of what you've been playing for the last hour and a half, else the time of day."""
        counts: Counter = Counter()
        for r in self.memory.recent(time.time() - 5400, 40):
            genres = " ".join(self.memory.artist_genres(r["artist_id"]) or []) if r.get("artist_id") else ""
            for mood in ("energetic", "chill", "sad", "happy", "darker"):
                counts[mood] += sum(1 for w in MOODS[mood] if w in genres)
        if counts and counts.most_common(1)[0][1] >= 3:
            return counts.most_common(1)[0][0]
        hour = time.localtime().tm_hour
        return "chill" if hour >= 22 or hour < 8 else "happy" if hour < 13 else "energetic" if hour < 19 else "chill"

    def play_recommended(self, context="", similar_to_current=False, seed="", mood="", novel=False,
                         seed_last=False):
        if seed_last and not seed:
            # "more like the last song": the track before the one playing now.
            rows = [r for r in self.memory.recent(limit=5) if r.get("track_name")]
            st = self._state()
            prev = next((r for r in rows if r.get("track_id") != st.get("id")), None)
            if prev is None:
                raise SpotifyAPIError("I don't know what the last song was yet.", status=404, code="NO_HISTORY")
            seed = f"{prev['track_name']} by {prev['artist']}"
        if (mood or "").lower() == "auto":
            mood = self.infer_mood()
        rec = self.recommend(context=context, limit=6 + RADIO_BATCH * 2, similar_to_current=similar_to_current,
                             seed=seed, mood=mood, novel=novel, per_artist=3 if (similar_to_current or seed) else 2)
        recs = rec["recommendations"]
        if not recs:
            if rec.get("reason") == "no_history":
                raise SpotifyAPIError("Not enough listening history yet.", status=404, code="NO_HISTORY")
            raise SpotifyAPIError("No recommendations found.", status=404, code="NO_MATCH")
        first = recs[0]
        self._radio_stop("new recommendation")
        # Start the first song now; the next ones go straight into the queue and
        # the auto-queue keeps topping it up in the same spirit.
        self._with_device(lambda d: self.client.play(uris=[first["uri"]], device_id=d))
        self._note_recs([first])
        queued = self._queue_now(recs[1:1 + RADIO_BATCH]) if self._autoqueue_on() else []
        if self._autoqueue_on():
            self._radio_start(seed_track=None if mood else (first.get("item") if similar_to_current or seed else None),
                              mood=mood, context=context, novel=novel, played_id=first["id"],
                              queued=[r["id"] for r in queued], pool=recs[1 + RADIO_BATCH:],
                              seen={r["id"] for r in recs}, basis=rec.get("basis", ""),
                              similar=bool(similar_to_current or seed))
        self.memory.record_request(context or (f"like {seed}" if seed else f"{mood} mood" if mood else
                                               "similar" if similar_to_current else "something I like"),
                                   "recommendation", first["name"], first["uri"], first["artists"])
        self._refresh_soon()
        return {"success": True, "action": "play", "kind": "recommendation", "name": first["name"],
                "artist": first["artists"], "count": 1 + len(queued), "basis": rec.get("basis", ""),
                "mood": mood or ""}

    def queue_similar(self, count=RADIO_BATCH):
        """'Queue more songs like this': keep the song that's playing, add songs like it
        to the queue, and keep the queue topped up."""
        count = max(1, min(10, int(count or RADIO_BATCH)))
        st = self._state(force=True)
        if not st["item"]:
            raise SpotifyAPIError("Nothing is playing to base that on.", status=404, code="NO_PLAYBACK")
        with self._lock:
            seen = set(self._radio["seen"]) if self._radio else set()
        rec = self.recommend(limit=count * 3, seed_track=st["item"], exclude_ids=seen, per_artist=3)
        recs = rec["recommendations"]
        if not recs:
            raise SpotifyAPIError("I couldn't find songs like this one.", status=404, code="NO_MATCH")
        queued = self._queue_now(recs[:count])
        if not queued:
            raise SpotifyAPIError("Spotify wouldn't add songs to the queue.", status=403, code="FORBIDDEN")
        self._radio_start(seed_track=st["item"], played_id=st["id"], queued=[r["id"] for r in queued],
                          pool=recs[count:], seen={r["id"] for r in recs} | seen, basis=rec.get("basis", ""),
                          similar=True)
        return {"success": True, "action": "queue", "count": len(queued), "names": [
            f"{r['name']} by {r['artists']}" for r in queued], "basis": rec.get("basis", ""),
                "seed": st["track"]}

    # ------------------------------------------------------------------ #
    # Auto-queue ("radio"): a single song, a mood or "more like this" keeps
    # going — songs are added RADIO_BATCH at a time, topped up as they play.
    # ------------------------------------------------------------------ #
    def _autoqueue_on(self) -> bool:
        return bool(config.get("spotify.autoqueue", True))

    def _note_recs(self, recs):
        now = time.time()
        with self._lock:
            for r in recs:
                self._rec_ids[r["id"]] = {"at": now, "track": {
                    "id": r["id"], "name": r.get("name", ""), "artist": (r.get("artists") or "").split(",")[0].strip()}}

    def _queue_now(self, recs) -> List[Dict[str, Any]]:
        """Add songs to Spotify's queue, in order. Returns the ones that went in."""
        done = []
        for r in recs:
            try:
                self._with_device(lambda d, u=r["uri"]: self.client.queue(u, device_id=d))
            except SpotifyAPIError as e:
                logger.warning("spotify.queue_failed %r: %s", r.get("name"), e)
                break
            done.append(r)
        self._note_recs(done)
        return done

    def _radio_start(self, seed_track=None, mood="", context="", novel=False, played_id=None, queued=None,
                     pool=None, seen=None, basis="", similar=True):
        with self._lock:
            self._radio_orphan_leftovers()
            self._radio_gen += 1
            r = {"gen": self._radio_gen, "seed": seed_track, "mood": mood, "context": context, "novel": novel,
                 "similar": similar, "played": played_id, "queued": list(queued or []), "pool": list(pool or []),
                 "seen": set(seen or ()) | set(queued or ()) | ({played_id} if played_id else set()),
                 "known": set(), "started": time.time(), "filling": False, "basis": basis, "drift": None}
            self._radio = r
        logger.info("spotify.radio.start seed=%r mood=%r queued=%d", (seed_track or {}).get("name"), mood,
                    len(r["queued"]))
        if not r["queued"]:
            threading.Thread(target=self._radio_fill, args=(r["gen"],), daemon=True, name="spotify-radio").start()

    def _radio_stop(self, why: str = ""):
        with self._lock:
            if self._radio is None:
                return
            self._radio_orphan_leftovers()
            logger.info("spotify.radio.stop (%s)", why)
            self._radio = None
            self._radio_gen += 1

    def _radio_orphan_leftovers(self):
        """Caller holds the lock. Queued songs of the radio being replaced that
        haven't played yet will be skipped if they come up."""
        r = self._radio
        if r is None:
            return
        last = r.get("last_index", -1)
        now = time.time()
        for tid in r["queued"][last + 1:]:
            self._radio_orphans[tid] = now
        for tid, at in list(self._radio_orphans.items()):
            if now - at > 3 * 3600:
                del self._radio_orphans[tid]

    def _radio_fill(self, gen: int, n: int = RADIO_BATCH) -> int:
        with self._lock:
            r = self._radio
            if r is None or r["gen"] != gen or r["filling"]:
                return 0
            r["filling"] = True
        try:
            if len(r["pool"]) < n:
                r["pool"] += self._radio_more(r)
            batch, r["pool"] = r["pool"][:n], r["pool"][n:]
            if self._radio is not r:
                return 0
            done = self._queue_now(batch)
            with self._lock:
                r["queued"] += [x["id"] for x in done]
            logger.info("spotify.radio.queued %d (%s)", len(done), ", ".join(x["name"] for x in done)[:160])
            return len(done)
        except SpotifyAPIError as e:
            logger.warning("spotify.radio.fill_failed %s", e)
            return 0
        except Exception:
            logger.exception("spotify.radio.fill_crashed")
            return 0
        finally:
            r["filling"] = False

    def _radio_more(self, r) -> List[Dict[str, Any]]:
        """More songs for the auto-queue, never one it already had."""
        if r["mood"] or not r["similar"]:
            rec = self.recommend(context=r["context"], limit=RADIO_BATCH * 3, mood=r["mood"], novel=r["novel"],
                                 exclude_ids=r["seen"])
        else:
            # Drift with the listener: base it on the last queued song they played through.
            seed = r.get("drift") or r["seed"]
            if seed is None:
                return []
            rec = self.recommend(limit=RADIO_BATCH * 3, seed_track=seed, exclude_ids=r["seen"], per_artist=3)
        recs = rec["recommendations"]
        r["seen"] |= {x["id"] for x in recs}
        return recs

    def _radio_tick(self, st: Dict[str, Any], prev: Optional[Dict[str, Any]]):
        """Poller: top the queue up as it plays; notice when the user moved on."""
        tid = st.get("id")
        if not tid:
            return
        with self._lock:
            orphan = tid in self._radio_orphans and not (self._radio and tid in self._radio["queued"])
            r = self._radio
        if orphan and st.get("is_playing"):
            # Left over from an earlier auto-queue: the user has moved on from it.
            logger.info("spotify.radio.skip_leftover %r", st.get("track"))
            with self._lock:
                self._radio_orphans.pop(tid, None)
                self._skip_handled_id = tid
            self._saint_skip_at = time.time()
            try:
                self._with_device(lambda d: self.client.next(device_id=d))
            except SpotifyAPIError:
                pass
            return
        if r is None:
            return
        if tid in r["queued"]:
            i = r["queued"].index(tid)
            r["last_index"] = max(r.get("last_index", -1), i)
            if prev and prev.get("id") in r["queued"] and prev.get("listened", 0) >= 0.6:
                r["drift"] = prev.get("item")
            if len(r["queued"]) - i - 1 <= RADIO_LOW_WATER:
                threading.Thread(target=self._radio_fill, args=(r["gen"],), daemon=True,
                                 name="spotify-radio").start()
        elif tid == r["played"] or tid in r["known"]:
            if not r["queued"] and not r["filling"] and time.time() - r["started"] > 20:
                threading.Thread(target=self._radio_fill, args=(r["gen"],), daemon=True,
                                 name="spotify-radio").start()
        elif time.time() - r["started"] > 20:
            self._radio_stop("something else is playing")

    def radio_status(self) -> Dict[str, Any]:
        with self._lock:
            r = self._radio
            if r is None:
                return {"active": False}
            return {"active": True, "mood": r["mood"], "basis": r["basis"], "queued": len(r["queued"]),
                    "played": r.get("last_index", -1) + 1}

    def stop_autoqueue(self):
        was = self._radio is not None
        self._radio_stop("asked to stop")
        return {"stopped": was}

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
        track = started.get("track") if isinstance(started, dict) else None
        track = track or {"id": track_id, "name": "", "artist": ""}
        listened = progress_ms / max(1, duration_ms)
        if skipped and listened < 0.35:
            self.memory.record_feedback(track, -0.5, "recommendation rejected (skipped)")
        elif listened >= 0.6:
            self.memory.record_feedback(track, 0.5, "recommendation accepted (listened)")

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
        if state and mode(label) != "smart":
            # Spotify only offers Smart Shuffle on a playlist or Liked Songs; on a single
            # song the button just toggles shuffle (clicked round for 12 s on 2026-09-29).
            ctx = ""
            try:
                ctx = self._state(force=True).get("context_uri") or ""
            except SpotifyAPIError:
                pass
            if not (ctx.startswith("spotify:playlist:") or ctx.endswith(":collection")):
                return {"smart": False, "shuffle": mode(label) != "off", "fallback": False, "unsupported": True,
                        "autoqueue": self._radio is not None}
        seen = [mode(label)]
        for _ in range(3):
            if seen[-1] == want:
                break
            label = self._spotify_shuffle_label(click=True) or ""
            seen.append(mode(label))
            if state and seen[-1] == "off" and "shuffle" in seen:
                break                          # went round without a Smart Shuffle step
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

    def ban_artist(self, name=""):
        """'No more of this artist' (the one playing) or 'no more Drake'."""
        if not name:
            st = self._state()
            if not st["item"]:
                raise SpotifyAPIError("Nothing is playing.", status=404, code="NO_PLAYBACK")
            name = (st["item"].get("artists") or [{}])[0].get("name", "")
        self.memory.ban_artist(name)
        return {"banned": name}

    def unban_artist(self, name):
        return {"unbanned": name, "was_banned": self.memory.unban_artist(name)}

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
            self._radio_tick(st, None)
            return st
        if not self._backfilled:
            # Once per start: what you played on your phone / other devices
            # while SAINT wasn't watching (Spotify's recently-played list).
            self._backfilled = True
            try:
                self.recent(50)
            except Exception as e:
                logger.debug("spotify.backfill_failed %s", e)
        prev = None
        with self._lock:
            last = self._last
            if st["id"] and (last is None or last.get("id") != st["id"]):
                if last and last.get("id"):
                    played = last["progress_ms"] + ((time.time() - last["seen_at"]) * 1000
                                                    if last.get("is_playing") else 0)
                    prev = dict(last, listened=min(1.0, played / max(1, last["duration_ms"])))
                if last and last.get("id") and last["id"] != self._skip_handled_id:
                    # Only time spent *playing* counts: a paused song left for an
                    # hour and then changed is not "listened to the end".
                    elapsed = last["progress_ms"]
                    if last.get("is_playing"):
                        elapsed += (time.time() - last["seen_at"]) * 1000
                    elapsed = min(elapsed, last["duration_ms"])
                    frac = elapsed / max(1, last["duration_ms"])
                    user_skip = frac < SKIP_FRACTION and time.time() - self._saint_skip_at > 5
                    if user_skip and last.get("item"):
                        self.memory.record_skip(last["item"], int(elapsed), source="app")
                        logger.info("spotify.skip.app track=%r at=%.0f%%", last["item"].get("name"), frac * 100)
                    self._note_rec_outcome(last["id"], elapsed, last["duration_ms"], skipped=user_skip)
                self._skip_handled_id = None
                self.memory.record_listening(st["item"], context_uri=st["context_uri"])
            if st["id"]:
                self._last = {"id": st["id"], "item": st["item"], "progress_ms": st["progress_ms"],
                              "duration_ms": st["duration_ms"] or 1, "seen_at": time.time(),
                              "is_playing": bool(st["is_playing"])}
        self._radio_tick(st, prev)
        # Slowly fill in genre data for artists we've seen (one lookup per poll).
        for artist_id in self.memory.uncached_artist_ids(1):
            self._genres_for(artist_id, "")
        return st


def speakable_error(exc) -> str:
    return friendly_error(exc)

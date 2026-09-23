"""Persistent, confidence-aware Spotify memory for SAINT.

Separate from general long-term memory so music taste can evolve on its own.
Everything here comes from real events — Spotify playback observed by the
background poller, commands the user gave, skips, and recommendation
outcomes. Nothing is fabricated: if there is no data, queries return empty.

Tables
    listening         tracks actually observed playing (poller / recent-played sync)
    requests          what the user asked SAINT to play
    skips             tracks the user skipped (and how far in)
    feedback          explicit/implicit likes & dislikes (+ recommendation outcomes)
    playlist_aliases  "my gym playlist" -> playlist id
    artist_genres     cached genres per artist (for genre-level taste)
    preferences       explicit key/value music preferences
"""

import json
import os
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing
from typing import Any, Dict, List, Optional


class SpotifyMemory:
    def __init__(self, path: Optional[str] = None):
        from core.paths import data_path
        self.path = str(path or data_path("memory", "spotify_memory.db"))
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _exec(self, sql, params=(), script=False):
        with self._lock, closing(self._connect()) as conn:
            if script:
                conn.executescript(sql)
            else:
                conn.execute(sql, params)
            conn.commit()

    def _query(self, sql, params=()):
        with self._lock, closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def _init_db(self):
        self._exec("""
            CREATE TABLE IF NOT EXISTS preferences (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, kind TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0, evidence INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS listening (
                id INTEGER PRIMARY KEY AUTOINCREMENT, played_at REAL NOT NULL,
                track_id TEXT, track_uri TEXT, track_name TEXT, artist TEXT, artists_json TEXT,
                album TEXT, context_uri TEXT, source TEXT NOT NULL DEFAULT 'spotify', artist_id TEXT);
            CREATE INDEX IF NOT EXISTS idx_listening_time ON listening(played_at DESC);
            CREATE TABLE IF NOT EXISTS playlist_aliases (
                alias TEXT PRIMARY KEY, playlist_id TEXT NOT NULL, playlist_name TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, track_id TEXT,
                track_name TEXT, artist TEXT, signal REAL NOT NULL, reason TEXT);
            CREATE INDEX IF NOT EXISTS idx_feedback_time ON feedback(created_at DESC);
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, query TEXT,
                kind TEXT, entity_name TEXT, entity_uri TEXT, artist TEXT);
            CREATE TABLE IF NOT EXISTS skips (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, track_id TEXT,
                track_name TEXT, artist TEXT, progress_ms INTEGER, duration_ms INTEGER);
            CREATE TABLE IF NOT EXISTS artist_genres (
                artist_id TEXT PRIMARY KEY, name TEXT, genres_json TEXT, updated_at REAL);
        """, script=True)
        # Older databases lack listening.artist_id
        try:
            self._exec("ALTER TABLE listening ADD COLUMN artist_id TEXT")
        except sqlite3.OperationalError:
            pass

    # ------------------------------------------------------------------ #
    # Preferences
    # ------------------------------------------------------------------ #
    def set_preference(self, key: str, value: Any, kind: str = "explicit",
                       confidence: float = 1.0, evidence: int = 1):
        rows = self._query("SELECT evidence FROM preferences WHERE key=?", (key,))
        if rows:
            evidence = max(1, int(rows[0]["evidence"]) + int(evidence))
        self._exec("""
            INSERT INTO preferences(key,value,kind,confidence,evidence,updated_at) VALUES(?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, kind=excluded.kind,
                confidence=excluded.confidence, evidence=excluded.evidence, updated_at=excluded.updated_at
        """, (key, json.dumps(value), kind, max(0.0, min(1.0, confidence)), evidence, time.time()))

    def get_preferences(self, prefix: str = "") -> List[Dict[str, Any]]:
        rows = self._query("SELECT * FROM preferences WHERE key LIKE ? ORDER BY confidence DESC, evidence DESC",
                           (prefix + "%",))
        return [dict(r, value=json.loads(r["value"])) for r in rows]

    # ------------------------------------------------------------------ #
    # Listening history
    # ------------------------------------------------------------------ #
    def record_listening(self, track: Dict[str, Any], context_uri: str = "",
                         played_at: Optional[float] = None, source: str = "spotify") -> bool:
        item = track or {}
        if not item.get("id") and not item.get("name"):
            return False
        artists = item.get("artists") or []
        now = played_at or time.time()
        # One row per play: ignore re-observations of the same track within
        # its duration (poller) or near-identical timestamps (history sync).
        window = max(2.0, (item.get("duration_ms") or 0) / 1000.0) if played_at is None else 2.0
        existing = self._query("SELECT id FROM listening WHERE track_id IS ? AND abs(played_at-?) < ?",
                               (item.get("id"), now, window))
        if existing:
            return False
        self._exec("""
            INSERT INTO listening(played_at,track_id,track_uri,track_name,artist,artists_json,album,
                                  context_uri,source,artist_id)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (
            now, item.get("id"), item.get("uri"), item.get("name"),
            artists[0].get("name", "") if artists else "",
            json.dumps([a.get("name", "") for a in artists]),
            (item.get("album") or {}).get("name", ""), context_uri, source,
            artists[0].get("id") if artists else None))
        return True

    def recent(self, since: Optional[float] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if since is None:
            return self._query("SELECT * FROM listening ORDER BY played_at DESC LIMIT ?", (limit,))
        return self._query("SELECT * FROM listening WHERE played_at>=? ORDER BY played_at DESC LIMIT ?",
                           (since, limit))

    def today(self) -> List[Dict[str, Any]]:
        t = time.localtime()
        midnight = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
        return self.recent(midnight, 200)

    def top_artists(self, days: int = 30, limit: int = 10) -> List[Dict[str, Any]]:
        since = time.time() - days * 86400
        rows = self._query("""
            SELECT artist, artist_id, COUNT(*) AS plays FROM listening
            WHERE played_at>=? AND artist != '' GROUP BY artist ORDER BY plays DESC LIMIT ?""",
                           (since, limit))
        skips = Counter(r["artist"] for r in self._query(
            "SELECT artist FROM skips WHERE created_at>=?", (since,)))
        for r in rows:
            r["skips"] = skips.get(r["artist"], 0)
        return rows

    def top_tracks(self, days: int = 30, limit: int = 10) -> List[Dict[str, Any]]:
        since = time.time() - days * 86400
        return self._query("""
            SELECT track_name, artist, track_uri, track_id, COUNT(*) AS plays FROM listening
            WHERE played_at>=? AND track_id IS NOT NULL GROUP BY track_id HAVING plays >= 2
            ORDER BY plays DESC LIMIT ?""", (since, limit))

    # ------------------------------------------------------------------ #
    # Requests / skips
    # ------------------------------------------------------------------ #
    def record_request(self, query: str, kind: str, entity_name: str = "", entity_uri: str = "",
                       artist: str = ""):
        self._exec("INSERT INTO requests(created_at,query,kind,entity_name,entity_uri,artist) VALUES(?,?,?,?,?,?)",
                   (time.time(), query, kind, entity_name, entity_uri, artist))

    def recent_requests(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self._query("SELECT * FROM requests ORDER BY created_at DESC LIMIT ?", (limit,))

    def record_skip(self, track: Dict[str, Any], progress_ms: int = 0):
        item = track or {}
        artists = item.get("artists") or []
        self._exec("""INSERT INTO skips(created_at,track_id,track_name,artist,progress_ms,duration_ms)
                      VALUES(?,?,?,?,?,?)""",
                   (time.time(), item.get("id"), item.get("name"),
                    artists[0].get("name", "") if artists else "", int(progress_ms or 0),
                    int(item.get("duration_ms") or 0)))

    def skipped_track_ids(self, days: int = 30) -> set:
        rows = self._query("SELECT track_id FROM skips WHERE created_at>=?", (time.time() - days * 86400,))
        return {r["track_id"] for r in rows if r["track_id"]}

    def skip_count(self, since: float) -> int:
        return len(self._query("SELECT id FROM skips WHERE created_at>=?", (since,)))

    # ------------------------------------------------------------------ #
    # Feedback
    # ------------------------------------------------------------------ #
    def record_feedback(self, track: Dict[str, Any], signal: float, reason: str = ""):
        item = track or {}
        artists = item.get("artists") or []
        artist = artists[0].get("name", "") if artists else item.get("artist", "")
        self._exec("""INSERT INTO feedback(created_at,track_id,track_name,artist,signal,reason)
                      VALUES(?,?,?,?,?,?)""",
                   (time.time(), item.get("id"), item.get("name"), artist, float(signal), reason))

    def feedback_scores(self, limit: int = 500) -> Dict[str, float]:
        rows = self._query("SELECT artist, signal FROM feedback ORDER BY created_at DESC LIMIT ?", (limit,))
        scores: Dict[str, float] = {}
        for row in rows:
            scores[row["artist"]] = scores.get(row["artist"], 0.0) + float(row["signal"])
        return scores

    def recommendation_outcomes(self) -> Dict[str, int]:
        rows = self._query("SELECT signal FROM feedback WHERE reason LIKE 'recommendation%'")
        return {"accepted": sum(1 for r in rows if r["signal"] > 0),
                "rejected": sum(1 for r in rows if r["signal"] < 0)}

    # ------------------------------------------------------------------ #
    # Playlist aliases
    # ------------------------------------------------------------------ #
    def set_playlist_alias(self, alias: str, playlist_id: str, playlist_name: str, confidence: float = 1.0):
        alias = " ".join(alias.lower().strip().split())
        if not alias:
            return
        self._exec("""
            INSERT INTO playlist_aliases(alias,playlist_id,playlist_name,confidence,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT(alias) DO UPDATE SET playlist_id=excluded.playlist_id,
                playlist_name=excluded.playlist_name, confidence=excluded.confidence,
                updated_at=excluded.updated_at""", (alias, playlist_id, playlist_name, confidence, time.time()))

    def resolve_playlist_alias(self, alias: str) -> Optional[Dict[str, Any]]:
        alias = " ".join(alias.lower().strip().split())
        rows = self._query("SELECT * FROM playlist_aliases WHERE alias=?", (alias,))
        return rows[0] if rows else None

    # ------------------------------------------------------------------ #
    # Genres
    # ------------------------------------------------------------------ #
    def cache_artist_genres(self, artist_id: str, name: str, genres: List[str]):
        self._exec("INSERT OR REPLACE INTO artist_genres(artist_id,name,genres_json,updated_at) VALUES(?,?,?,?)",
                   (artist_id, name, json.dumps(genres or []), time.time()))

    def artist_genres(self, artist_id: str) -> Optional[List[str]]:
        rows = self._query("SELECT genres_json, updated_at FROM artist_genres WHERE artist_id=?", (artist_id,))
        if not rows or time.time() - rows[0]["updated_at"] > 30 * 86400:
            return None
        return json.loads(rows[0]["genres_json"])

    def top_genres(self, days: int = 30, limit: int = 8) -> List[Dict[str, Any]]:
        since = time.time() - days * 86400
        rows = self._query("""
            SELECT g.genres_json, COUNT(*) AS plays FROM listening l
            JOIN artist_genres g ON g.artist_id = l.artist_id
            WHERE l.played_at>=? GROUP BY l.artist_id""", (since,))
        counts: Counter = Counter()
        for r in rows:
            for g in json.loads(r["genres_json"] or "[]"):
                counts[g] += r["plays"]
        return [{"genre": g, "plays": n} for g, n in counts.most_common(limit)]

    def uncached_artist_ids(self, limit: int = 20) -> List[str]:
        rows = self._query("""
            SELECT DISTINCT l.artist_id FROM listening l LEFT JOIN artist_genres g ON g.artist_id = l.artist_id
            WHERE l.artist_id IS NOT NULL AND g.artist_id IS NULL ORDER BY l.played_at DESC LIMIT ?""", (limit,))
        return [r["artist_id"] for r in rows]

    # ------------------------------------------------------------------ #
    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "preferences": self.get_preferences(),
            "today": self.today(),
            "top_artists": self.top_artists(30),
            "top_tracks": self.top_tracks(30),
            "top_genres": self.top_genres(30),
            "recent_requests": self.recent_requests(10),
            "skips_7d": self.skip_count(now - 7 * 86400),
            "feedback": self.feedback_scores(),
            "recommendations": self.recommendation_outcomes(),
        }

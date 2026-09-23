"""Persistent, confidence-aware Spotify memory for SAINT.

This store is intentionally separate from generic conversation memory so music
preferences, listening context, playlist aliases, and recommendation feedback
can evolve independently.
"""

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional


class SpotifyMemory:
    def __init__(self, path: str = "data/memory/spotify_memory.db"):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock, self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    evidence INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS listening (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    played_at REAL NOT NULL,
                    track_id TEXT,
                    track_uri TEXT,
                    track_name TEXT,
                    artist TEXT,
                    artists_json TEXT,
                    album TEXT,
                    context_uri TEXT,
                    source TEXT NOT NULL DEFAULT 'spotify'
                );
                CREATE INDEX IF NOT EXISTS idx_listening_time ON listening(played_at DESC);
                CREATE TABLE IF NOT EXISTS playlist_aliases (
                    alias TEXT PRIMARY KEY,
                    playlist_id TEXT NOT NULL,
                    playlist_name TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    track_id TEXT,
                    track_name TEXT,
                    artist TEXT,
                    signal REAL NOT NULL,
                    reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_time ON feedback(created_at DESC);
            """)

    def set_preference(self, key: str, value: Any, kind: str = "explicit",
                       confidence: float = 1.0, evidence: int = 1):
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT evidence FROM preferences WHERE key=?", (key,)).fetchone()
            if row:
                evidence = max(1, int(row["evidence"]) + int(evidence))
            conn.execute("""
                INSERT INTO preferences(key,value,kind,confidence,evidence,updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, kind=excluded.kind,
                    confidence=excluded.confidence, evidence=excluded.evidence,
                    updated_at=excluded.updated_at
            """, (key, json.dumps(value), kind, max(0.0, min(1.0, confidence)), evidence, now))

    def get_preferences(self, prefix: str = "") -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM preferences WHERE key LIKE ? ORDER BY confidence DESC, evidence DESC",
                (prefix + "%",),
            ).fetchall()
        return [dict(r, value=json.loads(r["value"])) for r in rows]

    def record_listening(self, track: Dict[str, Any], context_uri: str = "",
                         played_at: Optional[float] = None, source: str = "spotify"):
        item = track or {}
        artists = item.get("artists") or []
        now = played_at or time.time()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM listening WHERE track_id IS ? AND abs(played_at-?) < 2",
                (item.get("id"), now),
            ).fetchone()
            if existing:
                return
            conn.execute("""
                INSERT INTO listening
                (played_at,track_id,track_uri,track_name,artist,artists_json,album,context_uri,source)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (
                now, item.get("id"), item.get("uri"), item.get("name"),
                artists[0].get("name", "") if artists else "",
                json.dumps([a.get("name", "") for a in artists]),
                (item.get("album") or {}).get("name", ""),
                context_uri, source,
            ))

    def recent(self, since: Optional[float] = None, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if since is None:
                rows = conn.execute(
                    "SELECT * FROM listening ORDER BY played_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM listening WHERE played_at>=? ORDER BY played_at DESC LIMIT ?",
                    (since, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def record_feedback(self, track: Dict[str, Any], signal: float, reason: str = ""):
        item = track or {}
        artists = item.get("artists") or []
        artist = artists[0].get("name", "") if artists else item.get("artist", "")
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO feedback(created_at,track_id,track_name,artist,signal,reason)
                VALUES(?,?,?,?,?,?)
            """, (time.time(), item.get("id"), item.get("name"), artist, float(signal), reason))

    def feedback_scores(self, limit: int = 500) -> Dict[str, float]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT artist, signal FROM feedback ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        scores: Dict[str, float] = {}
        for row in rows:
            scores[row["artist"]] = scores.get(row["artist"], 0.0) + float(row["signal"])
        return scores

    def set_playlist_alias(self, alias: str, playlist_id: str, playlist_name: str,
                           confidence: float = 1.0):
        alias = " ".join(alias.lower().strip().split())
        if not alias:
            return
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO playlist_aliases(alias,playlist_id,playlist_name,confidence,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(alias) DO UPDATE SET
                    playlist_id=excluded.playlist_id,
                    playlist_name=excluded.playlist_name,
                    confidence=excluded.confidence,
                    updated_at=excluded.updated_at
            """, (alias, playlist_id, playlist_name, confidence, time.time()))

    def resolve_playlist_alias(self, alias: str) -> Optional[Dict[str, Any]]:
        alias = " ".join(alias.lower().strip().split())
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM playlist_aliases WHERE alias=?", (alias,)
            ).fetchone()
        return dict(row) if row else None

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        day = now - 86400
        week = now - 7 * 86400
        return {
            "preferences": self.get_preferences(),
            "today": self.recent(day, 50),
            "recent": self.recent(week, 100),
            "feedback": self.feedback_scores(),
        }

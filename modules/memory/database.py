"""
modules/memory/database.py

SQLite-backed persistent memory for SAINT.
"""

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, List, Optional, Dict
from enum import Enum


class MemoryType(Enum):
    CONVERSATION = "conversation"
    LONG_TERM = "long_term"
    PREFERENCE = "preference"
    TASK = "task"
    PROJECT = "project"


@dataclass
class MemoryEntry:
    id: int
    type: MemoryType
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    source: str = "user"
    confidence: float = 1.0
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "content": self.content,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "confidence": self.confidence,
            "tags": self.tags,
        }

    @classmethod
    def from_row(cls, row: tuple) -> "MemoryEntry":
        return cls(
            id=row[0],
            type=MemoryType(row[1]),
            content=row[2],
            metadata=json.loads(row[3]) if row[3] else {},
            created_at=row[4],
            updated_at=row[5],
            source=row[6],
            confidence=row[7],
            tags=json.loads(row[8]) if row[8] else [],
        )


class MemoryDatabase:
    """Thread-safe SQLite database for persistent memory."""

    def __init__(self, path: Optional[str] = None):
        from core.paths import data_path
        path = str(path or data_path("memory", "saint_memory.db"))
        self._path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata TEXT DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    source TEXT DEFAULT 'user',
                    confidence REAL DEFAULT 1.0,
                    tags TEXT DEFAULT '[]'
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_type
                ON memory(type)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_created
                ON memory(created_at)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_tags
                ON memory(tags)
            """)

    def store(
        self,
        type_: MemoryType,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        source: str = "user",
        confidence: float = 1.0,
        tags: Optional[List[str]] = None,
    ) -> int:
        """Store a new memory entry. Returns the entry ID."""
        now = time.time()
        with self._lock:
            with self._conn() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO memory (type, content, metadata, created_at, updated_at, source, confidence, tags)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        type_.value,
                        content,
                        json.dumps(metadata or {}),
                        now,
                        now,
                        source,
                        confidence,
                        json.dumps(tags or []),
                    ),
                )
                return cursor.lastrowid

    def retrieve(self, entry_id: int) -> Optional[MemoryEntry]:
        """Retrieve a memory entry by ID."""
        with self._lock:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM memory WHERE id = ?", (entry_id,)
                ).fetchone()
                return MemoryEntry.from_row(row) if row else None

    def search(
        self,
        query: str,
        type_: Optional[MemoryType] = None,
        limit: int = 20,
        min_confidence: float = 0.0,
    ) -> List[MemoryEntry]:
        """Search memory by text content (simple LIKE search)."""
        with self._lock:
            with self._conn() as conn:
                sql = "SELECT * FROM memory WHERE content LIKE ? AND confidence >= ?"
                params = [f"%{query}%", min_confidence]
                if type_:
                    sql += " AND type = ?"
                    params.append(type_.value)
                sql += " ORDER BY created_at DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
                return [MemoryEntry.from_row(row) for row in rows]

    def all_of_types(self, types: List[MemoryType], limit: int = 2000) -> List[MemoryEntry]:
        """All entries of the given types, newest first."""
        if not types:
            return []
        with self._lock:
            with self._conn() as conn:
                marks = ",".join("?" for _ in types)
                rows = conn.execute(
                    f"SELECT * FROM memory WHERE type IN ({marks}) ORDER BY updated_at DESC LIMIT ?",
                    [t.value for t in types] + [limit],
                ).fetchall()
                return [MemoryEntry.from_row(row) for row in rows]

    def list_by_type(
        self,
        type_: MemoryType,
        limit: int = 50,
        offset: int = 0,
    ) -> List[MemoryEntry]:
        """List all memories of a specific type."""
        with self._lock:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM memory WHERE type = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (type_.value, limit, offset),
                ).fetchall()
                return [MemoryEntry.from_row(row) for row in rows]

    def update(
        self,
        entry_id: int,
        content: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        confidence: Optional[float] = None,
        tags: Optional[List[str]] = None,
    ) -> bool:
        """Update an existing memory entry."""
        with self._lock:
            with self._conn() as conn:
                updates = []
                params = []
                if content is not None:
                    updates.append("content = ?")
                    params.append(content)
                if metadata is not None:
                    updates.append("metadata = ?")
                    params.append(json.dumps(metadata))
                if confidence is not None:
                    updates.append("confidence = ?")
                    params.append(confidence)
                if tags is not None:
                    updates.append("tags = ?")
                    params.append(json.dumps(tags))
                if not updates:
                    return False
                updates.append("updated_at = ?")
                params.append(time.time())
                params.append(entry_id)
                cursor = conn.execute(
                    f"UPDATE memory SET {', '.join(updates)} WHERE id = ?",
                    params,
                )
                return cursor.rowcount > 0

    def delete(self, entry_id: int) -> bool:
        """Delete a memory entry."""
        with self._lock:
            with self._conn() as conn:
                cursor = conn.execute("DELETE FROM memory WHERE id = ?", (entry_id,))
                return cursor.rowcount > 0

    def forget_by_type(self, type_: MemoryType, older_than_days: Optional[int] = None) -> int:
        """Delete memories of a type, optionally older than N days."""
        with self._lock:
            with self._conn() as conn:
                if older_than_days:
                    cutoff = time.time() - (older_than_days * 86400)
                    cursor = conn.execute(
                        "DELETE FROM memory WHERE type = ? AND created_at < ?",
                        (type_.value, cutoff),
                    )
                else:
                    cursor = conn.execute(
                        "DELETE FROM memory WHERE type = ?", (type_.value,)
                    )
                return cursor.rowcount

    def get_stats(self) -> Dict[str, Any]:
        """Get memory statistics."""
        with self._lock:
            with self._conn() as conn:
                stats = {}
                for type_ in MemoryType:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM memory WHERE type = ?", (type_.value,)
                    ).fetchone()[0]
                    stats[type_.value] = count
                total = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
                stats["total"] = total
                return stats


# Singleton
_memory_db: Optional[MemoryDatabase] = None


def get_memory_db() -> MemoryDatabase:
    global _memory_db
    if _memory_db is None:
        _memory_db = MemoryDatabase()
    return _memory_db
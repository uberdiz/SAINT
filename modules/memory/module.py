"""
modules/memory/module.py

Memory module with persistent SQLite storage.
Supports conversation history, long-term memory, preferences, tasks, and project state.
"""

from typing import Optional, List

from modules.base import BaseModule
from modules.memory.database import (
    MemoryDatabase,
    MemoryEntry,
    MemoryType,
    get_memory_db,
)
from core.events import event_bus, EventType
from core.config import config


class MemoryModule(BaseModule):
    name = "Memory"
    description = "Persistent memory: conversation history, long-term facts, preferences, tasks, project state."

    def __init__(self):
        super().__init__()
        self.subtasks = {
            "SQLite Database": True,
            "Conversation History": True,
            "Long-Term Memory": True,
            "Preferences": True,
            "Tasks": True,
            "Project State": True,
            "Memory Search": True,
            "Memory Management": True,
        }
        self._db: MemoryDatabase = None

    def enable(self):
        super().enable()
        self._db = get_memory_db()
        event_bus.emit_event(EventType.MODULE_ENABLED, {"module": self.name})

    def disable(self):
        super().disable()
        event_bus.emit_event(EventType.MODULE_DISABLED, {"module": self.name})

    # ------------------------------------------------------------------ #
    # High-level API
    # ------------------------------------------------------------------ #

    def remember_conversation(
        self,
        user_text: str,
        assistant_text: str,
        metadata: Optional[dict] = None,
    ) -> int:
        """Store a conversation turn."""
        return self._db.store(
            type_=MemoryType.CONVERSATION,
            content=f"User: {user_text}\nAssistant: {assistant_text}",
            metadata=metadata or {},
            source="conversation",
            confidence=1.0,
            tags=["conversation", "history"],
        )

    def remember_fact(
        self,
        fact: str,
        confidence: float = 1.0,
        tags: Optional[list] = None,
        metadata: Optional[dict] = None,
    ) -> int:
        """Store a long-term fact explicitly (e.g., 'Remember that...')."""
        return self._db.store(
            type_=MemoryType.LONG_TERM,
            content=fact,
            metadata=metadata or {},
            source="explicit",
            confidence=confidence,
            tags=tags or ["fact"],
        )

    def set_preference(self, key: str, value: str, metadata: Optional[dict] = None) -> int:
        """Store a user preference."""
        return self._db.store(
            type_=MemoryType.PREFERENCE,
            content=f"{key}: {value}",
            metadata={"key": key, "value": value, **(metadata or {})},
            source="user",
            confidence=1.0,
            tags=["preference", key],
        )

    def get_preference(self, key: str) -> Optional[str]:
        """Retrieve a user preference."""
        results = self._db.search(key, type_=MemoryType.PREFERENCE, limit=1)
        if results:
            meta = results[0].metadata
            return meta.get("value")
        return None

    def create_task(
        self,
        title: str,
        description: str = "",
        priority: str = "medium",
        metadata: Optional[dict] = None,
    ) -> int:
        """Create a persistent task."""
        return self._db.store(
            type_=MemoryType.TASK,
            content=f"{title}\n{description}",
            metadata={
                "title": title,
                "description": description,
                "priority": priority,
                "status": "pending",
                **(metadata or {}),
            },
            source="user",
            confidence=1.0,
            tags=["task", priority],
        )

    def update_task(self, task_id: int, **updates) -> bool:
        """Update a task (status, priority, etc.)."""
        entry = self._db.retrieve(task_id)
        if not entry or entry.type != MemoryType.TASK:
            return False
        metadata = {**entry.metadata, **updates}
        return self._db.update(task_id, metadata=metadata)

    def list_tasks(self, status: Optional[str] = None) -> list:
        """List all tasks, optionally filtered by status."""
        tasks = self._db.list_by_type(MemoryType.TASK, limit=100)
        if status:
            tasks = [t for t in tasks if t.metadata.get("status") == status]
        return [t.to_dict() for t in tasks]

    def set_project_state(self, key: str, value: str, metadata: Optional[dict] = None) -> int:
        """Store project state information."""
        return self._db.store(
            type_=MemoryType.PROJECT,
            content=f"{key}: {value}",
            metadata={"key": key, "value": value, **(metadata or {})},
            source="system",
            confidence=1.0,
            tags=["project", key],
        )

    def get_project_state(self, key: str) -> Optional[str]:
        """Retrieve project state."""
        results = self._db.search(key, type_=MemoryType.PROJECT, limit=1)
        if results:
            meta = results[0].metadata
            return meta.get("value")
        return None

    def search(self, query: str, type_: Optional[MemoryType] = None, limit: int = 20) -> list:
        """Search memory by text."""
        results = self._db.search(query, type_=type_, limit=limit)
        return [r.to_dict() for r in results]

    def get_recent_conversations(self, limit: int = 10) -> list:
        """Get recent conversation history."""
        results = self._db.list_by_type(MemoryType.CONVERSATION, limit=limit)
        return [r.to_dict() for r in results]

    def get_stats(self) -> dict:
        """Get memory statistics."""
        return self._db.get_stats()

    def forget_conversation_older_than(self, days: int) -> int:
        """Delete conversation history older than N days."""
        return self._db.forget_by_type(MemoryType.CONVERSATION, older_than_days=days)
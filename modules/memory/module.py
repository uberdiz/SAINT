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
        self._register_tools()
        from modules.memory.learner import learner
        learner.start()

    # ------------------------------------------------------------------ #
    # Tools (the agent and LLM use these; nothing else writes memory)
    # ------------------------------------------------------------------ #
    def _register_tools(self):
        from modules.automation.tools import Tool, PermissionLevel, P, ToolError, get_tool_registry
        from modules.memory.service import memory_service, second_person

        def available():
            if not config.get("modules.memory", True) or not config.get("memory.enabled", True):
                return False, "Memory is turned off in Settings."
            return True, ""

        def remember(content="", key="", value="", category="fact"):
            try:
                return memory_service.remember(content=content, key=key, value=value, category=category)
            except ValueError as e:
                raise ToolError(str(e), "INVALID")

        def recall(query, limit=5):
            hits = memory_service.recall(query, limit=limit)
            return {"memories": [{"id": h.entry.id, "content": h.entry.content, "key": h.key,
                                  "value": h.value, "spoken": second_person(h.entry.content),
                                  "score": round(h.score, 2)} for h in hits]}

        def list_memories(category=None):
            return {"memories": [e.to_dict() for e in memory_service.all(category)]}

        def update(id, content=None, value=None):
            if not memory_service.update(id, content=content, value=value):
                raise ToolError(f"There's no memory #{id}.", "NOT_FOUND")
            return {"id": id, "updated": True}

        def forget(id=None, query=None):
            if id is not None:
                if not memory_service.forget(id):
                    raise ToolError(f"There's no memory #{id}.", "NOT_FOUND")
                return {"deleted": [id]}
            if not query:
                raise ToolError("Tell me what to forget.", "INVALID")
            try:
                deleted = memory_service.forget_matching(query)
            except ValueError as e:
                raise ToolError(f"That matches more than one memory ({str(e)[11:]}). Which one?", "AMBIGUOUS")
            if not deleted:
                raise ToolError(f"I don't have anything stored about {second_person(query)}.", "NOT_FOUND")
            return {"deleted": [d["id"] for d in deleted], "content": [d["content"] for d in deleted]}

        def forget_all():
            return {"deleted": memory_service.forget_all()}

        reg = get_tool_registry()
        cat = ["fact", "preference", "personal", "project"]
        tools = [
            Tool("memory.remember", "Store a fact or preference about the user in long-term memory",
                 {"content": "string"}, PermissionLevel.LOW, remember,
                 parameters={"content": P("string", "the fact, in the user's words", required=False, default=""),
                             "key": P("string", "short topic, e.g. 'favorite programming language'", required=False, default=""),
                             "value": P("string", "the value, e.g. 'Python'", required=False, default=""),
                             "category": P("string", required=False, default="fact", enum=cat)},
                 llm_exposed=True, category="memory"),
            Tool("memory.recall", "Search long-term memory for stored facts about the user",
                 {"query": "string"}, PermissionLevel.LOW, recall,
                 parameters={"query": P("string", "what to look up"),
                             "limit": P("integer", required=False, default=5, minimum=1, maximum=20)},
                 llm_exposed=True, category="memory"),
            Tool("memory.list", "List everything in long-term memory", {}, PermissionLevel.LOW, list_memories,
                 parameters={"category": P("string", required=False, enum=cat)}, category="memory"),
            Tool("memory.update", "Change a stored memory by id", {"id": "int"}, PermissionLevel.LOW, update,
                 parameters={"id": P("integer"), "content": P("string", required=False),
                             "value": P("string", required=False)}, category="memory"),
            Tool("memory.forget", "Delete a memory by id or by description",
                 {"id": "int (optional)", "query": "string (optional)"}, PermissionLevel.MEDIUM, forget,
                 parameters={"id": P("integer", required=False), "query": P("string", required=False)},
                 llm_exposed=True, category="memory"),
            Tool("memory.forget_all", "Delete ALL long-term memories", {}, PermissionLevel.HIGH, forget_all,
                 parameters={}, category="memory"),
        ]
        for t in tools:
            t.availability = available
            reg.register(t)

    def disable(self):
        super().disable()

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
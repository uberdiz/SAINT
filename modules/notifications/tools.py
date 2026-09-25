"""Notification tools: read recent ones, clear, and set what's important."""

import os
import threading
import time
from collections import deque
from typing import Dict, List

from core.config import config
from modules.automation.tools import P, PermissionLevel, Tool, ToolError
from modules.notifications import reader


class Inbox:
    """What arrived while SAINT was watching, and what you've already heard."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: deque = deque(maxlen=50)
        self.read_up_to = 0

    def add(self, n: Dict):
        with self._lock:
            self._items.append(n)

    def unread(self) -> List[Dict]:
        with self._lock:
            return [n for n in self._items if n["order"] > self.read_up_to]


inbox = Inbox()


def read_recent(limit: int = 5, only_new: bool = True):
    items = inbox.unread() if only_new else []
    if not items:
        # Not watching (or nothing new): ask Windows for the last hour's toasts.
        cutoff = time.time() - 3600
        items = [n for n in reader.read(inbox.read_up_to if only_new else 0, limit=40) if n["at"] >= cutoff]
    if not items:
        return {"count": 0, "summary": "No new notifications."}
    items = items[-limit:]
    inbox.read_up_to = max([inbox.read_up_to] + [n["order"] for n in items])
    said = "; ".join(reader.spoken(n) for n in items)
    return {"count": len(items), "items": items,
            "summary": f"{len(items)} notification{'s' if len(items) != 1 else ''}: {said}."}


def clear():
    inbox.read_up_to = max(inbox.read_up_to, reader.latest_order())
    try:
        os.startfile("ms-actioncenter:")
    except OSError:
        pass
    return {"summary": "Marked them as read. I opened the notification centre so you can clear them there — "
                       "I never delete Windows' notifications myself."}


def set_filter(important_only: bool = True, allow: str = "", deny: str = "", announce: str = ""):
    changed = []
    if announce in ("on", "off"):
        from core.module_manager import module_manager
        config.set("modules.notifications", announce == "on")
        module_manager.set_enabled("notifications", announce == "on")
        changed.append("I'll read new notifications out loud" if announce == "on"
                       else "I'll stop reading notifications out loud")
    if allow:
        lst = [a for a in config.get("notifications.allow", []) or [] if a.lower() != allow.lower()] + [allow]
        config.set("notifications.allow", lst)
        config.set("notifications.deny", [d for d in config.get("notifications.deny", []) or []
                                          if d.lower() != allow.lower()])
        changed.append(f"I'll always tell you about {allow}")
    if deny:
        lst = [d for d in config.get("notifications.deny", []) or [] if d.lower() != deny.lower()] + [deny]
        config.set("notifications.deny", lst)
        config.set("notifications.allow", [a for a in config.get("notifications.allow", []) or []
                                           if a.lower() != deny.lower()])
        changed.append(f"I won't mention {deny} notifications")
    if not (allow or deny or announce):
        config.set("notifications.important_only", bool(important_only))
        changed.append("I'll only mention important notifications" if important_only
                       else "I'll mention every notification")
    return {"summary": " and ".join(changed) + "."}


def _available():
    return (True, "") if os.path.exists(reader.db_path()) else \
        (False, "I can't find Windows' notification list on this PC.")


def register_notification_tools(registry):
    tools = [
        # Not offered to the LLM: notifications can hold private messages.
        Tool("notifications.read", "Read recent Windows notifications aloud", {}, PermissionLevel.LOW, read_recent,
             parameters={"limit": P("integer", required=False, default=5, minimum=1, maximum=20),
                         "only_new": P("boolean", required=False, default=True)}, category="notifications"),
        Tool("notifications.clear", "Mark notifications as read and open the notification centre", {},
             PermissionLevel.LOW, clear, parameters={}, category="notifications"),
        Tool("notifications.set_filter", "Choose which notifications SAINT announces", {},
             PermissionLevel.LOW, set_filter,
             parameters={"important_only": P("boolean", required=False, default=True),
                         "allow": P("string", required=False, default=""),
                         "deny": P("string", required=False, default=""),
                         "announce": P("string", required=False, default="", enum=["", "on", "off"])},
             category="notifications"),
    ]
    for t in tools:
        t.availability = _available
        registry.register(t)

"""
modules/link/context_feed.py

What you were just doing on your *other* devices, so this one keeps up.

    on the iPhone, a minute ago:  "remind me to call mom at 5"  ->  "Okay, reminder set…"
    now, on the PC:               "what did I just ask you to remember?"   ✓

Each device pushes a one-line note for every turn it handles; the receiving
SAINT keeps the last few for half an hour, in memory only (never written to
disk), and gives them to the language model as facts next to its own
short-term memory (modules/agent/recent.py). It only shares between your own
devices, and only while ``link.share_context`` is on.
"""

import threading
import time
from typing import List

MAX_AGE = 30 * 60
KEEP = 20


class ContextFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._items: List[dict] = []

    def add(self, source: str, user: str, reply: str = "", kind: str = "turn", ts: float = 0.0):
        item = {"source": (source or "another device")[:40], "user": (user or "")[:240],
                "reply": (reply or "")[:240], "kind": kind, "ts": ts or time.time()}
        with self._lock:
            self._items = (self._items + [item])[-KEEP:]

    def recent(self, now: float = 0.0) -> List[dict]:
        now = now or time.time()
        with self._lock:
            return [i for i in self._items if now - i["ts"] <= MAX_AGE]

    def describe(self, now: float = 0.0) -> str:
        now = now or time.time()
        lines = []
        for i in reversed(self.recent(now)):
            mins = max(0, int((now - i["ts"]) // 60))
            when = "just now" if mins < 1 else f"{mins} min ago"
            said = f" and SAINT answered “{i['reply']}”" if i["reply"] else ""
            lines.append(f"- {when}, on {i['source']}: the user said “{i['user']}”{said}")
        return "\n".join(lines)

    def clear(self):
        with self._lock:
            self._items = []


context_feed = ContextFeed()

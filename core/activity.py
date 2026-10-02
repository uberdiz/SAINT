"""
core/activity.py

What SAINT is doing right now — answers "SAINT, what are you doing?".

The foreground job (a multi-step plan or a scene) reports its steps here;
background tasks (scans, extraction) and watchers register themselves too,
with how far along they are ("I'm checking E: for junk — about 40% done").
Nothing here affects behaviour; it only describes it.
"""

import threading
import time
from typing import Dict, List, Optional

from core.events import event_bus, EventType


class Activity:
    def __init__(self):
        self._lock = threading.Lock()
        self._label = ""
        self._steps: List[str] = []
        self._index = -1
        self._started = 0.0
        self._background: Dict[str, str] = {}   # id -> description
        self._progress: Dict[str, float] = {}   # id -> 0..1
        self._stack: List[tuple] = []

    # -- foreground ------------------------------------------------------ #
    def begin(self, label: str, steps: Optional[List[str]] = None):
        with self._lock:
            if self._label:          # nested (a plan inside a scene step): remember the outer job
                self._stack.append((self._label, self._steps, self._index, self._started))
            self._label, self._steps, self._index = label, list(steps or []), -1
            self._started = time.time()
        self._emit()

    def step(self, index: int, name: str = ""):
        with self._lock:
            self._index = index
            if name and 0 <= index < len(self._steps):
                self._steps[index] = name
        self._emit()

    @property
    def foreground(self) -> bool:
        """A multi-step job (plan, scene, lesson) is running right now."""
        with self._lock:
            return bool(self._label)

    def end(self):
        with self._lock:
            if self._stack:
                self._label, self._steps, self._index, self._started = self._stack.pop()
            else:
                self._label, self._steps, self._index = "", [], -1
        self._emit()

    # -- background ------------------------------------------------------ #
    def add_background(self, key: str, description: str):
        with self._lock:
            self._background[key] = description
        self._emit()

    def remove_background(self, key: str):
        with self._lock:
            self._background.pop(key, None)
            self._progress.pop(key, None)
        self._emit()

    def set_progress(self, key: str, fraction: float):
        """How far a background task is (0-1). Not emitted: tasks report often."""
        with self._lock:
            if key in self._background:
                self._progress[key] = max(0.0, min(1.0, float(fraction or 0.0)))

    # -- description ----------------------------------------------------- #
    def snapshot(self) -> dict:
        with self._lock:
            return {"label": self._label, "steps": list(self._steps), "index": self._index,
                    "started": self._started, "background": dict(self._background),
                    "progress": dict(self._progress)}

    def describe(self) -> str:
        s = self.snapshot()
        parts = []
        if s["label"]:
            steps, i = s["steps"], s["index"]
            if steps and 0 <= i < len(steps):
                now = steps[i]
                rest = steps[i + 1:]
                text = f"I'm {now}"
                if rest:
                    text += ", then " + _join(rest)
                parts.append(text + ".")
            else:
                parts.append(f"I'm working on {s['label']}.")
        bg = []
        for key, desc in s["background"].items():
            frac = s["progress"].get(key)
            bg.append(f"{desc} — about {round(frac * 100)}% done" if frac and 0.01 <= frac < 1 else desc)
        if bg:
            parts.append(("In the background: " if parts else "I'm ") + _join(bg) + ".")
        return " ".join(parts) or "Nothing right now — I'm just listening."

    def _emit(self):
        event_bus.emit_event(EventType.ACTIVITY_CHANGED, self.snapshot())


def _join(items: List[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


activity = Activity()

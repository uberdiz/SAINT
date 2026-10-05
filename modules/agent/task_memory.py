"""
modules/agent/task_memory.py

The task SAINT is working on, step by step, so these work:

    "continue what we were doing" / "finish it" / "keep going"
        -> run the steps that didn't happen (after a failure, a "stop", a
           question that was never answered, or a restart)
    "do the same thing for Discord"
        -> repeat the last task with the thing it was about swapped
    "what were we doing?" / "where were we?"
        -> what's done, what failed, what's left

A task is a multi-step request (router.run_plan) or a built-in job such as
"set up my gaming workspace". Each step keeps the words it came from, so it
can be routed again later — the step itself (a closure) can't be saved.
The last few tasks are kept in data/tasks.json.
"""

import json
import logging
import re
import threading
import time
import uuid
from typing import Dict, List, Optional

from core.events import event_bus, EventType
from core.paths import data_path

log = logging.getLogger("saint.tasks")

KEEP = 20
RESUME_MAX_AGE = 6 * 3600          # "continue" picks up tasks from the last few hours

PENDING, RUNNING, DONE, FAILED, SKIPPED = "pending", "running", "done", "failed", "skipped"
OPEN = ("running", "waiting", "failed", "stopped")       # a task that didn't finish


def _title(request: str) -> str:
    t = re.sub(r"^(?:hey\s+)?saint[,.!\s]+", "", (request or "").strip(), flags=re.I)
    return (t[:1].upper() + t[1:])[:80] if t else "a task"


class TaskMemory:
    def __init__(self, path=None):
        self._path = path
        self._lock = threading.RLock()
        self._tasks: List[Dict] = []
        self._loaded = False

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _file(self):
        return self._path or data_path("tasks.json")

    def load(self):
        with self._lock:
            self._loaded = True
            try:
                data = json.loads(self._file().read_text(encoding="utf-8"))
                self._tasks = [t for t in data.get("tasks", []) if isinstance(t, dict)][-KEEP:]
            except FileNotFoundError:
                self._tasks = []
            except Exception:
                log.warning("tasks.load_failed", exc_info=True)
                self._tasks = []
            for t in self._tasks:            # SAINT restarted mid-task: it stopped there
                if t.get("status") == "running":
                    t["status"] = "stopped"
                    for s in t.get("steps", []):
                        if s.get("status") == RUNNING:
                            s["status"] = PENDING

    def _save(self):
        try:
            self._file().write_text(json.dumps({"tasks": self._tasks[-KEEP:]}, indent=1), encoding="utf-8")
        except Exception:
            log.warning("tasks.save_failed", exc_info=True)

    def _ensure(self):
        if not self._loaded:
            self.load()

    def _emit(self, task: Dict):
        self._save()
        event_bus.emit_event(EventType.TASK_STATE, {"id": task["id"], "title": task["title"],
                                                    "status": task["status"]})

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #
    def begin(self, request: str, steps: List[Dict], kind: str = "plan") -> str:
        """steps: [{"label": "opening Spotify", "text": "open spotify"}, ...]"""
        with self._lock:
            self._ensure()
            task = {"id": uuid.uuid4().hex[:10], "title": _title(request), "request": request, "kind": kind,
                    "status": "running", "started": time.time(), "updated": time.time(), "note": "",
                    "steps": [{"label": s.get("label", ""), "text": s.get("text", ""), "part": s.get("part", i),
                               "status": PENDING, "note": ""} for i, s in enumerate(steps)]}
            self._tasks.append(task)
            del self._tasks[:-KEEP]
            self._emit(task)
            return task["id"]

    def step(self, task_id: Optional[str], index: int, status: str, note: str = ""):
        if not task_id:
            return
        with self._lock:
            task = self._get(task_id)
            if task is None or not 0 <= index < len(task["steps"]):
                return
            task["steps"][index].update(status=status, note=(note or "")[:200])
            task["updated"] = time.time()
            if status == RUNNING and task["status"] != "running":
                task["status"] = "running"
            self._save()

    def finish(self, task_id: Optional[str], status: str, note: str = ""):
        """status: done | failed | stopped | waiting (a question to the user is open)."""
        if not task_id:
            return
        with self._lock:
            task = self._get(task_id)
            if task is None:
                return
            task.update(status=status, note=(note or "")[:300], updated=time.time())
            self._emit(task)

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def _get(self, task_id: str) -> Optional[Dict]:
        return next((t for t in reversed(self._tasks) if t["id"] == task_id), None)

    def current(self) -> Optional[Dict]:
        with self._lock:
            self._ensure()
            return dict(self._tasks[-1]) if self._tasks else None

    def resumable(self) -> Optional[Dict]:
        """The latest task that stopped before its end, if it's recent."""
        with self._lock:
            self._ensure()
            for t in reversed(self._tasks):
                if time.time() - t.get("updated", 0) > RESUME_MAX_AGE:
                    return None
                if t["status"] == "done":
                    return None                      # the last thing finished: nothing to continue
                if t["status"] in OPEN and self.remaining(t):
                    return json.loads(json.dumps(t))
            return None

    def last_request(self) -> Optional[Dict]:
        with self._lock:
            self._ensure()
            return dict(self._tasks[-1]) if self._tasks else None

    @staticmethod
    def remaining(task: Dict) -> List[int]:
        return [i for i, s in enumerate(task.get("steps", [])) if s.get("status") in (PENDING, FAILED, RUNNING)]

    def describe(self) -> str:
        t = self.current()
        if not t:
            return "We haven't worked on anything with steps yet."
        done = [s["label"] for s in t["steps"] if s["status"] == DONE]
        failed = [s for s in t["steps"] if s["status"] == FAILED]
        left = [s["label"] for s in t["steps"] if s["status"] in (PENDING, RUNNING)]
        head = {"running": "I'm working on", "done": "We finished", "waiting": "I'm waiting on you for",
                "failed": "We were doing", "stopped": "We were doing"}.get(t["status"], "We were doing")
        parts = [f"{head}: {t['title']}."]
        if done:
            parts.append("Done: " + _join(done) + ".")
        for s in failed:
            parts.append(f"{s['label'][:1].upper() + s['label'][1:]} didn't work" +
                         (f" — {s['note']}" if s.get("note") else "") + ".")
        if left and t["status"] != "done":
            parts.append("Still to do: " + _join(left) + ".")
            parts.append("Say “continue” and I'll pick it up.")
        return " ".join(parts)


def _join(items: List[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


task_memory = TaskMemory()

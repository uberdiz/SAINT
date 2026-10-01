"""
modules/learning/habits.py

Routines SAINT notices by itself. It reads the history of what the user asked
(data/history.jsonl) for two commands that keep following each other —

    "open roblox"  ...a minute later...  "turn it down"      (on 3+ different days)

— and suggests making them one routine: next time "open roblox" also turns
it down. A suggestion is only a suggestion: it shows on the Learned page and
as a quiet chat line, and nothing changes until the user says "make that a
routine" or presses Accept. An accepted routine is an ordinary learned skill
(or an extra scene step), so it can be edited or forgotten like any other.

Deterministic counting, no model calls, nothing leaves the PC.
"""

import json
import logging
import os
import threading
import time
import uuid
from collections import Counter, defaultdict
from typing import Dict, List, Optional

from core.config import config
from core.events import EventType, event_bus
from core.paths import data_path
from modules.learning.skills import norm, skills

log = logging.getLogger("saint.learning")

FOLLOW_SEC = 180           # B has to come within this long after A
MIN_TIMES = 3              # ...this many times
MIN_DAYS = 2               # ...on this many different days
MIN_SHARE = 0.5            # ...and after at least half of the times A was said
SCAN_EVERY = 600           # seconds between scans

# Tools that only look or react — never the second half of a habit (a "skip"
# after "play X" is about the song, not a routine).
_NOT_HABITS = ("screen.", "spotify.current", "spotify.queue_list", "spotify.next", "spotify.previous",
               "spotify.like", "spotify.dislike", "spotify.not_mood", "spotify.recently", "system.health",
               "system.processes", "system.startup_apps", "memory.", "web.", "files.search", "learning.",
               "desktop.describe", "desktop.find", "vision.")


class HabitMiner:
    def __init__(self, path: Optional[str] = None, history_path: Optional[str] = None):
        self._path = path
        self._history_path = history_path
        self._lock = threading.Lock()
        self._last_scan = 0.0
        self._scanning = False

    @property
    def path(self) -> str:
        return self._path or data_path("routine_suggestions.json")

    @property
    def history_path(self) -> str:
        return self._history_path or data_path("history.jsonl")

    # ------------------------------------------------------------------ #
    def _load(self) -> List[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _write(self, items: List[dict]):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    def all(self) -> List[dict]:
        with self._lock:
            return self._load()

    def pending(self) -> List[dict]:
        return [s for s in self.all() if s.get("status") == "new"]

    def latest(self) -> Optional[dict]:
        p = self.pending()
        return max(p, key=lambda s: s.get("created", 0)) if p else None

    # ------------------------------------------------------------------ #
    def _actions(self, days: int = 30) -> List[dict]:
        """Successful requests that did something, oldest first."""
        since = time.time() - days * 86400
        out = []
        try:
            with open(self.history_path, encoding="utf-8") as f:
                lines = f.readlines()[-3000:]
        except OSError:
            return out
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            tools = e.get("tools") or []
            said = norm(e.get("user") or "").strip(" .!?")
            if not said or e.get("ts", 0) < since or not tools or not all(t.get("ok") for t in tools):
                continue
            names = [t.get("tool", "") for t in tools]
            out.append({"at": float(e["ts"]), "said": said, "tools": names,
                        "habit": not any(n.startswith(_NOT_HABITS) for n in names)})
        return sorted(out, key=lambda a: a["at"])

    def find(self) -> List[dict]:
        """Pairs (when, then) the user keeps saying one after the other."""
        acts = self._actions()
        said_count: Counter = Counter(a["said"] for a in acts)
        pairs: Dict[tuple, set] = defaultdict(set)       # (A, B) -> {times A was followed by B}
        for i, a in enumerate(acts):
            if not a["habit"]:
                continue
            for b in acts[i + 1:i + 4]:
                if b["at"] - a["at"] > FOLLOW_SEC:
                    break
                if b["said"] == a["said"]:
                    continue
                if b["habit"]:
                    pairs[(a["said"], b["said"])].add(a["at"])
                break                                    # only what came right after
        found = []
        for (when, then), times in pairs.items():
            days = {time.strftime("%Y-%m-%d", time.localtime(t)) for t in times}
            if len(times) >= MIN_TIMES and len(days) >= MIN_DAYS and len(times) / said_count[when] >= MIN_SHARE \
                    and skills.learnable(when, [when, then]):
                found.append({"when": when, "then": then, "times": len(times), "days": len(days)})
        return sorted(found, key=lambda f: f["times"], reverse=True)

    def _already(self, when: str, then: str) -> bool:
        sk = skills.match(when)
        if sk is not None and norm(then) in [norm(s) for s in sk.steps]:
            return True
        try:
            from modules.automation.scenes import scenes
            sc = scenes.match(when)
        except Exception:
            sc = None
        return sc is not None and norm(then) in [norm(s) for s in sc.steps]

    def scan(self) -> List[dict]:
        """Look for new habits; returns the suggestions added."""
        if not config.get("learning.notice_habits", True):
            return []
        found = self.find()
        new = []
        with self._lock:
            items = self._load()
            known = {(s["when"], s["then"]) for s in items}
            for f in found:
                if (f["when"], f["then"]) in known or self._already(f["when"], f["then"]):
                    continue
                s = {"id": uuid.uuid4().hex[:8], "when": f["when"], "then": f["then"], "times": f["times"],
                     "days": f["days"], "status": "new", "created": time.time()}
                items.append(s)
                new.append(s)
            if new:
                self._write(items[-200:])
        for s in new:
            log.info("learning.habit_noticed when=%r then=%r times=%d days=%d", s["when"], s["then"],
                     s["times"], s["days"])
            event_bus.emit_event(EventType.UI_CHAT_RENDER, {
                "turn_id": 0, "role": "assistant", "source": "learning",
                "text": f"I noticed that after “{s['when']}” you usually say “{s['then']}”. Say “make that a "
                        f"routine” and I'll do both whenever you say “{s['when']}” — or change it on the "
                        f"Learned page."})
        if new:
            event_bus.emit_event(EventType.AUTOMATION_UPDATED, {"suggestions": len(new)})
        return new

    def maybe_scan(self):
        """Cheap to call after every request: scans at most every SCAN_EVERY seconds, in the background."""
        now = time.time()
        if self._scanning or now - self._last_scan < SCAN_EVERY:
            return
        self._last_scan, self._scanning = now, True

        def go():
            try:
                self.scan()
            except Exception:
                log.exception("learning.habit_scan_failed")
            finally:
                self._scanning = False
        threading.Thread(target=go, daemon=True, name="habit-scan").start()

    # ------------------------------------------------------------------ #
    def _set_status(self, sid: str, status: str) -> Optional[dict]:
        with self._lock:
            items = self._load()
            hit = next((s for s in items if s["id"] == sid), None)
            if hit is not None:
                hit["status"] = status
                self._write(items)
        return hit

    def accept(self, sid: str, then: str = "") -> str:
        """Make the suggestion a routine. ``then`` overrides what it adds (edited)."""
        s = next((x for x in self.all() if x["id"] == sid), None)
        if s is None:
            return "That suggestion is gone."
        then = (then or s["then"]).strip()
        try:
            from modules.automation.scenes import scenes
            sc = scenes.match(s["when"])
        except Exception:
            sc = None
        if sc is not None:
            sc.steps = list(sc.steps) + [then]
            scenes.save(sc)
            where = f"the {sc.name} scene"
        else:
            sk = skills.match(s["when"])
            steps = (list(sk.steps) if sk is not None else [s["when"]]) + [then]
            if skills.learn(s["when"], steps, "noticed") is None:
                return f"I couldn't make a routine out of “{s['when']}”."
            where = "a routine"
        self._set_status(sid, "accepted")
        log.info("learning.habit_accepted when=%r then=%r", s["when"], then)
        return f"Done — “{s['when']}” now also does “{then}” ({where}). Change it any time on the Learned page."

    def dismiss(self, sid: str) -> str:
        s = self._set_status(sid, "dismissed")
        return f"Okay, I'll leave “{s['when']}” as it is." if s else "That suggestion is gone."


habits = HabitMiner()

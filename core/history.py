"""
core/history.py

A private, local log of what you asked SAINT and what it did: one JSON object
per line in data/history.jsonl. It is written on this machine only (data/ is
git-ignored) and powers the History page. Turn it off with history.enabled.

    {"ts": 1758650000.1, "source": "voice", "user": "skip this song",
     "reply": "Skipped it.", "tools": [{"tool": "spotify.next", "ok": true, "ms": 212}],
     "ms": 1840}

source: voice | typed | hotword | scene | automation
"""

import json
import logging
import threading
import time
from pathlib import Path

from core.config import config
from core.events import event_bus, EventType
from core.paths import data_path

log = logging.getLogger("saint.history")


class History:
    def __init__(self, path=None):
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._sources = {}        # stt session id -> source
        self._turns = {}          # turn id -> record being built
        self._current = None
        self._count = None
        event_bus.subscribe(self._on_event)

    @property
    def path(self) -> Path:
        return self._path or data_path("history.jsonl")

    # ------------------------------------------------------------------ #
    def _on_event(self, ev):
        if not config.get("history.enabled", True):
            return
        t, p = ev.type, ev.payload or {}
        if t == EventType.VOICE_STT_FINAL:
            src = p.get("source")
            self._sources[p.get("session_id")] = ("hotword" if src == "hotword" else
                                                  "typed" if src in ("text", "inject_text") else "voice")
            if len(self._sources) > 64:
                self._sources.pop(next(iter(self._sources)))
        elif t == EventType.CONVERSATION_TURN_START:
            self._turns[p.get("turn_id")] = {
                "ts": ev.timestamp, "source": self._sources.pop(p.get("session_id"), "voice"),
                "user": p.get("text", ""), "tools": []}
            self._current = p.get("turn_id")
            while len(self._turns) > 32:          # interrupted turns never end
                self._turns.pop(next(iter(self._turns)))
        elif t in (EventType.TOOL_COMPLETED, EventType.TOOL_FAILED):
            rec = self._turns.get(self._current)
            if rec is not None:
                rec["tools"].append({"tool": p.get("tool", ""), "ok": t == EventType.TOOL_COMPLETED,
                                     "ms": p.get("duration_ms", 0)})
        elif t == EventType.CONVERSATION_TURN_END:
            rec = self._turns.pop(p.get("turn_id"), None)
            if rec is not None:
                rec["reply"] = p.get("response", "")
                rec["ms"] = round((ev.timestamp - rec["ts"]) * 1000)
                self.append(rec)
        elif t == EventType.AUTOMATION_TRIGGERED:
            self.append({"ts": ev.timestamp, "source": "scene" if p.get("kind") == "scene" else "automation",
                         "user": p.get("title", ""), "reply": p.get("result", ""), "tools": []})

    # ------------------------------------------------------------------ #
    def append(self, rec: dict):
        rec.setdefault("ts", time.time())
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as e:
                log.warning("history.write_failed %s", e)
                return
            if self._count is None:
                self._count = len(self._read_lines())
            else:
                self._count += 1
            limit = int(config.get("history.max_entries", 5000) or 5000)
            if self._count > limit + 500:
                self._trim(limit)
        event_bus.emit_event(EventType.HISTORY_APPENDED, {"source": rec.get("source")})

    def _read_lines(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return f.readlines()
        except OSError:
            return []

    def _trim(self, keep: int):
        lines = self._read_lines()[-keep:]
        tmp = str(self.path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
        Path(tmp).replace(self.path)
        self._count = len(lines)

    def read(self, limit: int = 5000) -> list:
        """Entries oldest → newest (at most ``limit``, newest kept)."""
        with self._lock:
            lines = self._read_lines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def clear(self):
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self._count = 0
        event_bus.emit_event(EventType.HISTORY_APPENDED, {"cleared": True})


history = History()

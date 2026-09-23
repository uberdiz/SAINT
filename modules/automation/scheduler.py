"""
modules/automation/scheduler.py

Persistent automations: reminders, timers, recurring reminders and scheduled
commands. Stored in data/memory/automations.db, executed by a background
thread that does not depend on the UI.

Automation kinds
    reminder   notify (tray/UI) + speak the message
    command    run an utterance through the agent at the scheduled time
               ("every morning at 8 play my morning playlist"), using the same
               tools and permission policy as a spoken command

Optional condition (checked right before running; the occurrence is skipped
if it doesn't hold):
    {"type": "spotify_playing"} | {"type": "spotify_not_playing"}
    {"type": "app_running", "value": "discord"}

Startup behaviour
    * one-shot reminders missed while SAINT was closed fire once on startup
      (marked as missed) if they are within automation.missed_grace_hours;
      older ones are marked "missed" without firing
    * recurring automations skip missed occurrences and resume on schedule
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional

from core.config import config
from core.events import event_bus, EventType
from core.paths import data_path
from modules.automation.timeparse import next_run, describe

log = logging.getLogger("saint.automation")

ACTIVE, PAUSED, COMPLETED, CANCELLED, MISSED, FAILED = (
    "active", "paused", "completed", "cancelled", "missed", "failed")


@dataclass
class Automation:
    id: str
    title: str
    kind: str                       # "reminder" | "command"
    message: str                    # reminder text or command utterance
    schedule: Dict[str, Any]
    condition: Optional[Dict[str, Any]] = None
    status: str = ACTIVE
    next_run: Optional[float] = None
    last_run: Optional[float] = None
    run_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_error: str = ""
    last_result: str = ""

    @property
    def recurring(self) -> bool:
        return self.schedule.get("type") != "once"

    def describe(self) -> str:
        return describe(self.schedule)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["when"] = self.describe()
        d["recurring"] = self.recurring
        return d


class AutomationStore:
    def __init__(self, path: Optional[str] = None):
        self.path = str(path or data_path("memory", "automations.db"))
        self._lock = threading.Lock()
        with self._lock, self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS automations (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    status TEXT NOT NULL,
                    next_run REAL
                )""")

    def _connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, a: Automation):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("INSERT OR REPLACE INTO automations(id,data,status,next_run) VALUES(?,?,?,?)",
                             (a.id, json.dumps(asdict(a)), a.status, a.next_run))
                conn.commit()
            finally:
                conn.close()

    def all(self) -> List[Automation]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT data FROM automations").fetchall()
            finally:
                conn.close()
        out = []
        for r in rows:
            try:
                out.append(Automation(**json.loads(r["data"])))
            except Exception:
                log.warning("automation.corrupt_row skipped")
        return out

    def get(self, aid: str) -> Optional[Automation]:
        return next((a for a in self.all() if a.id == aid), None)

    def delete(self, aid: str):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM automations WHERE id=?", (aid,))
                conn.commit()
            finally:
                conn.close()


class Scheduler:
    def __init__(self):
        self._store: Optional[AutomationStore] = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Hooks supplied by core.runtime
        self.announce: Optional[Callable[[str], None]] = None
        self.run_command: Optional[Callable[[str], str]] = None

    @property
    def store(self) -> AutomationStore:
        if self._store is None:
            self._store = AutomationStore()
        return self._store

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._catch_up()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()
        log.info("scheduler.started active=%d", len(self.list(active_only=True)))

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _catch_up(self):
        now = time.time()
        grace = float(config.get("automation.missed_grace_hours", 12)) * 3600
        for a in self.store.all():
            if a.status != ACTIVE or a.next_run is None or a.next_run > now:
                continue
            if not a.recurring:
                if now - a.next_run <= grace:
                    log.info("automation.missed_fire id=%s title=%r", a.id, a.title)
                    self._execute(a, missed=True)
                else:
                    a.status = MISSED
                    self.store.save(a)
                    self._changed(a, EventType.AUTOMATION_UPDATED)
            else:
                a.next_run = next_run(a.schedule, now)
                self.store.save(a)

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #
    def create(self, kind: str, message: str, schedule: Dict[str, Any], title: str = "",
               condition: Optional[Dict[str, Any]] = None) -> Automation:
        if kind not in ("reminder", "command"):
            raise ValueError("kind must be 'reminder' or 'command'")
        nr = next_run(schedule, time.time() - 1)
        if nr is None:
            raise ValueError("That time is already in the past.")
        a = Automation(id=uuid.uuid4().hex[:8], title=title or message[:60], kind=kind,
                       message=message, schedule=schedule, condition=condition, next_run=nr)
        self.store.save(a)
        log.info("automation.created id=%s kind=%s when=%r title=%r", a.id, kind, a.describe(), a.title)
        self._changed(a, EventType.AUTOMATION_CREATED)
        self._wake.set()
        return a

    def list(self, active_only: bool = False) -> List[Automation]:
        items = self.store.all()
        if active_only:
            items = [a for a in items if a.status in (ACTIVE, PAUSED)]
        return sorted(items, key=lambda a: (a.status != ACTIVE, a.next_run or 9e18))

    def cancel(self, aid: str) -> Optional[Automation]:
        a = self.store.get(aid)
        if not a:
            return None
        a.status = CANCELLED
        a.next_run = None
        self.store.save(a)
        log.info("automation.cancelled id=%s title=%r", a.id, a.title)
        self._changed(a, EventType.AUTOMATION_CANCELLED)
        return a

    def delete(self, aid: str) -> bool:
        a = self.store.get(aid)
        if not a:
            return False
        self.store.delete(aid)
        self._changed(a, EventType.AUTOMATION_CANCELLED, deleted=True)
        return True

    def set_paused(self, aid: str, paused: bool) -> Optional[Automation]:
        a = self.store.get(aid)
        if not a or a.status not in (ACTIVE, PAUSED):
            return None
        a.status = PAUSED if paused else ACTIVE
        if not paused:
            a.next_run = next_run(a.schedule, time.time())
            if a.next_run is None:
                a.status = MISSED
        self.store.save(a)
        self._changed(a, EventType.AUTOMATION_UPDATED)
        self._wake.set()
        return a

    def run_now(self, aid: str) -> Optional[Automation]:
        a = self.store.get(aid)
        if a:
            self._execute(a, manual=True)
        return a

    def find(self, query: str) -> List[Automation]:
        q = (query or "").lower().strip()
        active = self.list(active_only=True)
        if not q:
            return active
        words = [w for w in q.split() if len(w) > 2 and w not in {"the", "reminder", "my", "about", "for"}]
        hits = [a for a in active if all(w in (a.title + " " + a.message).lower() for w in words)]
        return hits

    def _changed(self, a: Automation, etype: str, **extra):
        payload = {"id": a.id, "title": a.title, "status": a.status, "next_run": a.next_run,
                   "when": a.describe()}
        payload.update(extra)
        event_bus.emit_event(etype, payload)

    # ------------------------------------------------------------------ #
    # Execution loop
    # ------------------------------------------------------------------ #
    def _loop(self):
        while not self._stop.is_set():
            now = time.time()
            due, soonest = [], None
            try:
                for a in self.store.all():
                    if a.status != ACTIVE or a.next_run is None:
                        continue
                    if a.next_run <= now:
                        due.append(a)
                    elif soonest is None or a.next_run < soonest:
                        soonest = a.next_run
            except Exception:
                log.exception("scheduler.read_failed")
            for a in sorted(due, key=lambda x: x.next_run or 0):
                self._execute(a)
            wait = 30.0 if soonest is None else max(0.2, min(30.0, soonest - time.time()))
            self._wake.wait(wait)
            self._wake.clear()

    def _condition_holds(self, cond: Optional[Dict[str, Any]]) -> (bool, str):
        if not cond:
            return True, ""
        ctype = cond.get("type")
        try:
            if ctype in ("spotify_playing", "spotify_not_playing"):
                from core.module_manager import module_manager
                sp = module_manager.get("spotify")
                playing = False
                if sp and sp.enabled and sp.is_connected():
                    state = sp.client.playback() or {}
                    playing = bool(state.get("is_playing"))
                ok = playing if ctype == "spotify_playing" else not playing
                return ok, f"Spotify is {'playing' if playing else 'not playing'}"
            if ctype == "app_running":
                import psutil
                name = str(cond.get("value", "")).lower()
                running = any(name in (p.info.get("name") or "").lower() for p in psutil.process_iter(["name"]))
                return running, f"{name} {'is' if running else 'is not'} running"
        except Exception as e:
            return False, f"couldn't check condition: {e}"
        return True, ""

    def _execute(self, a: Automation, missed: bool = False, manual: bool = False):
        ok, why = self._condition_holds(a.condition)
        now = time.time()
        if not ok:
            log.info("automation.skipped id=%s condition=%s (%s)", a.id, a.condition, why)
            a.last_result = f"skipped: {why}"
        else:
            try:
                if a.kind == "reminder":
                    text = a.message if not missed else f"{a.message} (missed while SAINT was closed)"
                    event_bus.emit_event(EventType.NOTIFY, {"title": "SAINT reminder", "message": text,
                                                            "automation_id": a.id})
                    if self.announce and config.get("automation.speak_reminders", True):
                        spoken = f"Reminder: {a.message}." if not missed else \
                            f"You missed a reminder while I was closed: {a.message}."
                        self.announce(spoken)
                    a.last_result = "delivered"
                else:
                    if not self.run_command:
                        raise RuntimeError("the agent isn't running")
                    result = self.run_command(a.message)
                    a.last_result = (result or "")[:200]
                    event_bus.emit_event(EventType.NOTIFY, {"title": "SAINT automation",
                                                            "message": f"{a.title}: {a.last_result}"})
                a.last_error = ""
                a.run_count += 1
                a.last_run = now
                event_bus.emit_event(EventType.AUTOMATION_TRIGGERED, {
                    "id": a.id, "title": a.title, "kind": a.kind, "missed": missed, "manual": manual})
                log.info("automation.triggered id=%s kind=%s title=%r missed=%s", a.id, a.kind, a.title, missed)
            except Exception as e:
                a.last_error = str(e)
                log.exception("automation.failed id=%s", a.id)
                event_bus.emit_event(EventType.AUTOMATION_FAILED, {"id": a.id, "title": a.title, "error": str(e)})
        if not manual:
            if a.recurring:
                a.next_run = next_run(a.schedule, now + 0.5)
            else:
                a.status = COMPLETED if not a.last_error else FAILED
                a.next_run = None
        self.store.save(a)
        self._changed(a, EventType.AUTOMATION_UPDATED)


scheduler = Scheduler()

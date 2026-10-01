"""
modules/link/sync.py

What one of your devices learns, the others know.

Syncable kinds (each an ``Adapter`` over one of SAINT's existing stores):

    memory     facts and preferences, told or learned       (modules/memory)
    skill      things SAINT learned to do                    (modules/learning/skills.py)
    alias      "when I say the lab, I mean ..."              (modules/agent/aliases.py)
    scene      named routines (steps, phrase; not the schedule)
    reminder   reminders and timers (not scheduled commands: those act on one PC)
    settings   language preferences

How it works
    * Every item has a stable uid and, in the local mirror (data/link/sync.db),
      the last content hash, timestamp and origin device we know of. ``scan()``
      compares the stores with the mirror: a changed hash is a local edit, a
      missing item is a deletion (kept as a tombstone) — so no store needs to
      announce its changes, and the stores stay exactly as they are.
    * Timestamps are a hybrid logical clock (never runs backwards, follows the
      largest timestamp seen), so a phone with a wrong clock can't overwrite
      everything.
    * Two devices exchange manifests (uid -> timestamp, origin), then only the
      items the other side is missing or has older. Last writer wins per item;
      ties break on the device id.
    * Applying a remote item records its content hash *as stored locally*, so
      it isn't mistaken for a local edit and echoed back.
"""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.paths import data_path

log = logging.getLogger("saint.link.sync")

TOMBSTONE_DAYS = 180


def canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(data: Any) -> str:
    return hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------- #
# Adapters
# ---------------------------------------------------------------------- #
class Adapter:
    kind = ""

    def snapshot(self) -> Dict[str, dict]:
        """uid -> data for everything that exists locally now."""
        raise NotImplementedError

    def apply_batch(self, changes: List[Tuple[str, Optional[dict]]]) -> List[str]:
        """Write remote changes ((uid, data) or (uid, None) to delete). Returns
        the uids that were refused (they're then recorded as deleted here)."""
        raise NotImplementedError

    def fingerprint(self, data: dict) -> str:
        """What counts as a change. Default: any field."""
        return fingerprint(data)


class MemoryAdapter(Adapter):
    kind = "memory"
    _VOLATILE = ("confidence",)

    def _entries(self):
        from modules.memory.service import memory_service
        return memory_service, memory_service.all()

    def snapshot(self) -> Dict[str, dict]:
        service, entries = self._entries()
        out = {}
        for e in entries:
            meta = dict(e.metadata)
            uid = meta.get("uid")
            if not uid:
                uid = uuid.uuid4().hex[:16]
                meta["uid"] = uid
                service.db.update(e.id, metadata=meta)
            out[uid] = {"content": e.content, "key": meta.get("key", ""), "value": meta.get("value", ""),
                        "category": meta.get("category", "fact"), "how": meta.get("how", "told"),
                        "tags": list(e.tags), "confidence": round(e.confidence, 2)}
        return out

    def fingerprint(self, data: dict) -> str:
        return fingerprint({k: v for k, v in data.items() if k not in self._VOLATILE})

    def apply_batch(self, changes):
        from core.events import event_bus, EventType
        from modules.memory.service import _CATEGORY_TYPES, memory_service
        service, entries = self._entries()
        by_uid = {e.metadata.get("uid"): e for e in entries if e.metadata.get("uid")}
        for uid, data in changes:
            existing = by_uid.get(uid)
            if data is None:
                if existing is not None:
                    service.db.delete(existing.id)
                    event_bus.emit_event(EventType.MEMORY_DELETED, {"id": existing.id, "link": True})
                continue
            category = data.get("category") if data.get("category") in _CATEGORY_TYPES else "fact"
            meta = dict(existing.metadata) if existing is not None else {"mentions": 1, "last_seen": time.time()}
            meta.update({"uid": uid, "key": data.get("key", ""), "value": data.get("value", ""),
                         "category": category, "how": data.get("how", "told")})
            confidence = float(data.get("confidence", 1.0))
            tags = list(data.get("tags") or [category])
            if existing is not None:
                service.db.update(existing.id, content=data.get("content", existing.content), metadata=meta,
                                  confidence=confidence, tags=tags)
                event_bus.emit_event(EventType.MEMORY_UPDATED, {"id": existing.id, "link": True})
            else:
                new_id = service.db.store(_CATEGORY_TYPES[category], data.get("content", ""), metadata=meta,
                                          source="link", confidence=confidence, tags=tags)
                event_bus.emit_event(EventType.MEMORY_STORED, {"id": new_id, "link": True})
        return []


class SkillAdapter(Adapter):
    kind = "skill"

    def snapshot(self):
        from modules.learning.skills import skills
        return {s.id: {"phrase": s.phrase, "steps": list(s.steps), "how": s.how, "said": s.said}
                for s in skills.all()}

    def apply_batch(self, changes):
        from modules.learning.skills import Skill, skills
        refused = []
        current = {s.id: s for s in skills.all()}
        for uid, data in changes:
            if data is None:
                if uid in current:
                    skills.forget(uid)
                    current.pop(uid, None)
                continue
            clash = next((s for s in current.values() if s.phrase == data.get("phrase") and s.id != uid), None)
            if clash is not None:
                if clash.id < uid:                    # the same phrase learned twice: the smaller id wins, everywhere
                    refused.append(uid)
                    continue
                skills.forget(clash.id)
                current.pop(clash.id, None)
            old = current.get(uid)
            skill = Skill(data.get("phrase", ""), list(data.get("steps") or []), data.get("how", "shown"),
                          said=data.get("said", ""), id=uid)
            if not skill.phrase or not skill.steps:
                refused.append(uid)
                continue
            if old is not None:
                skill.created, skill.uses, skill.last_used, skill.fails = old.created, old.uses, old.last_used, old.fails
            skills.upsert(skill)
            current[uid] = skill
        return refused


class AliasAdapter(Adapter):
    kind = "alias"

    def snapshot(self):
        from modules.agent.aliases import aliases
        return {k: {"target": v} for k, v in aliases.all().items()}

    def apply_batch(self, changes):
        from modules.agent.aliases import aliases
        for uid, data in changes:
            if data is None:
                aliases.forget(uid)
            else:
                aliases.set(uid, data.get("target", ""))
        return []


class SceneAdapter(Adapter):
    kind = "scene"

    def snapshot(self):
        from modules.automation.scenes import scenes
        return {s.id: {"name": s.name, "steps": list(s.steps), "phrase": s.phrase} for s in scenes.all()}

    def apply_batch(self, changes):
        from modules.automation.scenes import Scene, _norm, scenes
        refused = []
        for uid, data in changes:
            if data is None:
                if scenes.get(uid) is not None:
                    scenes.delete(uid)
                continue
            old = scenes.get(uid)
            scene = old or Scene(data.get("name", ""), [], id=uid)
            scene.name, scene.steps, scene.phrase = data.get("name", ""), list(data.get("steps") or []), data.get("phrase", "")
            try:
                scenes.save(scene)
            except ValueError:
                # A different scene already has this name: identical ones are the same scene; otherwise keep both.
                twin = next((s for s in scenes.all() if _norm(s.name) == _norm(scene.name) and s.id != uid), None)
                if twin is not None and twin.steps == scene.steps and twin.phrase == scene.phrase:
                    if twin.id < uid:
                        refused.append(uid)
                        continue
                    scenes.delete(twin.id)
                    try:
                        scenes.save(scene)
                        continue
                    except ValueError:
                        pass
                scene.name = f"{scene.name} (synced)"
                try:
                    scenes.save(scene)
                except ValueError:
                    refused.append(uid)
        return refused


class ReminderAdapter(Adapter):
    kind = "reminder"
    def snapshot(self):
        from modules.automation.scheduler import scheduler
        cutoff = time.time() - 30 * 86400
        out = {}
        for a in scheduler.store.all():
            if a.kind != "reminder":
                continue
            if a.status not in ("active", "paused") and (a.last_run or a.created_at) < cutoff:
                continue
            out[a.id] = {"title": a.title, "message": a.message, "schedule": a.schedule, "status": a.status,
                         "last_run": round(a.last_run or 0)}
        return out

    def fingerprint(self, data):
        return fingerprint({k: v for k, v in data.items() if k not in ("last_run",)} | {"ran": bool(data.get("last_run"))})

    def apply_batch(self, changes):
        from core.events import event_bus, EventType
        from modules.automation.scheduler import ACTIVE, Automation, MISSED, scheduler
        from modules.automation.timeparse import next_run
        for uid, data in changes:
            if data is None:
                scheduler.delete(uid)
                continue
            a = scheduler.store.get(uid) or Automation(id=uid, title=data.get("title", ""), kind="reminder",
                                                       message=data.get("message", ""), schedule=data.get("schedule") or {})
            a.title, a.message, a.schedule = data.get("title", a.title), data.get("message", a.message), \
                data.get("schedule") or a.schedule
            a.status = data.get("status", ACTIVE)
            a.last_run = float(data.get("last_run") or 0) or a.last_run
            if a.status == ACTIVE:
                a.next_run = next_run(a.schedule, time.time() - 1)
                if a.next_run is None:
                    a.status = MISSED
            else:
                a.next_run = None
            scheduler.store.save(a)
            event_bus.emit_event(EventType.AUTOMATION_UPDATED, {"id": a.id, "title": a.title, "link": True})
        scheduler._wake.set()
        return []


class SettingsAdapter(Adapter):
    kind = "settings"
    KEYS = ("preferred", "mixed_mode", "reply_in_user_language")

    def snapshot(self):
        from core.config import config
        return {"language": {k: config.get(f"language.{k}") for k in self.KEYS}}

    def apply_batch(self, changes):
        from core.config import config
        for uid, data in changes:
            if uid == "language" and isinstance(data, dict):
                for k in self.KEYS:
                    if k in data:
                        config.set(f"language.{k}", data[k], persist=False)
                config.save()
        return []


def default_adapters() -> List[Adapter]:
    return [MemoryAdapter(), SkillAdapter(), AliasAdapter(), SceneAdapter(), ReminderAdapter(), SettingsAdapter()]


# ---------------------------------------------------------------------- #
# The mirror
# ---------------------------------------------------------------------- #
class Mirror:
    def __init__(self, path: Optional[str] = None):
        self.path = path or str(data_path("link", "sync.db"))
        self._lock = threading.RLock()
        self._tx: Optional[sqlite3.Connection] = None
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with self._lock, self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS items (kind TEXT, uid TEXT, ts INTEGER, origin TEXT, hash TEXT, "
                      "deleted INTEGER, data TEXT, PRIMARY KEY (kind, uid))")
            c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def tx(self):
        """Many writes, one commit (a thousand-item sync shouldn't fsync a thousand times)."""
        with self._lock:
            conn = self._conn()
            self._tx = conn
            try:
                yield
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._tx = None
                conn.close()

    def _run(self, sql, params=()):
        with self._lock:
            if self._tx is not None:
                return self._tx.execute(sql, params).fetchall()
            conn = self._conn()
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return cur.fetchall()
            finally:
                conn.close()

    def get(self, kind: str, uid: str) -> Optional[dict]:
        rows = self._run("SELECT * FROM items WHERE kind=? AND uid=?", (kind, uid))
        return dict(rows[0]) if rows else None

    def all(self, kind: Optional[str] = None) -> List[dict]:
        rows = self._run("SELECT * FROM items" + (" WHERE kind=?" if kind else ""), (kind,) if kind else ())
        return [dict(r) for r in rows]

    def put(self, kind: str, uid: str, ts: int, origin: str, hash_: str, deleted: bool, data: Optional[dict]):
        self._run("INSERT OR REPLACE INTO items(kind,uid,ts,origin,hash,deleted,data) VALUES(?,?,?,?,?,?,?)",
                  (kind, uid, int(ts), origin, hash_, 1 if deleted else 0,
                   None if data is None else canonical(data)))

    def purge(self, older_than_ms: int):
        self._run("DELETE FROM items WHERE deleted=1 AND ts<?", (older_than_ms,))

    def meta(self, key: str, default: str = "") -> str:
        rows = self._run("SELECT v FROM meta WHERE k=?", (key,))
        return rows[0]["v"] if rows else default

    def set_meta(self, key: str, value: str):
        self._run("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (key, value))


# ---------------------------------------------------------------------- #
# The engine
# ---------------------------------------------------------------------- #
class SyncEngine:
    def __init__(self, device_id: str, adapters: Optional[List[Adapter]] = None, mirror: Optional[Mirror] = None):
        self.device_id = device_id
        self.adapters = {a.kind: a for a in (adapters if adapters is not None else default_adapters())}
        self.mirror = mirror or Mirror()
        self._lock = threading.RLock()
        self._clock = int(self.mirror.meta("clock", "0") or 0)

    # -- clock -----------------------------------------------------------
    def tick(self) -> int:
        with self._lock:
            self._clock = max(int(time.time() * 1000), self._clock + 1)
            self.mirror.set_meta("clock", str(self._clock))
            return self._clock

    def observe(self, ts: int):
        with self._lock:
            if ts > self._clock:
                self._clock = ts
                self.mirror.set_meta("clock", str(ts))

    # -- local changes ---------------------------------------------------
    def scan(self) -> int:
        """Record local edits and deletions in the mirror. Returns how many."""
        changed = 0
        with self._lock, self.mirror.tx():
            for kind, adapter in self.adapters.items():
                try:
                    snap = adapter.snapshot()
                except Exception:
                    log.exception("link.sync.snapshot_failed %s", kind)
                    continue
                known = {m["uid"]: m for m in self.mirror.all(kind)}
                for uid, data in snap.items():
                    h = adapter.fingerprint(data)
                    m = known.get(uid)
                    if m is None or m["deleted"] or m["hash"] != h:
                        self.mirror.put(kind, uid, self.tick(), self.device_id, h, False, data)
                        changed += 1
                    elif m["data"] != canonical(data):
                        # volatile fields moved (confidence, last_run): refresh what we'd serve, keep the timestamp
                        self.mirror.put(kind, uid, m["ts"], m["origin"], h, False, data)
                for uid, m in known.items():
                    if uid not in snap and not m["deleted"]:
                        self.mirror.put(kind, uid, self.tick(), self.device_id, "", True, None)
                        changed += 1
            self.mirror.purge(int((time.time() - TOMBSTONE_DAYS * 86400) * 1000))
        return changed

    # -- exchanging ------------------------------------------------------
    def manifest(self) -> dict:
        with self._lock:
            out: Dict[str, dict] = {}
            for m in self.mirror.all():
                out.setdefault(m["kind"], {})[m["uid"]] = [m["ts"], m["origin"], m["deleted"]]
            return {"clock": self._clock, "items": out}

    def diff(self, remote: dict) -> Tuple[List[list], List[dict]]:
        """(what I want from them, items I have that are newer than theirs)."""
        self.observe(int(remote.get("clock", 0) or 0))
        theirs = remote.get("items") or {}
        want: List[list] = []
        offer: List[dict] = []
        with self._lock:
            mine = {(m["kind"], m["uid"]): m for m in self.mirror.all()}
        for kind, entries in theirs.items():
            if kind not in self.adapters or not isinstance(entries, dict):
                continue
            for uid, meta in entries.items():
                m = mine.get((kind, uid))
                if m is None or (int(meta[0]), str(meta[1])) > (m["ts"], m["origin"]):
                    want.append([kind, uid])
        for (kind, uid), m in mine.items():
            meta = (theirs.get(kind) or {}).get(uid)
            if meta is None or (m["ts"], m["origin"]) > (int(meta[0]), str(meta[1])):
                offer.append(self._item(m))
        return want, offer

    @staticmethod
    def _item(m: dict) -> dict:
        return {"k": m["kind"], "u": m["uid"], "ts": m["ts"], "o": m["origin"], "x": bool(m["deleted"]),
                "d": None if m["deleted"] else json.loads(m["data"]) if m["data"] else None}

    def items_for(self, want: Iterable[list]) -> List[dict]:
        out = []
        for kind, uid in want:
            m = self.mirror.get(kind, uid)
            if m is not None:
                out.append(self._item(m))
        return out

    def apply(self, items: Iterable[dict]) -> int:
        """Apply remote items that are newer than ours. Returns how many changed anything."""
        by_kind: Dict[str, List[dict]] = {}
        with self._lock:
            for it in items:
                try:
                    kind, uid, ts, origin = it["k"], str(it["u"]), int(it["ts"]), str(it["o"])
                except (KeyError, TypeError, ValueError):
                    continue
                if kind not in self.adapters:
                    continue
                self.observe(ts)
                m = self.mirror.get(kind, uid)
                if m is not None and (m["ts"], m["origin"]) >= (ts, origin):
                    continue
                if not it.get("x") and not isinstance(it.get("d"), dict):
                    continue
                by_kind.setdefault(kind, []).append({"uid": uid, "ts": ts, "origin": origin,
                                                     "data": None if it.get("x") else it["d"]})
            applied = 0
            with self.mirror.tx():
                for kind, batch in by_kind.items():
                    adapter = self.adapters[kind]
                    try:
                        refused = set(adapter.apply_batch([(b["uid"], b["data"]) for b in batch]) or [])
                        snap = adapter.snapshot()
                    except Exception:
                        log.exception("link.sync.apply_failed %s", kind)
                        continue
                    for b in batch:
                        uid = b["uid"]
                        if uid in refused:
                            self.mirror.put(kind, uid, self.tick(), self.device_id, "", True, None)
                            continue
                        if b["data"] is None:
                            self.mirror.put(kind, uid, b["ts"], b["origin"], "", True, None)
                        else:
                            stored = snap.get(uid, b["data"])
                            self.mirror.put(kind, uid, b["ts"], b["origin"], adapter.fingerprint(stored), False, stored)
                        applied += 1
        if applied:
            log.info("link.sync.applied %d items", applied)
        return applied

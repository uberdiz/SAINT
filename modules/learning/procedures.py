"""
modules/learning/procedures.py

Procedures: learned skills with what an agent task knows about each step — a label, how to check
it worked, other ways to do it, its risk — plus a version history.

    "set up my coding workspace"  ->  1. open the project C:\\src\\app      check: VS Code running
                                      2. start the dev server for ...      check: port 5173 answers
                                      3. open {url}                         check: the browser shows it
                                      ...

The steps themselves stay in ``skills.json`` (modules/learning/skills.py), so a procedure shows up
and can be edited on Automations › Learned, older SAINT versions still run it as a plain skill,
and forgetting the skill forgets the procedure. The extra knowledge lives in ``procedures.json``,
keyed by the skill's id.

Learning is incremental and validated: a step is only replaced when the replacement is a command
SAINT understands *and* it just worked in a real run; every change is a new version with the
reason, and nothing the model suggests is saved unless it ran and passed its check.
"""

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from core.events import event_bus, EventType
from core.paths import data_path

log = logging.getLogger("saint.learning.procedures")

MAX_HISTORY = 20


@dataclass
class ProcStep:
    action: str
    label: str = ""
    verify: Dict[str, Any] = field(default_factory=dict)
    fallback: List[str] = field(default_factory=list)
    risk: str = "low"
    optional: bool = False
    tool: str = ""                           # a built-in step: replayed with the same tool and arguments
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Procedure:
    id: str                                  # the skill's id
    phrase: str
    goal: str = ""
    steps: List[ProcStep] = field(default_factory=list)
    version: int = 1
    uses: int = 0
    fails: int = 0
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    source: str = "task"                     # task | shown | taught | corrected
    history: List[Dict[str, Any]] = field(default_factory=list)


class ProcedureStore:
    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.RLock()

    @property
    def path(self) -> str:
        return self._path or str(data_path("procedures.json"))

    # ---- storage -----------------------------------------------------------------------------
    def _load(self) -> Dict[str, Procedure]:
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return {}
        out = {}
        for pid, d in (raw.get("procedures", {}) if isinstance(raw, dict) else {}).items():
            try:
                steps = [ProcStep(**{k: v for k, v in s.items() if k in ProcStep.__dataclass_fields__})
                         for s in d.get("steps", []) if isinstance(s, dict)]
                p = Procedure(**{k: v for k, v in d.items() if k in Procedure.__dataclass_fields__ and k != "steps"})
                p.steps = steps
                out[pid] = p
            except TypeError:
                log.warning("procedures.bad_entry %s", pid)
        return out

    def _write(self, procs: Dict[str, Procedure]):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "procedures": {k: asdict(v) for k, v in procs.items()}}, f, indent=1)
        os.replace(tmp, self.path)

    def all(self) -> List[Procedure]:
        with self._lock:
            return list(self._load().values())

    def get(self, pid: str) -> Optional[Procedure]:
        with self._lock:
            return self._load().get(pid)

    # ---- matching ----------------------------------------------------------------------------
    def match(self, text: str) -> Optional[Procedure]:
        """The procedure for ``text`` — through the skill it's attached to, so a skill the user
        forgot or edited on the Automations page is respected."""
        from modules.learning.skills import skills
        skill = skills.match(text)
        if skill is None:
            return None
        with self._lock:
            proc = self._load().get(skill.id)
        if proc is None:
            return None
        if [s.action for s in proc.steps] != list(skill.steps):
            proc = self._resync(proc, skill.steps)        # edited by hand: the user's steps win
        return proc

    def _resync(self, proc: Procedure, actions: List[str]) -> Procedure:
        known = {s.action: s for s in proc.steps}
        proc.steps = [known.get(a) or ProcStep(a) for a in actions]
        proc.version += 1
        proc.history.append({"version": proc.version, "at": time.time(), "change": "edited on the Automations page"})
        del proc.history[:-MAX_HISTORY]
        with self._lock:
            procs = self._load()
            procs[proc.id] = proc
            self._write(procs)
        return proc

    # ---- learning -----------------------------------------------------------------------------
    def save_from_task(self, task, phrase: Optional[str] = None, source: str = "task") -> Optional[Procedure]:
        """Remember what a finished task actually did (the steps that ran, with their checks)."""
        from modules.learning.skills import skills
        # What actually ran. Steps a recovery added are left out (installing packages every time
        # would be wrong) except opening an app, which the procedure clearly needed.
        done = [s for s in task.plan if s.status == "done" and
                (not s.inputs.get("recovery") or s.action.lower().startswith("open "))]
        actions = [s.action for s in done]
        phrase = phrase or task.request
        if len(actions) < 1 or not skills.learnable(phrase, actions):
            return None
        skill = skills.learn(phrase, actions, how="task")
        if skill is None:
            return None
        steps = [ProcStep(s.action, s.label, dict(s.verify or {}), list(s.fallback or []), s.risk, s.optional,
                          s.tool if not s.replaced else "", dict(s.args or {}) if not s.replaced else {})
                 for s in done]
        with self._lock:
            procs = self._load()
            old = next((p for p in procs.values() if p.phrase == skill.phrase), None)
            proc = Procedure(skill.id, skill.phrase, task.goal or phrase, steps, source=source)
            if old is not None:
                procs.pop(old.id, None)
                proc.version, proc.uses, proc.created = old.version + 1, old.uses, old.created
                proc.history = old.history + [{"version": proc.version, "at": time.time(),
                                               "change": "relearned from a new run"}]
            else:
                proc.history = [{"version": 1, "at": time.time(), "change": f"learned from “{phrase[:80]}”"}]
            procs[skill.id] = proc
            self._write(procs)
        log.info("procedures.saved %r steps=%d v%d", proc.phrase, len(steps), proc.version)
        event_bus.emit_event(EventType.AUTOMATION_UPDATED, {"id": skill.id, "title": proc.phrase, "skill": True,
                                                            "procedure": True})
        return proc

    def update_step(self, pid: str, index: int, action: str, reason: str, verify: Optional[dict] = None,
                    keep_old_as_fallback: bool = True) -> Optional[Procedure]:
        """A step was replaced and the replacement just worked: make it the procedure's step.
        Validated — the new action must be a command SAINT understands."""
        try:
            from modules.learning.planner import understood
            if not understood(action):
                log.info("procedures.update_rejected %r (not understood)", action)
                return None
        except Exception:
            return None
        from modules.learning.skills import skills
        with self._lock:
            procs = self._load()
            proc = procs.get(pid)
            if proc is None or not 0 <= index < len(proc.steps):
                return None
            old = proc.steps[index]
            if old.action == action:
                return proc
            new = ProcStep(action, old.label, dict(verify if verify is not None else old.verify),
                           ([old.action] if keep_old_as_fallback else []) +
                           [f for f in old.fallback if f != action][:3], old.risk, old.optional)   # no tool: routed
            proc.steps[index] = new
            proc.version += 1
            proc.updated = time.time()
            proc.history.append({"version": proc.version, "at": proc.updated, "step": index,
                                 "change": f"“{old.action}” → “{action}”", "reason": reason[:160]})
            del proc.history[:-MAX_HISTORY]
            procs[pid] = proc
            self._write(procs)
        skill = next((s for s in skills.all() if s.id == pid), None)
        if skill is not None:
            skill.steps = [s.action for s in proc.steps]
            skills.upsert(skill)
        log.info("procedures.updated %r step %d: %r -> %r (%s)", proc.phrase, index, old.action, action, reason)
        return proc

    def note_result(self, pid: str, ok: bool):
        with self._lock:
            procs = self._load()
            p = procs.get(pid)
            if p is None:
                return
            p.uses += 1 if ok else 0
            p.fails = 0 if ok else p.fails + 1
            p.updated = time.time()
            self._write(procs)

    def forget(self, pid: str) -> bool:
        with self._lock:
            procs = self._load()
            gone = procs.pop(pid, None)
            if gone is not None:
                self._write(procs)
        return gone is not None


procedures = ProcedureStore()

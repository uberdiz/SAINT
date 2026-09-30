"""
modules/learning/skills.py

Things SAINT has learned to do: a spoken request -> the commands that did it.

    "minimize all my windows"   -> ["show the desktop"]
    "open disk clean up"        -> ["open disk cleanup"]
    "play my gym playlist"     -> ["play my moe playlist"]

A skill is learned three ways (``how``):

    planned    SAINT worked it out itself (planner.py) and it worked
    corrected  the user said "no, I meant ..." and that worked
    shown      the user did it while SAINT watched (demonstration.py)

Steps are ordinary commands, so a learned skill goes through the same router,
permission checks and confirmations as anything said aloud — learning never
unlocks something that wasn't allowed. Matching runs before the router, so a
learned fix beats the old wrong guess. Stored in data/skills.json.
"""

import difflib
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from core.events import event_bus, EventType
from core.paths import data_path

log = logging.getLogger("saint.learning")

_LEAD = re.compile(r"^(?:(?:hey\s+)?saint[,\s]+)?(?:please\s+|can you\s+|could you\s+|would you\s+|will you\s+|"
                   r"i want you to\s+|go ahead and\s+)+", re.I)
_TAIL = re.compile(r"\s+(?:please|for me|now|thanks|thank you)$", re.I)
# Too generic to ever be a skill of their own.
_NEVER = re.compile(r"^(?:yes|no|yeah|nope|ok(?:ay)?|stop|done|thanks|thank you|cancel|never ?mind|it|that|this|"
                    r"do it|again|what|why|how|hello|hi|hey)$")
# "Switch back", "delete it", "open that folder" mean something different every
# time — what they point at — so they're never saved as a fixed recipe.
_DEICTIC = re.compile(r"\b(?:it|its|that|this|these|those|them|there|here|back|again|same|previous|the other)\b")


def norm(text: str) -> str:
    """'Hey SAINT, could you minimize all my windows, please?' -> 'minimize all my windows'."""
    t = (text or "").strip().lower()
    t = re.sub(r"[“”\"]", "", t)
    t = _LEAD.sub("", t)
    t = re.sub(r"[^\w\s'+:/.-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    t = _TAIL.sub("", t)
    return t.strip()


@dataclass
class Skill:
    phrase: str                            # normalised request
    steps: List[str]                       # commands, in order
    how: str = "planned"                   # planned | corrected | shown
    said: str = ""                         # the request as first said
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: float = field(default_factory=time.time)
    uses: int = 0
    last_used: float = 0.0
    fails: int = 0

    def describe(self) -> str:
        return " then ".join(f"“{s}”" for s in self.steps)


class SkillStore:
    MAX = 300

    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.Lock()
        self.last_learned: Optional[Skill] = None
        self.last_learned_at = 0.0

    @property
    def path(self) -> str:
        return self._path or str(data_path("skills.json"))

    # ------------------------------------------------------------------ #
    def all(self) -> List[Skill]:
        with self._lock:
            return self._load()

    def _load(self) -> List[Skill]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return []
        known = Skill.__dataclass_fields__
        out = []
        for s in raw if isinstance(raw, list) else []:
            if isinstance(s, dict) and s.get("phrase") and s.get("steps"):
                out.append(Skill(**{k: v for k, v in s.items() if k in known}))
        return out

    def _write(self, skills: List[Skill]):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([asdict(s) for s in skills], f, indent=2)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ #
    @staticmethod
    def learnable(phrase: str, steps: List[str]) -> bool:
        p = norm(phrase)
        if not p or _NEVER.match(p) or _DEICTIC.search(p) or len(p) < 4 or not steps:
            return False
        # Learning "X means X" teaches nothing.
        return [norm(s) for s in steps] != [p]

    def learn(self, phrase: str, steps: List[str], how: str = "planned") -> Optional[Skill]:
        steps = [s.strip() for s in steps if s and s.strip()]
        if not self.learnable(phrase, steps):
            return None
        key = norm(phrase)
        skill = Skill(key, steps, how, said=phrase.strip()[:200])
        with self._lock:
            skills = [s for s in self._load() if s.phrase != key]
            skills.append(skill)
            if len(skills) > self.MAX:            # forget the least useful first
                skills.sort(key=lambda s: (s.uses, s.last_used or s.created))
                skills = skills[len(skills) - self.MAX:]
            self._write(skills)
        self.last_learned, self.last_learned_at = skill, time.time()
        log.info("learning.learned how=%s phrase=%r steps=%r", how, key, steps)
        event_bus.emit_event(EventType.AUTOMATION_UPDATED, {"id": skill.id, "title": key, "skill": True})
        return skill

    def upsert(self, skill: Skill) -> Skill:
        """Add or replace a skill by id, as it is (used when another device shares one; see modules/link)."""
        with self._lock:
            skills = [s for s in self._load() if s.id != skill.id]
            skills.append(skill)
            self._write(skills)
        event_bus.emit_event(EventType.AUTOMATION_UPDATED, {"id": skill.id, "title": skill.phrase, "skill": True})
        return skill

    def match(self, text: str) -> Optional[Skill]:
        key = norm(text)
        if not key:
            return None
        skills = self.all()
        for s in skills:
            if s.phrase == key:
                return s
        # Speech-to-text variation ("minimise all my windows", a dropped "the").
        if len(key) >= 10:
            best, score = None, 0.0
            for s in skills:
                r = difflib.SequenceMatcher(None, key, s.phrase).ratio()
                if r > score:
                    best, score = s, r
            if best is not None and score >= 0.92:
                return best
        return None

    def find(self, what: str) -> Optional[Skill]:
        """A skill the user refers to ("forget how to open disk cleanup")."""
        key = norm(what)
        skills = self.all()
        exact = next((s for s in skills if s.phrase == key), None)
        if exact:
            return exact
        close = difflib.get_close_matches(key, [s.phrase for s in skills], n=1, cutoff=0.75)
        return next((s for s in skills if close and s.phrase == close[0]), None)

    def forget(self, skill_id: str) -> Optional[Skill]:
        with self._lock:
            skills = self._load()
            gone = next((s for s in skills if s.id == skill_id), None)
            if gone:
                self._write([s for s in skills if s.id != skill_id])
        if gone:
            if self.last_learned and self.last_learned.id == skill_id:
                self.last_learned = None
            log.info("learning.forgot phrase=%r", gone.phrase)
            event_bus.emit_event(EventType.AUTOMATION_CANCELLED, {"id": skill_id, "skill": True})
        return gone

    def update(self, skill_id: Optional[str], phrase: str, steps: List[str]) -> Optional[Skill]:
        """Edit a skill (or add one when ``skill_id`` is None) from the
        Automations page. Returns None if the phrase can't be a command."""
        steps = [s.strip() for s in steps if s and s.strip()]
        key = norm(phrase)
        if not key or _NEVER.match(key) or len(key) < 3 or not steps:
            return None
        with self._lock:
            skills = self._load()
            old = next((s for s in skills if s.id == skill_id), None) if skill_id else None
            skills = [s for s in skills if s.id != skill_id and s.phrase != key]
            skill = Skill(key, steps, "edited", said=phrase.strip()[:200])
            if old is not None:
                skill.id, skill.created, skill.uses, skill.last_used = old.id, old.created, old.uses, old.last_used
            skills.append(skill)
            self._write(skills)
        log.info("learning.edited phrase=%r steps=%r", key, steps)
        event_bus.emit_event(EventType.AUTOMATION_UPDATED, {"id": skill.id, "title": key, "skill": True})
        return skill

    def note_result(self, skill: Skill, ok: bool):
        """Count uses; a skill that fails three times in a row is dropped."""
        with self._lock:
            skills = self._load()
            for s in skills:
                if s.id == skill.id:
                    if ok:
                        s.uses, s.last_used, s.fails = s.uses + 1, time.time(), 0
                    else:
                        s.fails += 1
            # Ones the user wrote or edited stay (they can fix them on the Automations page).
            broken = [s for s in skills if s.fails >= 3 and s.how not in ("edited", "saved")]
            if broken:
                skills = [s for s in skills if s not in broken]
                log.info("learning.dropped_broken %s", [s.phrase for s in broken])
            self._write(skills)

    def recent(self, n: int = 5) -> List[Skill]:
        return sorted(self.all(), key=lambda s: s.created, reverse=True)[:n]


skills = SkillStore()


def run_steps(steps: List[str]):
    """Route and run a skill's steps as one plan. Returns a router Reply, or
    None when a step no longer means anything (the skill is stale)."""
    from modules.agent.router import route, run_plan
    intents = []
    for step in steps:
        it = route(step)
        if it is None:
            log.info("learning.step_unroutable %r", step)
            return None
        intents.append(it)
    if len(intents) == 1:
        return intents[0].run()
    return run_plan(intents)

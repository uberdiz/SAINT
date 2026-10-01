"""
modules/learning/feedback.py

Learning from every mistake, without being told "no, I meant ...":

    complaint   "no" / "that's wrong" / "I didn't ask for that" / "why did you do
                that" right after SAINT acted. A recipe SAINT worked out itself
                (planned / rephrased) that drew a complaint is forgotten at once;
                one the user taught gets a strike (three and it's dropped).
    rephrase    a request fails (or draws a complaint) and the user says it
                another way that works: the first wording is learned to mean
                the second ("play pink panther says fancy that album" ->
                "play fancy that by pinkpantheress").
    journal     every failure, complaint and fix goes to data/learning_journal.jsonl;
                the planner reads the recent ones as lessons ("searching YouTube
                for a Spotify album was wrong"), so the same mistake isn't
                planned again.

Nothing here acts on the PC: it only changes what SAINT has learned.
"""

import json
import logging
import os
import re
import threading
import time
from collections import deque
from typing import Deque, List, Optional

from core.config import config
from core.paths import data_path

log = logging.getLogger("saint.learning")

COMPLAINT_WINDOW = 45           # seconds after an action that a complaint is about it
REPHRASE_WINDOW = 90

_COMPLAINT = re.compile(
    r"^(?:(?:no+|nope|nah)[\s,.!]+)*(?:"
    r"wrong|that'?s (?:wrong|not (?:it|right|what i (?:asked|wanted|meant|said)))|"
    r"i didn'?t (?:ask|say|want|tell you)(?: (?:for|to do) (?:that|this))?|why (?:did|would|are) you|"
    r"what (?:are|were|did) you (?:doing|do)|don'?t do that|stop doing that|undo(?: that| it)?|"
    r"not what i (?:asked|said|meant|wanted)|you (?:did|got) (?:it|that) wrong|that'?s not it|i (?:meant|said)|"
    r"what the (?:hell|heck|fuck)|wtf)\b", re.I)
# A bare "no" / "nope, no" (not "no more Drake").
_BARE_NO = re.compile(r"^(?:no+|nope|nah)(?:[\s,.!]+(?:no+|nope|nah))*[\s.!]*$", re.I)
_STOPWORDS = {"the", "a", "an", "my", "me", "to", "on", "in", "of", "for", "and", "please", "can", "you", "it",
              "this", "that", "some", "with", "saint", "hey", "just", "now"}


def is_complaint(text: str) -> bool:
    t = (text or "").strip().strip("\"'“”").lower()
    return bool(t) and len(t.split()) <= 14 and bool(_COMPLAINT.match(t) or _BARE_NO.match(t))


def _content(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if len(w) > 2 and w not in _STOPWORDS}


def related(a: str, b: str) -> bool:
    """Could ``b`` be ``a`` said another way? Same first verb, or a shared
    content word, or words that sound alike (misheard names)."""
    import difflib
    wa, wb = (a or "").lower().split(), (b or "").lower().split()
    if not wa or not wb:
        return False
    ca, cb = _content(a), _content(b)
    if ca & cb:
        return True
    if wa[0] == wb[0] and wa[0] in ("play", "open", "launch", "start", "queue", "put", "switch", "close"):
        return True
    return any(difflib.SequenceMatcher(None, x, y).ratio() >= 0.75 for x in ca for y in cb if len(x) > 3)


class Journal:
    MAX_LINES = 600

    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.Lock()
        self._recent: Deque[dict] = deque(maxlen=60)
        self._loaded = False

    @property
    def path(self) -> str:
        return self._path or data_path("learning_journal.jsonl")

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f.readlines()[-60:]:
                    try:
                        self._recent.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass

    def add(self, kind: str, said: str, **info):
        rec = {"at": round(time.time(), 1), "kind": kind, "said": (said or "")[:200],
               **{k: (v[:200] if isinstance(v, str) else v) for k, v in info.items()}}
        with self._lock:
            self._load()
            self._recent.append(rec)
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if os.path.getsize(self.path) > 400_000:
                    with open(self.path, encoding="utf-8") as f:
                        lines = f.readlines()[-self.MAX_LINES:]
                    with open(self.path, "w", encoding="utf-8") as f:
                        f.writelines(lines)
            except OSError as e:
                log.debug("learning.journal_write_failed %s", e)
        log.info("learning.journal %s said=%r %s", kind, rec["said"][:60],
                 {k: v for k, v in info.items() if k in ("intent", "fix", "did")})

    def recent(self, n: int = 20) -> List[dict]:
        with self._lock:
            self._load()
            return list(self._recent)[-n:]

    def lessons(self, n: int = 8) -> str:
        """Recent mistakes, phrased for the planner."""
        out = []
        for r in reversed(self.recent(40)):
            if r["kind"] == "complaint" and r.get("did"):
                out.append(f'- For "{r["said"]}", doing {r["did"]} was WRONG (the user complained).')
            elif r["kind"] == "rephrase_fixed":
                out.append(f'- "{r["said"]}" meant "{r["fix"]}".')
            if len(out) >= n:
                break
        return "\n".join(out)


class FeedbackLearner:
    def __init__(self):
        self._lock = threading.Lock()
        self._last_action: Optional[dict] = None      # what SAINT just did
        self._failed: Optional[dict] = None           # a request that didn't work (for rephrase learning)
        self.just_taught = ("", 0.0)                   # (phrase, when) — the reply says it was learned
        self._turns: Deque[dict] = deque(maxlen=6)     # the last few requests, for the planner

    def expect_teaching(self, text: str, intent: str):
        """SAINT just said it doesn't know how to do ``text`` and asked to be told:
        the next command that works within the window is what ``text`` means,
        related wording or not ("turn it back up" ... "turn the volume up")."""
        if not config.get("learning.from_mistakes", True):
            return
        with self._lock:
            self._failed = {"text": text, "intent": intent, "at": time.time(), "teach": True}
        journal.add("failure", text, intent=intent, reply="asked to be taught")

    def note_result(self, text: str, intent: str, ok: bool, reply: str = "", steps: Optional[List[str]] = None,
                    expects_reply: bool = False):
        """After every handled request (agent._finish)."""
        if not config.get("learning.from_mistakes", True) or intent.startswith(("meta.", "confirmation",
                                                                                   "choice", "learning.done")):
            return
        now = time.time()
        with self._lock:
            failed = self._failed
            if ok and not expects_reply and failed and now - failed["at"] <= REPHRASE_WINDOW \
                    and failed["text"].strip().lower() != (text or "").strip().lower() \
                    and (failed.get("teach") or related(failed["text"], text)):
                self._failed = None
                self._learn_rephrase(failed, text)
            elif not ok and not expects_reply and not is_complaint(text):
                self._failed = {"text": text, "intent": intent, "at": now}
                journal.add("failure", text, intent=intent, reply=reply)
            self._last_action = {"text": text, "intent": intent, "ok": ok, "at": now, "steps": steps or []}
            self._turns.append({"text": (text or "")[:120], "intent": intent, "ok": ok,
                                "reply": (reply or "")[:120], "at": now})

    def recent_turns(self, max_age: float = 300.0) -> List[dict]:
        """What was just asked and done, oldest first — so "turn it back up" can
        be worked out from "turn it down" a moment ago."""
        now = time.time()
        with self._lock:
            return [t for t in self._turns if now - t["at"] <= max_age]

    def recent_context(self) -> str:
        """The recent requests as plain text (for grounding: a place mentioned a
        moment ago may be meant by "it")."""
        return " ".join(f'{t["text"]} {t["intent"]} {t["reply"]}' for t in self.recent_turns(180.0)).lower()

    def check_complaint(self, text: str) -> Optional[str]:
        """Called first for every utterance: if it complains about what SAINT
        just did, learn from it. Returns a short note to say, or None. Never
        consumes the utterance ("no, I meant X" / "stop" still run)."""
        if not config.get("learning.from_mistakes", True) or not is_complaint(text):
            return None
        with self._lock:
            last = self._last_action
            if not last or time.time() - last["at"] > COMPLAINT_WINDOW or is_complaint(last["text"]):
                return None
            self._last_action = None
            # The request itself still needs doing: a rephrase next can fix it.
            self._failed = {"text": last["text"], "intent": last["intent"], "at": time.time()}
        did = ", ".join(last["steps"]) if last["steps"] else last["intent"]
        journal.add("complaint", last["text"], intent=last["intent"], did=did, complaint=text)
        from modules.learning.skills import skills
        sk = skills.match(last["text"])
        if sk is None or last["intent"] not in ("learning.planned", "learning.skill"):
            return None
        if sk.how in ("planned", "rephrased"):
            skills.forget(sk.id)
            log.info("learning.unlearned_on_complaint %r", sk.phrase)
            return f"Sorry — I won't do that for “{sk.said or sk.phrase}” again."
        skills.note_result(sk, False)          # taught by the user: a strike, not deleted
        return None

    def _learn_rephrase(self, failed: dict, text: str):
        from modules.learning.skills import skills
        from modules.agent.router import route
        first, fixed = route(failed["text"]), route(text)
        if first is not None and not failed["intent"].startswith("learning.") and not failed.get("teach"):
            # The first wording already means something to the router (it just
            # failed this time, e.g. nothing found): a skill would shadow it, so
            # only learn it when the fix is the same kind of request (a misheard name).
            if fixed is None or first.domain != fixed.domain:
                journal.add("rephrase_seen", failed["text"], fix=text)
                return
        sk = skills.learn(failed["text"], [text], "taught" if failed.get("teach") else "rephrased")
        if sk is not None:
            journal.add("rephrase_fixed", failed["text"], fix=text, intent=failed["intent"])
            if failed.get("teach"):
                self.just_taught = (failed["text"], time.time())


journal = Journal()
feedback = FeedbackLearner()

"""
modules/agent/confirm.py

Pending confirmations. When a tool's permission policy is "confirm" (or the
agent offers something, e.g. "Want me to play it?"), the action is parked
here and SAINT asks. The next utterance resolves it: yes -> run, no -> drop.
Confirmations expire (agent.confirm_timeout_sec) so a stray "yes" minutes
later never triggers anything.
"""

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.config import config
from core.events import event_bus, EventType

log = logging.getLogger("saint.agent")

_YES = re.compile(r"^(yes|yeah|yea|yep|yup|ya|sure|ok|okay|k|do it|do that|go ahead|go on|confirm|confirmed|"
                  r"please do|yes please|affirmative|absolutely|definitely|of course|go for it|"
                  r"sounds good|play it|alright|all right|correct|that's right|that's it|"
                  r"uh[- ]huh|mm[- ]?hmm|mhm|let'?s do it|y)\b", re.I)
_NO = re.compile(r"^(no|nope|nah|don'?t|do not|cancel|stop|never ?mind|forget it|negative|not now|not yet|"
                 r"no thanks|hold on|wait|leave it|n)\b", re.I)
# "Uh, yeah." / "Um no" / "Oh yes" — Whisper keeps the filler.
_FILLER = re.compile(r"^(?:(?:uh+|um+|er+|hmm+|oh|ah|well|so)[\s,.!]+)+", re.I)


def classify_reply(text: str) -> Optional[bool]:
    t = re.sub(r"^(saint[,\s]+|hey saint[,\s]+)", "", (text or "").strip().lower()).strip(" .!?,")
    t = _FILLER.sub("", t)
    if _NO.match(t):
        return False
    if _YES.match(t):
        return True
    return None


@dataclass
class PendingAction:
    description: str                 # "close Discord"
    run: Callable[[], str]           # executes and returns the spoken result
    created: float = field(default_factory=time.time)
    tool: str = ""


class ConfirmationManager:
    SET_ASIDE_SEC = 12.0

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: Optional[PendingAction] = None
        # A question the user talked past ("Close Rocket League?" -> "and also open
        # YouTube") — a plain yes/no right after still answers it (2026-09-28).
        self._set_aside: Optional[tuple] = None      # (action, when)

    def ask(self, action: PendingAction):
        with self._lock:
            self._pending = action
            self._set_aside = None
        log.info("agent.confirm.ask %s", action.description)
        event_bus.emit_event(EventType.AGENT_CONFIRM_REQUIRED, {"description": action.description,
                                                                "tool": action.tool})

    @property
    def pending(self) -> Optional[PendingAction]:
        with self._lock:
            p = self._pending
        if p and time.time() - p.created > float(config.get("agent.confirm_timeout_sec", 30)):
            self.clear("expired")
            return None
        return p

    def clear(self, reason: str = ""):
        with self._lock:
            p, self._pending = self._pending, None
            if reason != "superseded":
                self._set_aside = None
        if p:
            event_bus.emit_event(EventType.AGENT_CONFIRM_RESOLVED, {"description": p.description,
                                                                    "result": reason})

    def set_aside(self) -> Optional[PendingAction]:
        """The question the user just talked past, if it's recent."""
        with self._lock:
            s = self._set_aside
        if s and time.time() - s[1] <= self.SET_ASIDE_SEC \
                and time.time() - s[0].created <= float(config.get("agent.confirm_timeout_sec", 30)):
            return s[0]
        return None

    def can_answer(self, text: str) -> bool:
        """Would ``text`` answer a question SAINT asked (pending or just set aside)?"""
        if self.pending is not None:
            return True
        return self.set_aside() is not None and classify_reply(text) is not None

    def resolve(self, text: str) -> Optional[str]:
        """If ``text`` answers a pending confirmation, act on it and return
        the spoken result; otherwise return None. Something else said instead
        sets the question aside: a plain yes/no in the next few seconds still
        answers it, anything later doesn't."""
        p = self.pending
        if p is None:
            p = self.set_aside()
            answer = classify_reply(text) if p is not None else None
            if answer is None:
                return None
            with self._lock:
                self._set_aside = None
                self._pending = p                # resolved below like a normal answer
        answer = classify_reply(text)
        if answer is None:
            with self._lock:
                self._set_aside = (p, time.time())
            self.clear("superseded")
            return None
        if not answer:
            self.clear("declined")
            choices.clear()               # "no" answers the whole question (e.g. a list offered with it)
            log.info("agent.confirm.declined %s", p.description)
            return "Okay, I won't."
        self.clear("confirmed")
        log.info("agent.confirm.accepted %s", p.description)
        return p.run()


confirmations = ConfirmationManager()


# ---------------------------------------------------------------------- #
# Clarifying questions with several answers ("which window?")
# ---------------------------------------------------------------------- #
_ORDINALS = {"first": 1, "1st": 1, "one": 1, "1": 1, "second": 2, "2nd": 2, "two": 2, "2": 2,
             "third": 3, "3rd": 3, "three": 3, "3": 3, "fourth": 4, "4th": 4, "four": 4, "4": 4,
             "fifth": 5, "5th": 5, "five": 5, "5": 5}


@dataclass
class ChoiceOption:
    label: str                       # spoken: "the YouTube window on your second screen"
    keywords: str                    # searchable text: title, app, monitor words
    value: object = None


@dataclass
class PendingChoice:
    question: str
    options: list
    run: Callable[[object], str]     # called with the chosen option's value
    created: float = field(default_factory=time.time)


class ChoiceManager:
    """Holds one open question such as "Which browser window should I use?"
    and resolves the user's next utterance against the options ("the first
    one", "the YouTube one", "the one on my second screen", "number two")."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: Optional[PendingChoice] = None

    def ask(self, choice: PendingChoice):
        with self._lock:
            self._pending = choice
        log.info("agent.choice.ask %s options=%d", choice.question, len(choice.options))

    @property
    def pending(self) -> Optional[PendingChoice]:
        with self._lock:
            p = self._pending
        if p and time.time() - p.created > float(config.get("agent.confirm_timeout_sec", 30)) * 2:
            self.clear()
            return None
        return p

    def clear(self):
        with self._lock:
            self._pending = None

    def match(self, text: str, options: list) -> Optional[int]:
        t = re.sub(r"^(saint[,\s]+|hey saint[,\s]+)", "", (text or "").lower()).strip(" .!?,")
        t = re.sub(r"\b(the|one|window|please|use|pick|choose|that|go with|i mean)\b", " ", t)
        words = re.findall(r"[a-z0-9]+", t)
        if not words:
            return None
        if "last" in words:
            return len(options) - 1
        # "the one on my second screen" -> monitor words decide first
        for w in words:
            if w in _ORDINALS and not ({"screen", "monitor", "display"} & set(words)):
                idx = _ORDINALS[w] - 1
                return idx if idx < len(options) else None
        best, best_score = None, 0.0
        for i, opt in enumerate(options):
            hay = opt.keywords.lower()
            score = sum(1 for w in words if len(w) > 2 and w in hay)
            if {"screen", "monitor", "display"} & set(words):
                for w in words:
                    if w in _ORDINALS and f"monitor {_ORDINALS[w]}" in hay:
                        score += 2
            if score > best_score:
                best, best_score = i, score
        return best if best_score > 0 else None

    def resolve(self, text: str) -> Optional[str]:
        p = self.pending
        if p is None:
            return None
        if classify_reply(text) is False:
            self.clear()
            return "Okay, never mind."
        idx = self.match(text, p.options)
        if idx is None:
            self.clear()             # the user moved on to something else
            return None
        self.clear()
        log.info("agent.choice.resolved %s -> %s", p.question, p.options[idx].label)
        return p.run(p.options[idx].value)


choices = ChoiceManager()

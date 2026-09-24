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

_YES = re.compile(r"^(yes|yeah|yep|yup|sure|ok|okay|do it|go ahead|confirm|confirmed|please do|"
                  r"yes please|affirmative|absolutely|of course|go for it|sounds good|play it|y)\b", re.I)
_NO = re.compile(r"^(no|nope|nah|don'?t|do not|cancel|stop|never ?mind|forget it|negative|not now|"
                 r"no thanks|n)\b", re.I)


def classify_reply(text: str) -> Optional[bool]:
    t = re.sub(r"^(saint[,\s]+|hey saint[,\s]+)", "", (text or "").strip().lower()).strip(" .!?,")
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
    def __init__(self):
        self._lock = threading.Lock()
        self._pending: Optional[PendingAction] = None

    def ask(self, action: PendingAction):
        with self._lock:
            self._pending = action
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
        if p:
            event_bus.emit_event(EventType.AGENT_CONFIRM_RESOLVED, {"description": p.description,
                                                                    "result": reason})

    def resolve(self, text: str) -> Optional[str]:
        """If ``text`` answers a pending confirmation, act on it and return
        the spoken result; otherwise return None (and drop a stale pending
        action if the user moved on to something else)."""
        p = self.pending
        if p is None:
            return None
        answer = classify_reply(text)
        if answer is None:
            self.clear("superseded")
            return None
        if not answer:
            self.clear("declined")
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

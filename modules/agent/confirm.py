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

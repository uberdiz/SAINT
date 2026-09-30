"""
modules/link/approvals.py

"Gian wants to type a prompt into Claude on this PC. Allow it?"

When a paired device asks for something its permission says needs an "ask", the
request waits here until the owner answers — by voice (through the same
yes / no confirmation SAINT uses for closing an app), by the Allow / Deny
buttons on the Devices page, or not at all (which is a no). Nothing a
collaborator asks for runs unless the owner said yes.
"""

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

log = logging.getLogger("saint.link")


@dataclass
class Approval:
    id: int
    peer_id: str
    peer_name: str
    description: str                 # "type a prompt into Claude: “summarise my notes”"
    created: float = field(default_factory=time.time)
    decision: Optional[bool] = None
    event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict:
        return {"id": self.id, "peer_id": self.peer_id, "peer": self.peer_name,
                "description": self.description, "created": self.created, "decision": self.decision}


class ApprovalQueue:
    def __init__(self, emit: Optional[Callable] = None, announce: Optional[Callable] = None):
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._open: Dict[int, Approval] = {}
        self._emit = emit                  # (event_name, payload)
        self._announce = announce          # (text) -> speaks / shows the question
        self.history: List[dict] = []

    # ------------------------------------------------------------------ #
    def request(self, peer_id: str, peer_name: str, description: str, timeout: float = 60.0) -> bool:
        """Block until the owner decides. True only on an explicit yes."""
        a = Approval(next(self._ids), peer_id, peer_name, description)
        with self._lock:
            self._open[a.id] = a
        log.info("link.approval.ask id=%d peer=%s %s", a.id, peer_name, description)
        if self._emit:
            self._emit("link.approval", a.public())
        pending = self._ask_by_voice(a)
        deadline = time.monotonic() + timeout
        try:
            while a.decision is None and time.monotonic() < deadline:
                a.event.wait(0.3)
                # Said "no" (or the confirmation expired): the voice question is gone.
                if pending is not None and a.decision is None and not self._still_pending(pending):
                    # A "yes" clears the question a moment before its callback runs: give it a beat.
                    a.event.wait(0.6)
                    if a.decision is None:
                        a.decision = False
        finally:
            with self._lock:
                self._open.pop(a.id, None)
            self._drop_voice_question(pending)
        allowed = bool(a.decision)
        self.history = (self.history + [dict(a.public(), decision=allowed)])[-50:]
        log.info("link.approval.done id=%d allowed=%s", a.id, allowed)
        if self._emit:
            self._emit("link.approval.done", {"id": a.id, "allowed": allowed})
        return allowed

    def resolve(self, approval_id: int, allow: bool) -> bool:
        with self._lock:
            a = self._open.get(int(approval_id))
        if a is None or a.decision is not None:
            return False
        a.decision = bool(allow)
        a.event.set()
        return True

    def pending(self) -> List[dict]:
        with self._lock:
            return [a.public() for a in self._open.values() if a.decision is None]

    # ------------------------------------------------------------------ #
    # The spoken yes / no rides on the existing confirmation flow.
    # ------------------------------------------------------------------ #
    def _ask_by_voice(self, a: Approval):
        try:
            from modules.agent.confirm import confirmations, PendingAction
            if confirmations.pending is not None:
                return None            # busy with another question: the on-screen buttons still work
            action = PendingAction(description=a.description,
                                   run=lambda: ("Okay, allowing it." if self.resolve(a.id, True) else "Too late."),
                                   tool="link.approval")
            confirmations.ask(action)
            if self._announce:
                self._announce(f"{a.peer_name} wants to {a.description}. Allow it?")
            return action
        except Exception:
            log.debug("link.approval.voice_unavailable", exc_info=True)
            return None

    @staticmethod
    def _still_pending(action) -> bool:
        try:
            from modules.agent.confirm import confirmations
            return confirmations.pending is action
        except Exception:
            return False

    @staticmethod
    def _drop_voice_question(action):
        if action is None:
            return
        try:
            from modules.agent.confirm import confirmations
            if confirmations.pending is action:
                confirmations.clear("resolved")
        except Exception:
            pass

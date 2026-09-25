"""
core/ui_link.py

Lets tools (running on the agent's thread) ask SAINT's window to do things
— switch page, show the mini player, open the overlay — without touching Qt
widgets off the GUI thread.

    ui_link.send("navigate", page="History")   # -> (ok, message)

``send`` emits EventType.UI_COMMAND; MainWindow handles it on the GUI thread
and calls ``ack`` with the outcome. ``attached`` is False when SAINT runs
without a window (tests, --background before the window exists).
"""

import threading
import uuid
from typing import Dict, Tuple

from core.events import event_bus, EventType


class UILink:
    def __init__(self):
        self.attached = False
        self._lock = threading.Lock()
        self._pending: Dict[str, dict] = {}

    def send(self, cmd: str, timeout: float = 2.0, **args) -> Tuple[bool, str]:
        if not self.attached:
            return False, "SAINT's window isn't running."
        cid = uuid.uuid4().hex[:10]
        slot = {"event": threading.Event(), "ok": False, "message": ""}
        with self._lock:
            self._pending[cid] = slot
        event_bus.emit_event(EventType.UI_COMMAND, {"id": cid, "cmd": cmd, "args": args})
        done = slot["event"].wait(timeout)
        with self._lock:
            self._pending.pop(cid, None)
        if not done:
            # The window is busy (e.g. mid-animation) — the command is queued
            # on the GUI thread and will still run.
            return True, ""
        return slot["ok"], slot["message"]

    def ack(self, cid: str, ok: bool = True, message: str = ""):
        with self._lock:
            slot = self._pending.get(cid)
        if slot:
            slot["ok"], slot["message"] = ok, message
            slot["event"].set()


ui_link = UILink()

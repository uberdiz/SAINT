"""
core/cancel.py

One process-wide "stop" signal for multi-step work.

Voice "stop" / "cancel" / "stop everything" trips it; long-running code checks
it between steps (router.run_plan, scenes, chunked typing, background tasks)
and bails out cleanly. Each trip bumps a generation counter, so work started
*after* a stop is not affected by it:

    tok = cancel.token()          # remember the generation when work starts
    ...
    tok.check()                   # raises Cancelled if "stop" was said since

"current" stops the foreground plan/scene/typing; "all" also stops background
tasks and watchers (subscribers to EventType.STOP_ALL).
"""

import logging
import threading

log = logging.getLogger("saint.cancel")


class Cancelled(Exception):
    """Raised by CancelToken.check() when the user said stop."""


class CancelToken:
    def __init__(self, scope: "CancelScope", generation: int):
        self._scope = scope
        self._gen = generation

    @property
    def cancelled(self) -> bool:
        return self._scope.generation != self._gen

    def check(self):
        if self.cancelled:
            raise Cancelled()


class CancelScope:
    def __init__(self):
        self._lock = threading.Lock()
        self._gen = 0

    @property
    def generation(self) -> int:
        return self._gen

    def token(self) -> CancelToken:
        return CancelToken(self, self._gen)

    def trip(self, scope: str = "current"):
        with self._lock:
            self._gen += 1
        log.info("cancel.trip scope=%s generation=%d", scope, self._gen)
        release_modifiers()
        if scope == "all":
            from core.events import event_bus, EventType
            event_bus.emit_event(EventType.STOP_ALL, {})


def release_modifiers():
    """Let go of any modifier key a cancelled key combo may have left held."""
    try:
        import pyautogui
        for key in ("shift", "ctrl", "alt", "win"):
            pyautogui.keyUp(key)
    except Exception:
        pass


cancel = CancelScope()

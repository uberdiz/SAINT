"""
core/startup.py

What happened to each subsystem when SAINT started, and a way to retry it.

The runtime starts every subsystem through ``startup.run(key, label, fn)``:
a failure is logged, recorded with a plain-English reason, and SAINT carries
on with the rest. The System page lists the results and has a Retry button
for anything that failed (``startup.retry(key)`` runs the same function again).
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from core.events import event_bus, EventType

log = logging.getLogger("saint.startup")

OK, FAILED, OFF = "ok", "failed", "off"


@dataclass
class Step:
    key: str
    label: str
    status: str = OK
    detail: str = ""
    at: float = field(default_factory=time.time)
    fn: Optional[Callable[[], object]] = field(default=None, repr=False)

    def as_dict(self) -> Dict:
        return {"key": self.key, "label": self.label, "status": self.status, "detail": self.detail,
                "at": self.at, "retryable": self.status == FAILED and self.fn is not None}


def _reason(e: BaseException) -> str:
    text = str(e).strip() or type(e).__name__
    if isinstance(e, ImportError):
        return f"a component is missing ({text})"
    return text[:200]


class Startup:
    def __init__(self):
        self._steps: Dict[str, Step] = {}
        self._lock = threading.Lock()

    def run(self, key: str, label: str, fn: Callable[[], object], enabled: bool = True) -> bool:
        """Start one subsystem; never raises. ``enabled=False`` records it as off."""
        if not enabled:
            self._record(Step(key, label, OFF, "turned off in Settings", fn=fn))
            return False
        try:
            fn()
        except Exception as e:
            log.exception("startup.failed %s", key)
            self._record(Step(key, label, FAILED, _reason(e), fn=fn))
            return False
        self._record(Step(key, label, OK, "", fn=fn))
        return True

    def report(self, key: str, label: str, ok: bool, detail: str = "",
               retry: Optional[Callable[[], object]] = None):
        """For subsystems that start asynchronously and report back later."""
        self._record(Step(key, label, OK if ok else FAILED, detail, fn=retry))

    def retry(self, key: str) -> bool:
        with self._lock:
            step = self._steps.get(key)
        if step is None or step.fn is None:
            return False
        return self.run(step.key, step.label, step.fn)

    def steps(self) -> List[Dict]:
        with self._lock:
            return [s.as_dict() for s in self._steps.values()]

    def failed(self) -> List[Dict]:
        return [s for s in self.steps() if s["status"] == FAILED]

    def _record(self, step: Step):
        with self._lock:
            self._steps[step.key] = step
        event_bus.emit_event(EventType.STARTUP_STATUS, step.as_dict())


startup = Startup()

"""
modules/agent/autonomy/model.py

The data an agent task is made of. Plain dataclasses, serialisable to JSON, no behaviour beyond
small helpers — the executor changes them, the manager saves them, the UI renders them.
"""

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


class TaskStatus:
    IDLE = "idle"
    UNDERSTANDING = "understanding"
    OBSERVING = "observing"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    WAITING_FOR_USER = "waiting_for_user"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ACTIVE = (UNDERSTANDING, OBSERVING, PLANNING, EXECUTING, VERIFYING, RECOVERING)
    OPEN = ACTIVE + (WAITING_FOR_USER, PAUSED)          # not finished
    FINAL = (COMPLETED, FAILED, CANCELLED)


class StepStatus:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class Risk:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass
class PlanStep:
    """One action. ``action`` is a SAINT command ("open discord") routed through the same router,
    permission policy and confirmations as anything said aloud — a plan can't do anything a
    spoken command couldn't. ``verify`` says how to check it worked (verify.py); ``fallback`` is
    a list of commands to try instead if it didn't (before the general recoveries)."""
    action: str
    label: str = ""                         # "Opening VS Code" (for the UI and "what are you doing?")
    tool: str = ""                          # set for built-in steps: call this tool with ``args`` directly
    args: Dict[str, Any] = field(default_factory=dict)
    inputs: Dict[str, Any] = field(default_factory=dict)
    expect: str = ""                        # "a VS Code window is open"
    verify: Dict[str, Any] = field(default_factory=dict)
    fallback: List[str] = field(default_factory=list)
    risk: str = Risk.LOW
    optional: bool = False                  # failing it doesn't fail the task
    status: str = StepStatus.PENDING
    attempts: int = 0
    result: str = ""                        # what the action said
    evidence: str = ""                      # what verification saw
    note: str = ""                          # why it failed / how it was recovered
    replaced: str = ""                      # the original action, when recovery swapped it
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:6])

    def title(self) -> str:
        return self.label or self.action[:1].upper() + self.action[1:]


@dataclass
class TrailEntry:
    """One line of the activity trail: 15:03:24 ACTION Open VS Code."""
    at: float
    kind: str                               # TASK_START OBSERVE PLAN ACTION VERIFY ERROR RECOVERY WAIT
    text: str                               # PAUSE RESUME COMPLETE FAILED CANCELLED LEARN NOTE
    step: Optional[int] = None


@dataclass
class AgentTask:
    request: str                            # what the user said
    goal: str = ""                          # "Set up your coding workspace"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = TaskStatus.UNDERSTANDING
    source: str = ""                        # procedure | goal | steps | model | composite
    context: Dict[str, Any] = field(default_factory=dict)   # project dir, monitors, ... (small)
    plan: List[PlanStep] = field(default_factory=list)
    current: int = -1
    reason: str = ""                        # why SAINT is doing the current thing
    observations: List[str] = field(default_factory=list)
    retries: int = 0                        # recoveries used, whole task
    failures: List[str] = field(default_factory=list)
    result: str = ""                        # the final spoken summary
    learned: str = ""                       # id of the procedure saved / updated from this task
    procedure: str = ""                     # id of the procedure this task ran from
    trail: List[TrailEntry] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    background: bool = True                 # runs on its own thread and announces the result
    announce: bool = True

    # ---- helpers --------------------------------------------------------------------------------
    @property
    def step(self) -> Optional[PlanStep]:
        return self.plan[self.current] if 0 <= self.current < len(self.plan) else None

    def remaining(self) -> List[int]:
        return [i for i, s in enumerate(self.plan) if s.status in (StepStatus.PENDING, StepStatus.RUNNING,
                                                                    StepStatus.FAILED)]

    def done_steps(self) -> List[PlanStep]:
        return [s for s in self.plan if s.status == StepStatus.DONE]

    def progress(self) -> float:
        if not self.plan:
            return 0.0
        return sum(1 for s in self.plan if s.status in (StepStatus.DONE, StepStatus.SKIPPED)) / len(self.plan)

    def is_open(self) -> bool:
        return self.status in TaskStatus.OPEN

    def note(self, kind: str, text: str, step: Optional[int] = None) -> TrailEntry:
        entry = TrailEntry(time.time(), kind, (text or "")[:400], step)
        self.trail.append(entry)
        del self.trail[:-200]                            # bounded: a looping task can't grow forever
        self.updated = entry.at
        return entry

    def max_risk(self) -> str:
        return max((s.risk for s in self.plan), key=lambda r: Risk.ORDER.get(r, 0), default=Risk.LOW)

    # ---- serialisation ----------------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AgentTask":
        d = dict(d or {})
        plan = [PlanStep(**{k: v for k, v in s.items() if k in PlanStep.__dataclass_fields__})
                for s in d.pop("plan", []) if isinstance(s, dict)]
        trail = [TrailEntry(**{k: v for k, v in t.items() if k in TrailEntry.__dataclass_fields__})
                 for t in d.pop("trail", []) if isinstance(t, dict)]
        task = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        task.plan, task.trail = plan, trail
        return task

    def summary(self) -> Dict[str, Any]:
        """What the UI needs, without the whole trail."""
        step = self.step
        return {"id": self.id, "request": self.request, "goal": self.goal, "status": self.status,
                "source": self.source, "current": self.current, "reason": self.reason,
                "progress": round(self.progress(), 3), "result": self.result, "updated": self.updated,
                "created": self.created, "failures": list(self.failures[-3:]),
                "step": step.title() if step else "",
                "steps": [{"label": s.title(), "status": s.status, "note": s.note, "risk": s.risk,
                           "optional": s.optional} for s in self.plan],
                "trail": [asdict(t) for t in self.trail[-12:]]}

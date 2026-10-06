"""
ui/components/task_card.py

"What is SAINT doing right now?" — the current agent task: its goal, the step it's on, every step
with its state, why it's doing what it's doing, and Pause / Continue / Stop. When a task waits for
an answer, Yes / No buttons answer it (the same as saying it). A multi-step plan that isn't an
agent task (core/activity.py) shows here too, without the controls.

Renders the plain dict ``AgentTask.summary()`` — nothing here touches a task directly; buttons go
through ``ui.actions``.
"""

import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from core.events import EventType
from ui import actions, icons
from ui.components.panel import Panel
from ui.design import tokens
from ui.reactive import ui_bus
from ui.theme import current_palette
from ui.widgets import ElidedLabel, ProgressLine, clear_layout

_STEP_ICON = {"done": "check-circle", "running": "dot", "pending": "circle", "failed": "x", "skipped": "skip"}
_EYEBROW = {"understanding": "SAINT IS THINKING…", "observing": "SAINT IS LOOKING…", "planning": "SAINT IS PLANNING…",
            "executing": "SAINT IS WORKING…", "verifying": "SAINT IS CHECKING…", "recovering": "SAINT IS FIXING SOMETHING…",
            "waiting_for_user": "WAITING FOR YOU", "paused": "PAUSED", "completed": "DONE", "failed": "DIDN'T FINISH",
            "cancelled": "STOPPED"}
ACTIVE = ("understanding", "observing", "planning", "executing", "verifying", "recovering")


def _ago(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago"


class StepRow(QWidget):
    def __init__(self, step: dict, active: bool, parent=None):
        super().__init__(parent)
        p = current_palette()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 1, 0, 1)
        lay.setSpacing(tokens.SPACE_SM)
        status = step.get("status", "pending")
        color = {"done": p.success, "running": p.accent, "failed": p.danger, "skipped": p.faint}.get(status, p.faint)
        icon = QLabel()
        icon.setPixmap(icons.pixmap(_STEP_ICON.get(status, "circle"), color, 15))
        icon.setFixedWidth(18)
        lay.addWidget(icon, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(0)
        text = ElidedLabel(step.get("label", ""))
        text.setObjectName({"done": "StepDone", "running": "StepActive", "failed": "StepFailed"}.get(
            status, "StepActive" if active else "StepPending"))
        col.addWidget(text)
        if step.get("note") and status in ("failed", "skipped", "running", "done"):
            note = ElidedLabel(step["note"])
            note.setObjectName("Faint")
            col.addWidget(note)
        lay.addLayout(col, 1)
        if step.get("risk") in ("medium", "high"):
            risk = QLabel(step["risk"])
            risk.setObjectName("ChipWarn" if step["risk"] == "high" else "Chip")
            lay.addWidget(risk, 0, Qt.AlignTop)


class TaskCard(Panel):
    """The current task, big. ``compact`` drops the step list (for narrow layouts / the overlay)."""

    def __init__(self, compact: bool = False, parent=None):
        super().__init__("Current task", parent=parent)
        self._compact = compact
        self._task: Optional[dict] = None
        self.elapsed = QLabel("")
        self.elapsed.setObjectName("PanelHint")
        self.add_action(self.elapsed)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setObjectName("Ghost")
        self.pause_btn.clicked.connect(self._pause_or_resume)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("Danger")
        self.stop_btn.clicked.connect(lambda: actions.task_control("stop"))
        self.add_action(self.pause_btn)
        self.add_action(self.stop_btn)

        self.eyebrow = QLabel("")
        self.eyebrow.setObjectName("Eyebrow")
        self.body.addWidget(self.eyebrow)
        self.goal = QLabel("")
        self.goal.setObjectName("Goal")
        self.goal.setWordWrap(True)
        self.body.addWidget(self.goal)
        self.current = QLabel("")
        self.current.setObjectName("Muted")
        self.current.setWordWrap(True)
        self.body.addWidget(self.current)
        self.progress = ProgressLine(4)
        self.body.addWidget(self.progress)
        self.steps = QVBoxLayout()
        self.steps.setSpacing(2)
        self.body.addLayout(self.steps)
        self.reason = QLabel("")
        self.reason.setObjectName("Reason")
        self.reason.setWordWrap(True)
        self.body.addWidget(self.reason)
        answer = QHBoxLayout()
        self.yes_btn = QPushButton("Yes, go ahead")
        self.yes_btn.setObjectName("Primary")
        self.yes_btn.clicked.connect(lambda: actions.answer("yes"))
        self.no_btn = QPushButton("No")
        self.no_btn.clicked.connect(lambda: actions.answer("no"))
        answer.addWidget(self.yes_btn)
        answer.addWidget(self.no_btn)
        answer.addStretch()
        self.body.addLayout(answer)
        self.hint = QLabel("")
        self.hint.setObjectName("Faint")
        self.hint.setWordWrap(True)
        self.body.addWidget(self.hint)

        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick)
        self._clock.start(1000)
        ui_bus.event.connect(self._on_event)
        self.refresh()

    # ---- data -----------------------------------------------------------------------------------
    def refresh(self):
        self._task = actions.current_task()
        self.render()

    def _on_event(self, ev):
        if ev.type == EventType.AGENT_TASK:
            p = ev.payload or {}
            cur = self._task
            if cur is None or p.get("id") == cur.get("id") or p.get("status") in ACTIVE + ("waiting_for_user",):
                self._task = p
                self.render()
        elif ev.type == EventType.ACTIVITY_CHANGED and (self._task is None or
                                                       self._task.get("status") not in ACTIVE + ("waiting_for_user",)):
            self.render()

    def _tick(self):
        t = self._task
        if t and t.get("status") in ACTIVE + ("waiting_for_user",):
            secs = int(time.time() - t.get("created", time.time()))
            self.elapsed.setText(f"{secs // 60}:{secs % 60:02d}")
        elif t:
            self.elapsed.setText(_ago(t.get("updated", time.time())))

    # ---- rendering ------------------------------------------------------------------------------------
    def render(self):
        t = self._task
        if t is None or (t.get("status") in ("completed", "failed", "cancelled")
                         and time.time() - t.get("updated", 0) > 1800):
            return self._render_idle()
        status = t.get("status", "")
        active = status in ACTIVE
        self.set_active(active or status == "waiting_for_user")
        self.eyebrow.setText(_EYEBROW.get(status, status.upper()))
        self.goal.setText(t.get("goal") or t.get("request", ""))
        steps = t.get("steps") or []
        cur = t.get("current", -1)
        if active and 0 <= cur < len(steps):
            self.current.setText(f"Now: {steps[cur]['label']}  ·  step {cur + 1} of {len(steps)}")
        elif status in ("completed", "failed", "cancelled"):
            self.current.setText(t.get("result", ""))
        else:
            self.current.setText(t.get("reason", "") if status == "waiting_for_user" else "")
        self.current.setVisible(bool(self.current.text()))
        self.progress.set_value(float(t.get("progress", 0.0)))
        self.progress.setVisible(bool(steps))
        clear_layout(self.steps)
        if not self._compact:
            for i, s in enumerate(steps[:14]):
                self.steps.addWidget(StepRow(s, active and i == cur))
            if len(steps) > 14:
                more = QLabel(f"+ {len(steps) - 14} more")
                more.setObjectName("Faint")
                self.steps.addWidget(more)
        reason = t.get("reason", "") if status in ACTIVE else ""
        self.reason.setText(f"Why: {reason}" if reason else "")
        self.reason.setVisible(bool(reason))
        waiting = status == "waiting_for_user"
        self.yes_btn.setVisible(waiting)
        self.no_btn.setVisible(waiting)
        self.pause_btn.setVisible(active or status == "paused" or status in ("failed", "cancelled"))
        self.pause_btn.setText("Pause" if active else "Continue")
        self.stop_btn.setVisible(active or waiting)
        self.hint.setText("Say “pause”, “what's next?” or “stop” any time." if active else "")
        self.hint.setVisible(active)
        self._tick()
        self.relayout()

    def _render_idle(self):
        from core.activity import activity
        snap = activity.snapshot()
        self.set_active(bool(snap["label"]))
        for w in (self.yes_btn, self.no_btn, self.pause_btn, self.stop_btn, self.reason):
            w.hide()
        clear_layout(self.steps)
        if snap["label"]:
            steps, i = snap["steps"], snap["index"]
            self.eyebrow.setText("SAINT IS WORKING…")
            self.goal.setText(snap["label"][:1].upper() + snap["label"][1:])
            self.current.setText(f"Now: {steps[i]}" if steps and 0 <= i < len(steps) else "")
            self.current.setVisible(bool(self.current.text()))
            self.progress.set_value((i + 1) / len(steps) if steps else 0.0)
            self.progress.setVisible(bool(steps))
            for k, s in enumerate(steps[:14]):
                self.steps.addWidget(StepRow({"label": s, "status": "done" if k < i else "running" if k == i
                                              else "pending"}, k == i))
            self.hint.hide()
            self.elapsed.setText("")
            self.relayout()
            return
        self.eyebrow.setText("READY")
        self.goal.setText("Nothing running")
        last = self._task
        self.current.setText(f"Last: {last.get('goal')} — {last.get('status')} {_ago(last.get('updated', 0))}."
                             if last else "")
        self.current.setVisible(bool(last))
        self.progress.hide()
        self.hint.setText("Try “set up my coding workspace”, “run the tests and tell me why they failed” or "
                          "“get everything ready for my meeting”.")
        self.hint.show()
        self.elapsed.setText("")
        self.relayout()

    def _pause_or_resume(self):
        t = self._task or {}
        actions.task_control("pause" if t.get("status") in ACTIVE else "resume")

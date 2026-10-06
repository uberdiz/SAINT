"""
ui/pages/tasks.py

Every agent task SAINT ran recently: what you asked, the plan, each step's result, the full
activity trail (observe / act / verify / recover), why something failed, and what SAINT learned
from it. Continue, stop, run it again, or remember it under a name.
"""

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from core.events import EventType
from ui import actions
from ui.components.activity_item import TrailView
from ui.components.panel import Panel
from ui.components.task_card import StepRow, _ago
from ui.design import tokens
from ui.reactive import ui_bus
from ui.widgets import Page, chip, clear_layout, run_async, set_chip

STATUS_WORD = {"completed": "Done", "failed": "Didn't finish", "cancelled": "Stopped", "paused": "Paused",
               "waiting_for_user": "Waiting for you", "executing": "Working", "verifying": "Checking",
               "recovering": "Fixing", "planning": "Planning", "observing": "Looking", "understanding": "Thinking"}
STATUS_CHIP = {"completed": "ok", "failed": "err", "waiting_for_user": "warn", "paused": "", "cancelled": "",
               "executing": "accent", "verifying": "accent", "recovering": "warn"}


class TasksPage(Page):
    def __init__(self, shell=None):
        super().__init__("Tasks", "What SAINT worked on, step by step — and what it learned.")
        self.shell = shell
        self._selected = None
        body = QHBoxLayout()
        body.setSpacing(tokens.SECTION_GAP)
        self.root.addLayout(body, 1)

        self.list = QListWidget()
        self.list.setObjectName("TaskList")
        self.list.setMinimumWidth(240)
        self.list.setMaximumWidth(360)
        self.list.currentItemChanged.connect(lambda cur, _prev: self._select(cur.data(Qt.UserRole) if cur else None))
        body.addWidget(self.list, 2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self.detail = QVBoxLayout(inner)
        self.detail.setContentsMargins(0, 0, 6, 0)
        self.detail.setSpacing(tokens.SECTION_GAP)
        scroll.setWidget(inner)
        body.addWidget(scroll, 5)

        self.head = Panel("Task")
        self.status_chip = chip("")
        self.head.add_action(self.status_chip)
        self.goal = QLabel("")
        self.goal.setObjectName("Goal")
        self.goal.setWordWrap(True)
        self.said = QLabel("")
        self.said.setObjectName("Muted")
        self.said.setWordWrap(True)
        self.said.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        for w in (self.goal, self.said, self.result):
            self.head.body.addWidget(w)
        buttons = QHBoxLayout()
        self.continue_btn = QPushButton("Continue")
        self.continue_btn.setObjectName("Primary")
        self.continue_btn.clicked.connect(lambda: actions.task_control("resume", lambda _r: self.refresh()))
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("Danger")
        self.stop_btn.clicked.connect(lambda: actions.task_control("stop", lambda _r: self.refresh()))
        self.again_btn = QPushButton("Run again")
        self.again_btn.clicked.connect(lambda: actions.submit(self._task.request) if self._task else None)
        self.remember_btn = QPushButton("Remember as…")
        self.remember_btn.setToolTip("Save these steps under a phrase you choose")
        self.remember_btn.clicked.connect(self._remember)
        for b in (self.continue_btn, self.stop_btn, self.again_btn, self.remember_btn):
            buttons.addWidget(b)
        buttons.addStretch()
        self.head.body.addLayout(buttons)
        self.detail.addWidget(self.head)

        self.steps_panel = Panel("Plan")
        self.steps_box = QVBoxLayout()
        self.steps_box.setSpacing(2)
        self.steps_panel.body.addLayout(self.steps_box)
        self.detail.addWidget(self.steps_panel)

        self.learn_panel = Panel("Learned")
        self.learn_text = QLabel("")
        self.learn_text.setWordWrap(True)
        self.learn_text.setObjectName("Muted")
        self.learn_panel.body.addWidget(self.learn_text)
        self.detail.addWidget(self.learn_panel)

        self.trail_panel = Panel("Activity trail", "observe · act · verify · recover")
        self.trail = TrailView(newest_first=False, limit=200)
        self.trail_panel.body.addWidget(self.trail)
        self.detail.addWidget(self.trail_panel)
        self.detail.addStretch()

        self._task = None
        ui_bus.event.connect(self._on_event)
        self.refresh()

    # ------------------------------------------------------------------ #
    def _on_event(self, ev):
        if ev.type == EventType.AGENT_TASK and self.isVisible():
            self.refresh()

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()

    def refresh(self):
        tasks = actions.task_list(40)
        keep = self._selected
        self.list.blockSignals(True)
        self.list.clear()
        for t in tasks:
            word = STATUS_WORD.get(t.status, t.status)
            it = QListWidgetItem(f"{t.goal or t.request}\n{word} · {_ago(t.updated)}")
            it.setData(Qt.UserRole, t.id)
            it.setToolTip(t.request)
            self.list.addItem(it)
        self.list.blockSignals(False)
        if not tasks:
            self._show_empty()
            return
        ids = [t.id for t in tasks]
        target = keep if keep in ids else ids[0]
        self.list.setCurrentRow(ids.index(target))
        self._select(target)

    def _show_empty(self):
        self._task = None
        set_chip(self.status_chip, "")
        self.goal.setText("No tasks yet")
        self.said.setText("Ask SAINT for something with several steps — “set up my coding workspace”, “run the "
                          "tests and tell me why they failed”, “open Discord, put Spotify on my second monitor "
                          "and tell me when everything is ready”.")
        self.result.setText("")
        for w in (self.continue_btn, self.stop_btn, self.again_btn, self.remember_btn):
            w.hide()
        self.steps_panel.hide()
        self.learn_panel.hide()
        self.trail_panel.hide()

    def _select(self, task_id):
        if not task_id:
            return
        self._selected = task_id
        try:
            from modules.agent.autonomy.manager import agent_tasks
            t = agent_tasks.get(task_id)
        except Exception:
            t = None
        if t is None:
            return
        self._task = t
        set_chip(self.status_chip, STATUS_WORD.get(t.status, t.status), STATUS_CHIP.get(t.status, ""))
        self.goal.setText(t.goal or t.request)
        src = {"procedure": "a procedure SAINT learned", "goal": "a built-in goal", "steps": "your own steps",
               "model": "a plan from the local model", "composite": "your own steps"}.get(t.source, t.source)
        self.said.setText(f"You said “{t.request}” · {time.strftime('%a %H:%M', time.localtime(t.created))} · "
                          f"planned from {src}" + (f" · {t.retries} recover{'y' if t.retries == 1 else 'ies'}"
                                                   if t.retries else ""))
        self.result.setText(t.result or t.reason or "")
        self.result.setVisible(bool(self.result.text()))
        open_ = t.status in ("paused", "failed", "cancelled", "waiting_for_user")
        active = t.status in ("understanding", "observing", "planning", "executing", "verifying", "recovering")
        self.continue_btn.setVisible(open_ and t.status != "waiting_for_user")
        self.stop_btn.setVisible(active or t.status == "waiting_for_user")
        self.again_btn.setVisible(t.status in ("completed", "failed", "cancelled"))
        self.remember_btn.setVisible(t.status == "completed")
        clear_layout(self.steps_box)
        s = t.summary()
        for i, step in enumerate(s["steps"]):
            self.steps_box.addWidget(StepRow(step, active and i == t.current))
        self.steps_panel.setVisible(bool(s["steps"]))
        self._render_learned(t)
        self.trail.set_entries([{"at": e.at, "kind": e.kind, "text": e.text} for e in t.trail])
        self.trail_panel.show()

    def _render_learned(self, t):
        text = ""
        try:
            from modules.learning.procedures import procedures
            pid = t.learned or t.procedure
            proc = procedures.get(pid) if pid else None
            if proc is not None:
                text = (f"“{proc.phrase}” — version {proc.version}, {len(proc.steps)} steps, used {proc.uses} "
                        f"time{'s' if proc.uses != 1 else ''}.")
                if proc.history:
                    h = proc.history[-1]
                    text += f" Last change: {h.get('change', '')}" + (f" ({h['reason']})" if h.get("reason") else "") + "."
        except Exception:
            text = ""
        self.learn_text.setText(text)
        self.learn_panel.setVisible(bool(text))

    def _remember(self):
        if self._task is None:
            return
        name, ok = QInputDialog.getText(self, "Remember this task", "When I say:", text=self._task.request)
        if not ok or not name.strip():
            return

        def go(n=name.strip()):
            from modules.agent.autonomy.manager import agent_tasks
            from modules.learning.procedures import procedures
            proc = procedures.save_from_task(self._task, phrase=n, source="taught")
            return proc is not None
        run_async(go, lambda ok: self.shell and self.shell.toast(
            "Remembered" if ok else "Couldn't remember that",
            f"Say “{name.strip()}” and I'll do these steps." if ok else "That phrase can't be a command.",
            "ok" if ok else "warn"), lambda e: None)

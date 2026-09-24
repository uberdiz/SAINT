"""
ui/pages/automations.py

Scenes (several commands, one trigger) and Scheduled (reminders and timed
commands that run in the background, window open or not).
"""

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
                               QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from core.events import EventType
from ui import actions, icons
from ui.reactive import ui_bus
from ui.theme import current_palette
from ui.widgets import Card, Page, Segmented, run_async

TEMPLATES = [
    ("Focus mode", "focus time", ["play lofi beats", "set volume to 35"], ""),
    ("Wind down", "", ["play something chill", "set volume to 20"], "every day at 10 PM"),
    ("Morning", "good morning", ["play my liked songs", "open chrome"], ""),
]


def _preview(label: QLabel, text: str):
    from modules.automation.timeparse import describe, parse_schedule
    pal = current_palette()
    if not text.strip():
        label.setText("")
        return
    sched, _ = parse_schedule(text)
    label.setText(f"→ {describe(sched)}" if sched else "→ I can't read that time yet")
    label.setStyleSheet(f"color:{pal.success if sched else pal.warning};")


class ScenesView(QWidget):
    def __init__(self):
        super().__init__()
        self._current = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(10)
        new = QPushButton(" New scene")
        new.setObjectName("Primary")
        new.clicked.connect(lambda: self.edit(None))
        self._new_btn = new
        left.addWidget(new)
        self.list = QListWidget()
        self.list.setObjectName("Flat")
        self.list.currentItemChanged.connect(self._picked)
        left.addWidget(self.list, 1)
        self.templates = Card("Start from")
        for name, phrase, steps, sched in TEMPLATES:
            b = QPushButton(name)
            b.setObjectName("SceneButton")
            b.clicked.connect(lambda _=False, t=(name, phrase, steps, sched): self._template(*t))
            self.templates.body.addWidget(b)
        left.addWidget(self.templates)
        lw = QWidget()
        lw.setLayout(left)
        lw.setFixedWidth(260)
        lay.addWidget(lw)

        ed = Card("Scene")
        self.name = QLineEdit()
        self.name.setPlaceholderText("Name — e.g. Focus mode")
        self.phrase = QLineEdit()
        self.phrase.setPlaceholderText("Extra voice phrase (optional) — e.g. “focus time”")
        self.steps = QPlainTextEdit()
        self.steps.setMaximumHeight(210)
        self.steps.setPlaceholderText("One command per line, run in order:\nplay lofi beats\nset volume to 35\nopen notion")
        self.schedule = QLineEdit()
        self.schedule.setPlaceholderText("Schedule (optional) — “every weekday at 8”, “at 10 PM”")
        self.sched_preview = QLabel("")
        self.sched_preview.setObjectName("Faint")
        self.schedule.textChanged.connect(lambda t: _preview(self.sched_preview, t))
        self.name.textChanged.connect(self._explain)
        self.phrase.textChanged.connect(self._explain)
        for label, w in (("Name", self.name), ("Voice", self.phrase), ("Steps", self.steps),
                         ("Schedule", self.schedule)):
            lab = QLabel(label)
            lab.setObjectName("Faint")
            ed.body.addWidget(lab)
            ed.body.addWidget(w)
        ed.body.addWidget(self.sched_preview)
        ed.body.addStretch()
        self.how = QLabel("")
        self.how.setObjectName("Muted")
        self.how.setWordWrap(True)
        ed.body.addWidget(self.how)
        row = QHBoxLayout()
        self.run_btn = QPushButton("Run now")
        self.run_btn.clicked.connect(self._run)
        self.save_btn = QPushButton("Save scene")
        self.save_btn.setObjectName("Primary")
        self.save_btn.clicked.connect(self._save)
        self.del_btn = QPushButton("Delete")
        self.del_btn.setObjectName("Danger")
        self.del_btn.clicked.connect(self._delete)
        row.addWidget(self.del_btn)
        row.addStretch()
        row.addWidget(self.run_btn)
        row.addWidget(self.save_btn)
        ed.body.addLayout(row)
        self.status = QLabel("")
        self.status.setObjectName("Faint")
        self.status.setWordWrap(True)
        ed.body.addWidget(self.status)
        lay.addWidget(ed, 1)
        self.edit(None)

    def apply_theme(self):
        self._new_btn.setIcon(icons.icon("plus", current_palette().on_accent, 14))

    # ------------------------------------------------------------------ #
    def refresh(self, select_id=None):
        from modules.automation.scenes import scenes

        def show(items):
            self.list.blockSignals(True)
            self.list.clear()
            for s in items:
                it = QListWidgetItem(f"{s.name}\n{len(s.steps)} step{'s' if len(s.steps) != 1 else ''}"
                                     + (f" · {s.schedule}" if s.schedule else "")
                                     + (f" · “{s.phrase}”" if s.phrase else ""))
                it.setData(Qt.UserRole, s.id)
                self.list.addItem(it)
                if s.id == (select_id or getattr(self._current, "id", None)):
                    self.list.setCurrentItem(it)
            self.list.blockSignals(False)
            self.templates.setVisible(len(items) < 3)
        run_async(scenes.all, show)

    def _picked(self, item, _prev=None):
        if item is None:
            return
        from modules.automation.scenes import scenes
        self.edit(scenes.get(item.data(Qt.UserRole)))

    def edit(self, scene):
        self._current = scene
        self.name.setText(scene.name if scene else "")
        self.phrase.setText(scene.phrase if scene else "")
        self.steps.setPlainText("\n".join(scene.steps) if scene else "")
        self.schedule.setText(scene.schedule if scene else "")
        self.del_btn.setEnabled(scene is not None)
        self.status.setText("")
        self._explain()
        if scene is None:
            self.list.clearSelection()
            self.name.setFocus()

    def _explain(self):
        name = self.name.text().strip() or "the scene name"
        extra = f" or “{self.phrase.text().strip()}”" if self.phrase.text().strip() else ""
        self.how.setText(f"Say “Hey SAINT, {name}”{extra} — or “run {name}”.")

    def _template(self, name, phrase, steps, sched):
        self.edit(None)
        self.name.setText(name)
        self.phrase.setText(phrase)
        self.steps.setPlainText("\n".join(steps))
        self.schedule.setText(sched)
        self._explain()

    def _collect(self):
        from modules.automation.scenes import Scene
        s = Scene(self.name.text(), self.steps.toPlainText().splitlines(), phrase=self.phrase.text().strip(),
                  schedule=self.schedule.text().strip())
        if self._current:
            s.id, s.automation_id, s.last_run = self._current.id, self._current.automation_id, self._current.last_run
        return s

    def _save(self):
        from modules.automation.scenes import scenes
        try:
            s = scenes.save(self._collect())
        except ValueError as e:
            self._say(str(e), "warning")
            return
        self._current = s
        self._explain()
        self._say(f"Saved “{s.name}”." + (" It's scheduled too." if s.automation_id else ""), "success")
        self.refresh(s.id)
        self.del_btn.setEnabled(True)

    def _run(self):
        s = self._collect()
        if not s.steps:
            self._say("Add at least one step.", "warning")
            return
        self._say(f"Running {len(s.steps)} step{'s' if len(s.steps) != 1 else ''}…", "muted")
        actions.run_scene(s)

    def show_results(self, title, result):
        if self._current and title == self._current.name or title == self.name.text().strip():
            self._say(result or "Done.", "success")

    def _delete(self):
        from modules.automation.scenes import scenes
        if self._current:
            scenes.delete(self._current.id)
            self.edit(None)
            self.refresh()

    def _say(self, text, tone):
        pal = current_palette()
        self.status.setText(text)
        self.status.setStyleSheet(f"color:{getattr(pal, tone, pal.muted)};")


class ScheduledView(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        create = Card("New")
        row = QHBoxLayout()
        self.kind = QComboBox()
        self.kind.addItems(["Reminder", "Command"])
        self.what = QLineEdit()
        self.what.setPlaceholderText("What — “stretch”, or a command like “play my focus playlist”")
        self.when = QLineEdit()
        self.when.setPlaceholderText("When — “at 5 PM”, “in 30 minutes”, “every weekday at 8”")
        self.preview = QLabel("")
        self.preview.setObjectName("Faint")
        self.when.textChanged.connect(lambda t: _preview(self.preview, t))
        add = QPushButton("Schedule")
        add.setObjectName("Primary")
        add.clicked.connect(self._create)
        row.addWidget(self.kind)
        row.addWidget(self.what, 2)
        row.addWidget(self.when, 2)
        row.addWidget(add)
        create.body.addLayout(row)
        create.body.addWidget(self.preview)
        lay.addWidget(create)

        card = Card("Scheduled")
        self.show_all = QCheckBox("Show finished")
        self.show_all.toggled.connect(self.refresh)
        card.header.addWidget(self.show_all)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Title", "Type", "Schedule", "Next run", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        card.body.addWidget(self.table)
        btns = QHBoxLayout()
        for label, fn, obj in (("Pause / resume", self._toggle_pause, ""), ("Run now", self._run_now, ""),
                               ("Cancel", self._cancel, "Danger"), ("Delete", self._delete, "Danger")):
            b = QPushButton(label)
            if obj:
                b.setObjectName(obj)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch()
        self.status = QLabel("")
        self.status.setObjectName("Faint")
        btns.addWidget(self.status)
        card.body.addLayout(btns)
        lay.addWidget(card, 1)

    def _create(self):
        from modules.automation.scheduler import scheduler
        from modules.automation.timeparse import parse_schedule
        what, when = self.what.text().strip(), self.when.text().strip()
        if not what or not when:
            self.status.setText("Fill in what and when.")
            return
        sched, _ = parse_schedule(when)
        if not sched:
            self.status.setText("I couldn't understand that time.")
            return
        try:
            a = scheduler.create("reminder" if self.kind.currentIndex() == 0 else "command", what, sched)
        except ValueError as e:
            self.status.setText(str(e))
            return
        self.status.setText(f"Scheduled “{a.title}” — {a.describe()}")
        self.what.clear()
        self.when.clear()

    def refresh(self):
        from modules.automation.scheduler import scheduler
        show_all = self.show_all.isChecked()

        def show(items):
            items = items if show_all else [a for a in items if a.status in ("active", "paused")]
            self.table.setRowCount(0)
            for a in items:
                r = self.table.rowCount()
                self.table.insertRow(r)
                nxt = datetime.fromtimestamp(a.next_run).strftime("%a %b %d  %H:%M") if a.next_run else "—"
                vals = [a.title, a.kind, a.describe(), nxt,
                        a.status + (f" · ran {a.run_count}×" if a.run_count else "")]
                for c, v in enumerate(vals):
                    it = QTableWidgetItem(str(v))
                    it.setData(Qt.UserRole, a.id)
                    self.table.setItem(r, c, it)
        run_async(scheduler.list, show)

    def _selected(self):
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole) if rows else None

    def _toggle_pause(self):
        from modules.automation.scheduler import scheduler
        aid = self._selected()
        a = scheduler.store.get(aid) if aid else None
        if a:
            scheduler.set_paused(aid, a.status == "active")

    def _run_now(self):
        from modules.automation.scheduler import scheduler
        aid = self._selected()
        if aid:
            run_async(lambda: scheduler.run_now(aid))

    def _cancel(self):
        from modules.automation.scheduler import scheduler
        aid = self._selected()
        if aid:
            scheduler.cancel(aid)

    def _delete(self):
        from modules.automation.scheduler import scheduler
        aid = self._selected()
        if aid:
            scheduler.delete(aid)
            self.refresh()


class AutomationsPage(Page):
    def __init__(self):
        super().__init__("Automations", "Scenes run several commands from one phrase. Schedules run on time — "
                                        "even with the window closed.")
        self.seg = Segmented(["Scenes", "Scheduled"])
        self.actions.addWidget(self.seg)
        self.stack = QStackedWidget()
        self.scenes = ScenesView()
        self.scheduled = ScheduledView()
        self.stack.addWidget(self.scenes)
        self.stack.addWidget(self.scheduled)
        self.seg.changed.connect(self.stack.setCurrentIndex)
        self.root.addWidget(self.stack, 1)
        ui_bus.event.connect(self._on_event)

    def apply_theme(self):
        self.scenes.apply_theme()

    def _on_event(self, ev):
        t, p = ev.type, ev.payload or {}
        if t in (EventType.AUTOMATION_CREATED, EventType.AUTOMATION_UPDATED, EventType.AUTOMATION_CANCELLED,
                 EventType.AUTOMATION_TRIGGERED) and self.isVisible():
            self.scheduled.refresh()
            if p.get("kind") == "scene":
                self.scenes.show_results(p.get("title", ""), p.get("result", ""))

    def showEvent(self, e):
        super().showEvent(e)
        self.scenes.refresh()
        self.scheduled.refresh()

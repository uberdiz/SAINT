"""
ui/automations_ui.py

View and manage reminders and scheduled automations. The list reflects the
scheduler's persistent store; changes made by voice show up here live.
"""

from datetime import datetime

from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QCheckBox,
)

from core.events import event_bus, EventType
from ui.theme import current_palette
from ui.widgets import Card, run_async


class AutomationsUI(QWidget):
    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(14)
        t = QLabel("Automations")
        t.setObjectName("PageTitle")
        root.addWidget(t)
        sub = QLabel("Reminders and scheduled commands persist across restarts and run in the background, "
                     "whether or not this window is open.")
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # ---- create ---------------------------------------------------------
        create = Card("New")
        row = QHBoxLayout()
        self.kind = QComboBox()
        self.kind.addItems(["Reminder", "Command"])
        self.what = QLineEdit()
        self.what.setPlaceholderText("What — e.g. “Work on AIDE” or, for a command, “play my focus playlist”")
        self.when = QLineEdit()
        self.when.setPlaceholderText("When — “at 5 PM”, “in 30 minutes”, “every weekday at 8”")
        self.when.textChanged.connect(self._preview)
        add = QPushButton("Schedule")
        add.setObjectName("Primary")
        add.clicked.connect(self._create)
        row.addWidget(self.kind)
        row.addWidget(self.what, 2)
        row.addWidget(self.when, 2)
        row.addWidget(add)
        create.body.addLayout(row)
        self.preview = QLabel("")
        self.preview.setObjectName("Faint")
        create.body.addWidget(self.preview)
        root.addWidget(create)

        # ---- list ---------------------------------------------------------------
        card = Card("Scheduled")
        self.show_all = QCheckBox("Show finished")
        self.show_all.toggled.connect(self.refresh)
        card.header.addWidget(self.show_all)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Title", "Type", "Schedule", "Next run", "Status", "ID"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        card.body.addWidget(self.table)
        btns = QHBoxLayout()
        for label, fn, obj in (("Pause / Resume", self._toggle_pause, ""), ("Run now", self._run_now, ""),
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
        root.addWidget(card, 1)

        event_bus.event_occurred.connect(self._on_event)
        self.refresh()

    def _on_event(self, ev):
        if ev.type in (EventType.AUTOMATION_CREATED, EventType.AUTOMATION_UPDATED,
                       EventType.AUTOMATION_CANCELLED, EventType.AUTOMATION_TRIGGERED):
            self.refresh()

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()

    def _preview(self):
        from modules.automation.timeparse import parse_schedule, describe
        text = self.when.text().strip()
        if not text:
            self.preview.setText("")
            return
        sched, _ = parse_schedule(text)
        pal = current_palette()
        if sched:
            self.preview.setText(f"→ {describe(sched)}")
            self.preview.setStyleSheet(f"color:{pal.success};")
        else:
            self.preview.setText("→ I can't read that time yet")
            self.preview.setStyleSheet(f"color:{pal.warning};")

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
                nxt = datetime.fromtimestamp(a.next_run).strftime("%a %b %d %H:%M") if a.next_run else "—"
                vals = [a.title, a.kind, a.describe(), nxt, a.status + (f" · ran {a.run_count}×" if a.run_count else ""), a.id]
                for c, v in enumerate(vals):
                    self.table.setItem(r, c, QTableWidgetItem(str(v)))
        run_async(scheduler.list, show)

    def _selected(self):
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 5).text() if rows else None

    def _toggle_pause(self):
        from modules.automation.scheduler import scheduler
        aid = self._selected()
        if aid:
            a = scheduler.store.get(aid)
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

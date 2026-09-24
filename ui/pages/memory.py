"""
ui/pages/memory.py

What SAINT remembers — exactly what's stored, nothing inferred. Edit or
delete anything; say “forget …” to do the same by voice.
"""

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QTableWidget, QTableWidgetItem)

from core.events import EventType
from ui.reactive import ui_bus
from ui.widgets import Card, Page, Segmented, run_async

CATEGORIES = ["All", "preference", "personal", "fact", "project"]


class MemoryPage(Page):
    def __init__(self):
        super().__init__("Memory", "Long-term facts SAINT keeps because you told it. Say “forget …” or edit them here.")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search memories…")
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(self.refresh)
        self.actions.addWidget(self.search)
        self.category = Segmented([c.capitalize() for c in CATEGORIES])
        self.category.changed.connect(lambda _i: self.refresh())
        self.root.addWidget(self.category, 0, Qt.AlignLeft)

        card = Card("Long-term memory")
        self.count = QLabel("")
        self.count.setObjectName("Faint")
        card.header.addWidget(self.count)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Category", "Memory", "Updated"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.doubleClicked.connect(self.edit_selected)
        card.body.addWidget(self.table)
        btns = QHBoxLayout()
        for label, fn, obj in (("Add…", self.add, "Primary"), ("Edit…", self.edit_selected, ""),
                               ("Delete", self.delete_selected, "Danger")):
            b = QPushButton(label)
            if obj:
                b.setObjectName(obj)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch()
        card.body.addLayout(btns)
        self.root.addWidget(card, 3)

        self.music = Card("Music memory")
        self.music_label = QLabel("")
        self.music_label.setWordWrap(True)
        self.music_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.music.body.addWidget(self.music_label)
        self.root.addWidget(self.music, 1)
        ui_bus.event.connect(self._on_event)

    def _on_event(self, ev):
        if ev.type in (EventType.MEMORY_STORED, EventType.MEMORY_UPDATED, EventType.MEMORY_DELETED) and self.isVisible():
            self.refresh()

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()
        self.refresh_music()

    def refresh(self):
        from modules.memory.service import memory_service
        cat = CATEGORIES[max(0, self.category.index())]
        query = self.search.text().strip().lower()

        def load():
            items = memory_service.all(None if cat == "All" else cat)
            return [e for e in items if query in e.content.lower()] if query else items

        def show(items):
            self.table.setRowCount(0)
            for e in items:
                r = self.table.rowCount()
                self.table.insertRow(r)
                vals = (e.metadata.get("category", e.type.value), e.content,
                        datetime.fromtimestamp(e.updated_at).strftime("%b %d, %H:%M"))
                for c, v in enumerate(vals):
                    it = QTableWidgetItem(v)
                    it.setData(Qt.UserRole, e.id)
                    self.table.setItem(r, c, it)
            self.count.setText(f"{len(items)} stored")
        run_async(load, show, lambda err: self.count.setText(f"Couldn't read memory: {err}"))

    def _selected_id(self):
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole) if rows else None

    def add(self):
        text, ok = QInputDialog.getText(self, "Add memory", "What should SAINT remember?\n"
                                        "(e.g. “My favorite programming language is Python”)")
        if ok and text.strip():
            from modules.memory.service import extract_fact, memory_service
            fact = extract_fact(text) or {}
            memory_service.remember(content=fact.get("content", text.strip()), key=fact.get("key", ""),
                                    value=fact.get("value", ""), category=fact.get("category", "fact"))
            self.refresh()

    def edit_selected(self):
        mid = self._selected_id()
        if mid is None:
            return
        from modules.memory.service import extract_fact, memory_service
        entry = memory_service.db.retrieve(mid)
        if entry is None:
            return
        text, ok = QInputDialog.getText(self, "Edit memory", "Memory:", text=entry.content)
        if ok and text.strip():
            fact = extract_fact(text)
            memory_service.update(mid, content=text.strip(), value=fact["value"] if fact else None)
            self.refresh()

    def delete_selected(self):
        mid = self._selected_id()
        if mid is not None and QMessageBox.question(self, "Delete memory", "Delete this memory permanently?") \
                == QMessageBox.Yes:
            from modules.memory.service import memory_service
            memory_service.forget(mid)
            self.refresh()

    def refresh_music(self):
        from core.module_manager import module_manager
        sp = module_manager.get("spotify")

        def show(s):
            top = ", ".join(f"{a['artist']} ({a['plays']})" for a in s["top_artists"][:6]) or "—"
            genres = ", ".join(g["genre"] for g in s["top_genres"][:6]) or "—"
            recs = s["recommendations"]
            self.music_label.setText(
                f"<b>Plays today</b> {len(s['today'])} &nbsp;·&nbsp; <b>Skips (7 days)</b> {s['skips_7d']} "
                f"&nbsp;·&nbsp; <b>Picks kept</b> {recs['accepted']} of {recs['accepted'] + recs['rejected']}<br>"
                f"<b>Top artists (30 days)</b> {top}<br><b>Top genres</b> {genres}")
        run_async(lambda: sp.tools.memory.snapshot(), show,
                  lambda e: self.music_label.setText(f"Couldn't read music memory: {e}"))

"""
ui/memory_ui.py

Inspect, edit and delete what SAINT remembers. Shows exactly what is stored
in the memory databases — long-term facts/preferences and Spotify listening
memory — nothing inferred.
"""

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.events import event_bus, EventType
from ui.widgets import Card, run_async


class MemoryUI(QWidget):
    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(14)

        head = QHBoxLayout()
        t = QLabel("Memory")
        t.setObjectName("PageTitle")
        head.addWidget(t)
        head.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search memories…")
        self.search.textChanged.connect(self.refresh)
        self.category = QComboBox()
        self.category.addItems(["All", "preference", "personal", "fact", "project"])
        self.category.currentTextChanged.connect(self.refresh)
        head.addWidget(self.search)
        head.addWidget(self.category)
        root.addLayout(head)

        sub = QLabel("Long-term facts SAINT stored because you told it. Say “forget …” or delete them here. "
                     "Short-term conversation context is kept in memory only for the current session.")
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        root.addWidget(sub)

        card = Card("Long-term memory")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Category", "Memory", "Updated", "ID"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.doubleClicked.connect(self.edit_selected)
        card.body.addWidget(self.table)
        btns = QHBoxLayout()
        add = QPushButton("Add…")
        add.clicked.connect(self.add)
        edit = QPushButton("Edit…")
        edit.clicked.connect(self.edit_selected)
        delete = QPushButton("Delete")
        delete.setObjectName("Danger")
        delete.clicked.connect(self.delete_selected)
        self.count = QLabel("")
        self.count.setObjectName("Faint")
        btns.addWidget(add)
        btns.addWidget(edit)
        btns.addWidget(delete)
        btns.addStretch()
        btns.addWidget(self.count)
        card.body.addLayout(btns)
        root.addWidget(card, 3)

        self.music = Card("Music memory (Spotify)")
        self.music_label = QLabel("")
        self.music_label.setWordWrap(True)
        self.music_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.music.body.addWidget(self.music_label)
        mb = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_music)
        mb.addWidget(refresh)
        mb.addStretch()
        self.music.body.addLayout(mb)
        root.addWidget(self.music, 1)

        event_bus.event_occurred.connect(self._on_event)
        self.refresh()
        self.refresh_music()

    def _on_event(self, ev):
        if ev.type in (EventType.MEMORY_STORED, EventType.MEMORY_UPDATED, EventType.MEMORY_DELETED):
            self.refresh()

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()
        self.refresh_music()

    # ------------------------------------------------------------------ #
    def refresh(self):
        from modules.memory.service import memory_service
        cat = self.category.currentText()
        query = self.search.text().strip().lower()

        def load():
            items = memory_service.all(None if cat == "All" else cat)
            if query:
                items = [e for e in items if query in e.content.lower()]
            return items

        def show(items):
            self.table.setRowCount(0)
            for e in items:
                r = self.table.rowCount()
                self.table.insertRow(r)
                self.table.setItem(r, 0, QTableWidgetItem(e.metadata.get("category", e.type.value)))
                self.table.setItem(r, 1, QTableWidgetItem(e.content))
                self.table.setItem(r, 2, QTableWidgetItem(datetime.fromtimestamp(e.updated_at).strftime("%Y-%m-%d %H:%M")))
                self.table.setItem(r, 3, QTableWidgetItem(str(e.id)))
            self.count.setText(f"{len(items)} stored")
        run_async(load, show)

    def _selected_id(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return int(self.table.item(rows[0].row(), 3).text())

    def add(self):
        text, ok = QInputDialog.getText(self, "Add memory", "What should SAINT remember?\n"
                                        "(e.g. “My favorite programming language is Python”)")
        if ok and text.strip():
            from modules.memory.service import memory_service, extract_fact
            fact = extract_fact(text) or {}
            memory_service.remember(content=fact.get("content", text.strip()), key=fact.get("key", ""),
                                    value=fact.get("value", ""), category=fact.get("category", "fact"))

    def edit_selected(self):
        mid = self._selected_id()
        if mid is None:
            return
        from modules.memory.service import memory_service, extract_fact
        entry = memory_service.db.retrieve(mid)
        if entry is None:
            return
        text, ok = QInputDialog.getText(self, "Edit memory", "Memory:", text=entry.content)
        if ok and text.strip():
            fact = extract_fact(text)
            memory_service.update(mid, content=text.strip(), value=fact["value"] if fact else None)

    def delete_selected(self):
        mid = self._selected_id()
        if mid is None:
            return
        if QMessageBox.question(self, "Delete memory", "Delete this memory permanently?") == QMessageBox.Yes:
            from modules.memory.service import memory_service
            memory_service.forget(mid)

    def refresh_music(self):
        from core.module_manager import module_manager
        sp = module_manager.get("spotify")

        def load():
            return sp.tools.memory.snapshot()

        def show(s):
            top = ", ".join(f"{a['artist']} ({a['plays']})" for a in s["top_artists"][:6]) or "—"
            genres = ", ".join(g["genre"] for g in s["top_genres"][:6]) or "—"
            reqs = ", ".join(r["query"] for r in s["recent_requests"][:5]) or "—"
            recs = s["recommendations"]
            self.music_label.setText(
                f"<b>Plays today:</b> {len(s['today'])}<br>"
                f"<b>Top artists (30 days):</b> {top}<br>"
                f"<b>Top genres:</b> {genres}<br>"
                f"<b>Recent requests:</b> {reqs}<br>"
                f"<b>Skips (7 days):</b> {s['skips_7d']} · "
                f"<b>Recommendations:</b> {recs['accepted']} kept, {recs['rejected']} skipped")
        run_async(load, show, lambda e: self.music_label.setText(f"Couldn't read music memory: {e}"))

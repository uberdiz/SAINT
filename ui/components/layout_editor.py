"""
ui/components/layout_editor.py

Choose what shows and in what order — the sidebar's pages and the Overview's panels. A tick shows
an item, drag (or the arrows) reorders; every change applies immediately and is saved
(``layout.<name>_hidden`` / ``layout.<name>_order``). Items marked fixed can be moved but not hidden.
"""

from typing import Callable, List, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

from core.config import config


class OrderedChecklist(QWidget):
    changed = Signal()

    def __init__(self, items: List[Tuple[str, str]], hidden_key: str, order_key: str, fixed=(), parent=None):
        """items: (key, label) in their default order."""
        super().__init__(parent)
        self._items = items
        self._hidden_key, self._order_key = hidden_key, order_key
        self._fixed = set(fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.list = QListWidget()
        self.list.setObjectName("LayoutList")
        self.list.setDragDropMode(QListWidget.InternalMove)
        self.list.setMaximumHeight(min(320, 30 * len(items) + 12))
        self.list.model().rowsMoved.connect(lambda *_: self._save())
        self.list.itemChanged.connect(lambda _it: self._save())
        lay.addWidget(self.list, 1)
        col = QVBoxLayout()
        up = QPushButton("▲")
        up.setToolTip("Move up")
        down = QPushButton("▼")
        down.setToolTip("Move down")
        reset = QPushButton("Reset")
        reset.setObjectName("Ghost")
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        reset.clicked.connect(self.reset)
        for b in (up, down, reset):
            col.addWidget(b)
        col.addStretch()
        lay.addLayout(col)
        self.load()

    def load(self):
        order = [k for k in (config.get(self._order_key, []) or []) if any(k == i[0] for i in self._items)]
        keys = order + [k for k, _l in self._items if k not in order]
        hidden = set(config.get(self._hidden_key, []) or [])
        labels = dict(self._items)
        self.list.blockSignals(True)
        self.list.clear()
        for k in keys:
            it = QListWidgetItem(labels[k] + ("  (always shown)" if k in self._fixed else ""))
            it.setData(Qt.UserRole, k)
            flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled
            if k not in self._fixed:
                flags |= Qt.ItemIsUserCheckable
            it.setFlags(flags)
            it.setCheckState(Qt.Checked if k in self._fixed or k not in hidden else Qt.Unchecked)
            self.list.addItem(it)
        self.list.blockSignals(False)

    def values(self) -> Tuple[List[str], List[str]]:
        order, hidden = [], []
        for i in range(self.list.count()):
            it = self.list.item(i)
            k = it.data(Qt.UserRole)
            order.append(k)
            if k not in self._fixed and it.checkState() != Qt.Checked:
                hidden.append(k)
        return order, hidden

    def _save(self):
        order, hidden = self.values()
        config.set(self._order_key, order)
        config.set(self._hidden_key, hidden)
        self.changed.emit()

    def _move(self, d: int):
        row = self.list.currentRow()
        new = row + d
        if row < 0 or not 0 <= new < self.list.count():
            return
        it = self.list.takeItem(row)
        self.list.insertItem(new, it)
        self.list.setCurrentRow(new)
        self._save()

    def reset(self):
        config.set(self._order_key, [])
        config.set(self._hidden_key, [])
        self.load()
        self.changed.emit()

"""
ui/palette.py

Ctrl+K command palette: jump to a page, run a scene, flip a toggle, control
music — or, if nothing matches, send what you typed to SAINT.
"""

from dataclasses import dataclass
from typing import Callable, List

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from ui import icons, motion
from ui.theme import current_palette
from ui.widgets import kbd


@dataclass
class Command:
    title: str
    hint: str
    icon: str
    run: Callable[[], None]


def score(query: str, text: str) -> int:
    """Prefix > substring > in-order letters (fewer gaps is better). 0 = no match."""
    q, t = query.lower().strip(), text.lower()
    if not q:
        return 1
    if t.startswith(q):
        return 1000 - len(t)
    i = t.find(q)
    if i >= 0:
        return 800 - i
    pos, gaps = 0, 0
    for ch in q:
        j = t.find(ch, pos)
        if j < 0:
            return 0
        gaps += j - pos
        pos = j + 1
    return max(1, 400 - gaps * 10)


class CommandPalette(QWidget):
    def __init__(self, parent, provider: Callable[[], List[Command]], ask: Callable[[str], None]):
        super().__init__(parent)
        self.provider, self.ask = provider, ask
        self._items: List[Command] = []
        self.hide()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addSpacing(90)
        self.panel = QFrame()
        self.panel.setObjectName("PalettePanel")
        self.panel.setFixedWidth(620)
        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(0, 0, 0, 8)
        pl.setSpacing(0)
        self.input = QLineEdit()
        self.input.setObjectName("PaletteInput")
        self.input.setPlaceholderText("Search pages, scenes and actions — or ask SAINT…")
        self.input.textChanged.connect(self._filter)
        self.input.installEventFilter(self)
        pl.addWidget(self.input)
        self.list = QListWidget()
        self.list.setObjectName("PaletteList")
        self.list.setIconSize(QSize(16, 16))
        self.list.setFixedHeight(340)
        self.list.itemActivated.connect(lambda _i: self._run())
        self.list.itemClicked.connect(lambda _i: self._run())
        pl.addWidget(self.list)
        foot = QHBoxLayout()
        foot.setContentsMargins(14, 4, 14, 0)
        foot.setSpacing(6)
        for key, label in (("↑↓", "navigate"), ("↵", "run"), ("esc", "close")):
            foot.addWidget(kbd(key))
            lab = QLabel(label)
            lab.setObjectName("Faint")
            foot.addWidget(lab)
            foot.addSpacing(8)
        foot.addStretch()
        pl.addLayout(foot)
        outer.addWidget(self.panel, 0, Qt.AlignHCenter)
        outer.addStretch()

    # ------------------------------------------------------------------ #
    def open(self):
        self.setGeometry(self.parentWidget().rect())
        self._items = self.provider()
        self.input.clear()
        self._filter("")
        self.show()
        self.raise_()
        self.input.setFocus()
        motion.fade_in(self.panel, motion.FAST)

    def close_palette(self):
        self.hide()

    def _filter(self, text):
        p = current_palette()
        ranked = sorted(((score(text, f"{c.title} {c.hint}"), c) for c in self._items), key=lambda x: -x[0])
        self.list.clear()
        for s, c in ranked:
            if s <= 0:
                continue
            self._add(c, p)
        if text.strip():
            self._add(Command(f"Ask SAINT: “{text.strip()}”", "Send as a request", "send",
                              lambda t=text.strip(): self.ask(t)), p)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _add(self, c: Command, p):
        it = QListWidgetItem(icons.icon(c.icon, p.muted, 16), f"{c.title}")
        it.setToolTip(c.hint)
        it.setData(Qt.UserRole, c)
        it.setSizeHint(QSize(0, 38))
        self.list.addItem(it)

    def _run(self):
        it = self.list.currentItem()
        if it is None:
            return
        cmd = it.data(Qt.UserRole)
        self.close_palette()
        cmd.run()

    def eventFilter(self, obj, e):
        if obj is self.input and e.type() == QEvent.KeyPress:
            k = e.key()
            if k == Qt.Key_Escape:
                self.close_palette()
                return True
            if k in (Qt.Key_Down, Qt.Key_Up):
                row = self.list.currentRow() + (1 if k == Qt.Key_Down else -1)
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, row)))
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter):
                self._run()
                return True
        return super().eventFilter(obj, e)

    def paintEvent(self, _):
        g = QPainter(self)
        g.fillRect(self.rect(), QColor(0, 0, 0, 120 if current_palette().dark else 60))
        g.end()

    def mousePressEvent(self, e):
        if not self.panel.geometry().contains(e.position().toPoint()):
            self.close_palette()


if __name__ == "__main__":
    assert score("mus", "Music") > score("mus", "Resume music") > 0
    assert score("hst", "History") > 0 and score("xyz", "History") == 0
    assert score("", "anything") == 1
    print("palette ok")

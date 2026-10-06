"""
ui/components/sidebar.py

Navigation. Pages come from ``PAGES`` (key, label, icon, section); which ones show, and in what
order, is the user's (Settings › Appearance › Layout: ``layout.sidebar_hidden`` /
``layout.sidebar_order``). Overview and Settings can't be hidden. Badges show live counts (a
running task, connected devices). Collapsed = icons only.
"""

from typing import Dict, List, Tuple

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout)

from core.config import config
from ui import icons, motion
from ui.theme import current_palette
from ui.widgets import ElidedLabel, IconButton, Orb

# key, label, icon, section
PAGES: List[Tuple[str, str, str, str]] = [
    ("Overview", "Overview", "overview", "SAINT"),
    ("Activity", "Activity", "activity", "SAINT"),
    ("Tasks", "Tasks", "tasks", "SAINT"),
    ("Music", "Music", "music", "Everyday"),
    ("Devices", "Devices", "smartphone", "Everyday"),
    ("Memory", "Memory", "memory", "Everyday"),
    ("Automations", "Automations", "zap", "Everyday"),
    ("History", "History", "history", "Everyday"),
    ("Storage", "Storage", "drive", "PC"),
    ("System", "System", "cpu", "PC"),
    ("Settings", "Settings", "settings", ""),
]
FIXED = ("Overview", "Settings")


def ordered_pages() -> List[Tuple[str, str, str, str]]:
    """The pages in the user's order, hidden ones left out (Overview and Settings always shown)."""
    order = [k for k in (config.get("layout.sidebar_order", []) or []) if any(k == p[0] for p in PAGES)]
    keys = order + [p[0] for p in PAGES if p[0] not in order]
    hidden = set(config.get("layout.sidebar_hidden", []) or []) - set(FIXED)
    by_key = {p[0]: p for p in PAGES}
    pages = [by_key[k] for k in keys if k not in hidden and k != "Settings"]
    return pages + [by_key["Settings"]]


class Sidebar(QFrame):
    navigated = Signal(str)
    search = Signal()

    def __init__(self, logo_pixmap):
        super().__init__()
        self.setObjectName("Sidebar")
        self._logo_pixmap = logo_pixmap
        self.pill = QFrame(self)
        self.pill.setObjectName("NavPill")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(12, 18, 12, 12)
        self.lay.setSpacing(2)

        brand = QHBoxLayout()
        brand.setContentsMargins(6, 0, 0, 12)
        brand.setSpacing(9)
        self.logo = QLabel()
        self.word = QLabel("SAINT")
        self.word.setObjectName("Wordmark")
        brand.addWidget(self.logo)
        brand.addWidget(self.word, 1)
        self.lay.addLayout(brand)

        self.search_btn = QPushButton("  Search")
        self.search_btn.setObjectName("SearchButton")
        self.search_btn.setCursor(Qt.PointingHandCursor)
        self.search_btn.clicked.connect(self.search)
        sl = QHBoxLayout(self.search_btn)
        sl.setContentsMargins(0, 0, 8, 0)
        sl.addStretch()
        self.search_hint = QLabel("Ctrl K")
        self.search_hint.setObjectName("Kbd")
        sl.addWidget(self.search_hint, 0, Qt.AlignVCenter)
        self.lay.addWidget(self.search_btn)
        self.lay.addSpacing(8)

        self.nav = QVBoxLayout()
        self.nav.setSpacing(2)
        self.lay.addLayout(self.nav, 1)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: Dict[str, QPushButton] = {}
        self.badges: Dict[str, QLabel] = {}
        self._sections = []
        self._collapsed = False

        self.lay.addSpacing(8)
        status = QHBoxLayout()
        status.setContentsMargins(4, 0, 0, 0)
        status.setSpacing(8)
        self.orb = Orb(22)
        self.status = ElidedLabel("Starting…")
        self.status.setObjectName("Faint")
        self.mic = IconButton("mic", "Microphone on / off", 16)
        status.addWidget(self.orb)
        status.addWidget(self.status, 1)
        status.addWidget(self.mic)
        self.lay.addLayout(status)
        self.rebuild()

    # ------------------------------------------------------------------ #
    def rebuild(self):
        """(Re)create the nav buttons from the user's layout."""
        current = self.current()
        for b in list(self.buttons.values()):
            self.group.removeButton(b)
            b.deleteLater()
        for w in self._sections + list(self.badges.values()):
            w.deleteLater()
        while self.nav.count():
            item = self.nav.takeAt(0)
            if item.layout() is not None:
                item.layout().deleteLater()
        self.buttons, self.badges, self._sections = {}, {}, []
        section = None
        shortcut = 1
        for key, label, icon, sec in ordered_pages():
            if key == "Settings":
                self.nav.addStretch()
            elif sec != section and sec:
                head = QLabel(sec.upper())
                head.setObjectName("NavSection")
                head.setVisible(not self._collapsed)
                self._sections.append(head)
                self.nav.addWidget(head)
                section = sec
            b = QPushButton(label)
            b.setObjectName("NavButton")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setIconSize(QSize(17, 17))
            b.setToolTip(f"{label}  (Ctrl+{shortcut})" if shortcut <= 9 else label)
            shortcut += 1
            b.clicked.connect(lambda _=False, k=key: self.navigated.emit(k))
            row = QHBoxLayout(b)
            row.setContentsMargins(0, 0, 8, 0)
            row.addStretch()
            badge = QLabel("")
            badge.setObjectName("NavBadge")
            badge.hide()
            row.addWidget(badge, 0, Qt.AlignVCenter)
            self.badges[key] = badge
            self.group.addButton(b)
            self.buttons[key] = b
            self.nav.addWidget(b)
        self.refresh()
        self.set_collapsed(self._collapsed)
        if current in self.buttons:
            self.select(current, animate=False)

    def keys(self) -> List[str]:
        return list(self.buttons)

    def current(self) -> str:
        b = self.group.checkedButton() if hasattr(self, "group") else None
        return next((k for k, x in self.buttons.items() if x is b), "") if b else ""

    def select(self, key: str, animate: bool = True):
        b = self.buttons.get(key)
        if b is None:
            self.pill.hide()
            return
        b.setChecked(True)
        target = b.geometry()
        if animate and self.pill.isVisible() and self.pill.geometry().width() > 0:
            motion.animate(self.pill, b"geometry", self.pill.geometry(), target, motion.BASE)
        else:
            self.pill.setGeometry(target)
        self.pill.show()
        self.pill.lower()

    def set_badge(self, key: str, text: str):
        badge = self.badges.get(key)
        if badge is not None:
            badge.setText(text)
            badge.setVisible(bool(text) and not self._collapsed)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        checked = self.group.checkedButton()
        if checked:
            self.pill.setGeometry(checked.geometry())

    def set_collapsed(self, collapsed: bool):
        self._collapsed = collapsed
        self.setFixedWidth(68 if collapsed else 224)
        labels = {k: lab for k, lab, _i, _s in ordered_pages()}
        for k, b in self.buttons.items():
            b.setText("" if collapsed else labels.get(k, k))
        for w in self._sections:
            w.setVisible(not collapsed)
        for k, badge in self.badges.items():
            badge.setVisible(bool(badge.text()) and not collapsed)
        self.word.setVisible(not collapsed)
        self.search_btn.setText("" if collapsed else "  Search")
        self.search_hint.setVisible(not collapsed)
        self.status.setVisible(not collapsed)
        self.mic.setVisible(not collapsed)

    def refresh(self):
        p = current_palette()
        self.logo.setPixmap(self._logo_pixmap(24))
        self.search_btn.setIcon(icons.icon("search", p.faint, 15))
        icon_for = {k: ic for k, _l, ic, _s in PAGES}
        for k, b in self.buttons.items():
            b.setIcon(icons.icon(icon_for.get(k, "sparkles"), p.muted, 17, active_color=p.text))

    def render_state(self, s):
        self.orb.set_state(s.get("state", "offline"))
        self.status.setText(s.get("label", ""))

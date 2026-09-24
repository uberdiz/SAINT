"""
ui/pages/activity.py

Never hide anything: every event through SAINT, live, filterable. The same
events also go to the rotating log file (data/logs/saint.log).
"""

import logging

from PySide6.QtWidgets import QLineEdit, QPlainTextEdit

from core.logger import NOISY_EVENTS, read_recent_lines
from ui.reactive import ui_bus
from ui.widgets import IconButton, Page, Segmented

GROUPS = [
    ("All", ()),
    ("Voice", ("voice.", "wake.", "tts.")),
    ("Agent & tools", ("agent.", "tool.", "ai.", "conversation.", "automation.", "memory.")),
    ("Spotify", ("spotify.",)),
    ("Problems", ("error", "warning", ".failed", ".error", "crash")),
]


class ActivityPage(Page):
    def __init__(self):
        super().__init__("Activity", "Every event flowing through SAINT, live.")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter…")
        self.search.setFixedWidth(200)
        self.pause = IconButton("pause", "Pause the feed", 16, checkable=True)
        clear = IconButton("trash", "Clear", 16)
        clear.clicked.connect(lambda: self.output.clear())
        for w in (self.search, self.pause, clear):
            self.actions.addWidget(w)
        self.groups = Segmented([g[0] for g in GROUPS])
        self.root.addWidget(self.groups)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setObjectName("ConsoleOutput")
        self.output.setMaximumBlockCount(3000)
        self.root.addWidget(self.output, 1)
        for line in read_recent_lines(200):
            self.output.appendPlainText(line.rstrip("\n"))
        ui_bus.event.connect(self._on_event)

    def _match(self, text: str) -> bool:
        keys = GROUPS[max(0, self.groups.index())][1]
        if keys and not any(k in text for k in keys):
            return False
        q = self.search.text().strip().lower()
        return not q or q in text.lower()

    def _on_event(self, ev):
        if self.pause.isChecked():
            return
        if ev.type in NOISY_EVENTS and not logging.getLogger("saint").isEnabledFor(logging.DEBUG):
            return
        line = f"{ev.formatted_time()}  {ev.type}" + (f"  {ev.payload}" if ev.payload else "")
        if self._match(line):
            self.output.appendPlainText(line)

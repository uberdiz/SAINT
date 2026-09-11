"""
ui/console_ui.py

Never hide anything. Every event that flows through the Event Bus is
appended here live, in addition to being written to the rotating log
file on disk.
"""

from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPlainTextEdit, QPushButton, QHBoxLayout

from core.events import event_bus
from core.logger import read_recent_lines


class ConsoleUI(QWidget):
    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        header_row = QHBoxLayout()
        title = QLabel("Console")
        title.setObjectName("PageTitle")
        header_row.addWidget(title)
        header_row.addStretch()

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.clear)
        header_row.addWidget(clear_btn)
        root.addLayout(header_row)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setObjectName("ConsoleOutput")
        root.addWidget(self.output)

        # backfill from disk so history survives a page switch / restart
        for line in read_recent_lines(200):
            self.output.appendPlainText(line.rstrip("\n"))

        event_bus.event_occurred.connect(self._on_event)

    def _on_event(self, ev):
        msg = f"{ev.formatted_time()}  {ev.type}"
        if ev.payload:
            msg += f"  {ev.payload}"
        self.output.appendPlainText(msg)

    def clear(self):
        self.output.clear()

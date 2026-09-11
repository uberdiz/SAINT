"""
ui/dashboard.py

Home screen. Clean, no fancy graphics -- just the status cards
described in the spec, plus a small "try it" panel to exercise the AI
module directly from the Dashboard.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
    QLineEdit, QPushButton, QTextEdit, QSizePolicy,
)
from PySide6.QtCore import Qt, QTimer

from core.state import app_state
from core.module_manager import module_manager
from ui.workers import AIWorker


class StatCard(QFrame):
    def __init__(self, title, value="--"):
        super().__init__()
        self.setObjectName("StatCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("StatTitle")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("StatValue")

        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)

    def set_value(self, value):
        self.value_label.setText(str(value))


class Dashboard(QWidget):
    def __init__(self):
        super().__init__()
        self.ai_module = module_manager.get("ai")
        self._worker = None

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(20)

        header = QLabel("SAINT")
        header.setObjectName("PageTitle")
        root.addWidget(header)

        # --- Stat grid -------------------------------------------------
        grid = QGridLayout()
        grid.setSpacing(14)

        self.card_status = StatCard("Status", "Running")
        self.card_modules = StatCard("Modules Loaded", "0 / 0")
        self.card_cpu = StatCard("CPU", "0%")
        self.card_ram = StatCard("RAM", "0 MB")
        self.card_uptime = StatCard("Uptime", "00:00:00")
        self.card_events = StatCard("Events", "0")
        self.card_errors = StatCard("Errors", "0")

        cards = [
            self.card_status, self.card_modules, self.card_cpu,
            self.card_ram, self.card_uptime, self.card_events, self.card_errors,
        ]
        for i, card in enumerate(cards):
            grid.addWidget(card, i // 4, i % 4)

        root.addLayout(grid)

        # --- AI quick-test panel ----------------------------------------
        panel_title = QLabel("Try the AI Module")
        panel_title.setObjectName("SectionTitle")
        root.addWidget(panel_title)

        input_row = QHBoxLayout()
        self.prompt_input = QLineEdit()
        self.prompt_input.setPlaceholderText("Type a prompt and press Send...")
        self.prompt_input.returnPressed.connect(self.send_prompt)
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self.send_prompt)
        input_row.addWidget(self.prompt_input)
        input_row.addWidget(self.send_button)
        root.addLayout(input_row)

        self.response_area = QTextEdit()
        self.response_area.setReadOnly(True)
        self.response_area.setPlaceholderText("AI responses will appear here...")
        self.response_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self.response_area)

        root.addStretch()

        # --- refresh timer ----------------------------------------------
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    def refresh(self):
        self.card_cpu.set_value(f"{app_state.cpu_percent():.0f}%")
        self.card_ram.set_value(f"{app_state.ram_mb():.0f} MB")
        self.card_uptime.set_value(app_state.uptime_formatted())
        self.card_events.set_value(app_state.event_count)
        self.card_errors.set_value(app_state.error_count)
        self.card_modules.set_value(f"{module_manager.loaded_count()} / {module_manager.total_count()}")

    def send_prompt(self):
        prompt = self.prompt_input.text().strip()
        if not prompt:
            return
        if self._worker is not None and self._worker.isRunning():
            return  # a request is already in flight

        self.send_button.setEnabled(False)
        self.response_area.append(f"> {prompt}")
        self.prompt_input.clear()

        self._worker = AIWorker(self.ai_module, prompt)
        self._worker.finished_ok.connect(self._on_response)
        self._worker.finished_error.connect(self._on_error)
        self._worker.finished.connect(lambda: self.send_button.setEnabled(True))
        self._worker.start()

    def _on_response(self, text):
        self.response_area.append(text + "\n")

    def _on_error(self, err):
        self.response_area.append(f"[error] {err}\n")

"""
ui/health_ui.py

One screen dedicated to internal health -- status, warnings, critical
errors, slow modules, and basic performance figures.
"""

from PySide6.QtWidgets import QWidget, QVBoxLayout, QGridLayout, QLabel, QFrame
from PySide6.QtCore import QTimer

from core.state import app_state
from core.analytics import analytics


class HealthTile(QFrame):
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

    def set_value(self, value, warn=False):
        self.value_label.setText(str(value))
        self.value_label.setObjectName("StatValueWarn" if warn else "StatValue")
        self.value_label.setStyleSheet("")  # force style re-eval in some Qt versions


class HealthUI(QWidget):
    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(20)

        title = QLabel("Health Monitor")
        title.setObjectName("PageTitle")
        root.addWidget(title)

        grid = QGridLayout()
        grid.setSpacing(14)

        self.tile_status = HealthTile("Status", "Healthy")
        self.tile_warnings = HealthTile("Warnings", "0")
        self.tile_critical = HealthTile("Critical Errors", "0")
        self.tile_slow_modules = HealthTile("Slow Modules", "None")
        self.tile_avg_response = HealthTile("Average Response", "0 sec")
        self.tile_memory_leaks = HealthTile("Memory Leaks", "No")

        tiles = [
            self.tile_status, self.tile_warnings, self.tile_critical,
            self.tile_slow_modules, self.tile_avg_response, self.tile_memory_leaks,
        ]
        for i, tile in enumerate(tiles):
            grid.addWidget(tile, i // 3, i % 3)

        root.addLayout(grid)
        root.addStretch()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    def refresh(self):
        errors = app_state.error_count
        crashes = app_state.module_crash_count
        snap = analytics.snapshot()

        healthy = errors == 0 and crashes == 0
        self.tile_status.set_value("Healthy" if healthy else "Degraded", warn=not healthy)
        self.tile_warnings.set_value(0)  # reserved: distinct warning-level events
        self.tile_critical.set_value(crashes, warn=crashes > 0)
        self.tile_slow_modules.set_value(
            "None" if snap["average_response"] < 5 else "AI (slow responses)"
        )
        self.tile_avg_response.set_value(f'{snap["average_response"]} sec')
        self.tile_memory_leaks.set_value(
            "No" if app_state.ram_mb() < 500 else "Investigate"
        )

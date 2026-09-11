"""
ui/main_window.py

Top-level window: a sidebar for navigation (Dashboard, Module Manager,
Analytics, Health, Console, Settings) and a stacked widget holding
each page. Sidebar + stack is the whole shell -- every future feature
becomes "just another page".
"""

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QListWidget, QListWidgetItem, QStackedWidget,
)
from PySide6.QtCore import Qt

from ui.dashboard import Dashboard
from ui.module_manager_ui import ModuleManagerUI
from ui.analytics_ui import AnalyticsUI
from ui.health_ui import HealthUI
from ui.console_ui import ConsoleUI
from ui.settings_ui import SettingsUI
from ui.voice_ui import VoiceUI
from ui.theme import stylesheet_for

from core.config import config
from core.events import event_bus, EventType


PAGES = ["Dashboard", "Voice", "Module Manager", "Analytics", "Health", "Console", "Settings"]


class MainWindow(QMainWindow):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle("SAINT — v0.1 Core")
        self.resize(1100, 720)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setCentralWidget(central)

        # --- Sidebar -----------------------------------------------------
        self.sidebar = QListWidget()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(190)
        for name in PAGES:
            QListWidgetItem(name, self.sidebar)
        self.sidebar.currentRowChanged.connect(self._on_nav_changed)
        layout.addWidget(self.sidebar)

        # --- Pages ---------------------------------------------------------
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        self.dashboard = Dashboard()
        self.voice_ui = VoiceUI()
        self.module_manager_ui = ModuleManagerUI()
        self.analytics_ui = AnalyticsUI()
        self.health_ui = HealthUI()
        self.console_ui = ConsoleUI()
        self.settings_ui = SettingsUI(on_theme_changed=self.apply_theme)

        for page in [
            self.dashboard, self.voice_ui, self.module_manager_ui,
            self.analytics_ui, self.health_ui, self.console_ui, self.settings_ui,
        ]:
            self.stack.addWidget(page)

        self.sidebar.setCurrentRow(0)

        event_bus.emit_event(EventType.APP_STARTED, {})

    def _on_nav_changed(self, index):
        self.stack.setCurrentIndex(index)
        event_bus.emit_event(EventType.UI_UPDATED, {"page": PAGES[index]})

    def apply_theme(self, theme_name):
        self.app.setStyleSheet(stylesheet_for(theme_name))

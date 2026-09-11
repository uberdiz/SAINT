#!/usr/bin/env python3
"""
app.py

SAINT v0.1 "Core" -- entry point.

Boot order matters:
    1. Config      (so everyone else can read settings)
    2. Logger      (so nothing that happens next goes unrecorded)
    3. Event Bus   (already a singleton, imported by everything)
    4. State / Analytics (subscribe to the event bus)
    5. Module Manager (loads/enables modules per config)
    6. Qt Application + Main Window
"""

import sys

from core.config import config
from core.logger import init_logger

init_logger(config.get("logging.level", "Verbose"))

# Importing these triggers their singleton construction, which wires
# them up to the event bus.
from core import state as _state          # noqa: F401,E402
from core import analytics as _analytics  # noqa: F401,E402
from core.module_manager import module_manager  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.theme import stylesheet_for  # noqa: E402


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("SAINT")

    theme = config.get("theme", "Dark")
    app.setStyleSheet(stylesheet_for(theme))

    window = MainWindow(app)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

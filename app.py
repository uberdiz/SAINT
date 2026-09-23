#!/usr/bin/env python3
"""
app.py

SAINT Revitalized -- entry point.

Boot order matters:
    1. Config      (so everyone else can read settings)
    2. Logger      (so nothing that happens next goes unrecorded)
    3. Event Bus   (already a singleton, imported by everything)
    4. State / Analytics (subscribe to the event bus)
    5. Module Manager (loads/enables modules per config)
    6. First-run Setup (if needed)
    7. Qt Application + Main Window
"""

import sys
import os

# High-DPI configuration.
#
# Qt 6 already makes the process per-monitor DPI aware (V2) by default when the
# QGuiApplication is constructed. We must NOT also call
# ctypes.windll.shcore.SetProcessDpiAwareness() here: Windows only allows a
# process's DPI awareness to be set once, so setting it manually before Qt
# starts causes Qt's own (newer, better) SetProcessDpiAwarenessContext() call to
# fail with "Access is denied" — the warning we were seeing. Letting Qt own DPI
# awareness both removes the warning and keeps the superior V2 behaviour.
#
# The only thing worth setting explicitly is the rounding policy, which must be
# configured before the QApplication is instantiated.
try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
except Exception:
    pass

from core.config import config
from core.logger import init_logger
from core.setup import run_first_run_setup

init_logger(config.get("logging.level", "Verbose"))

# Importing these triggers their singleton construction, which wires
# them up to the event bus.
from core import state as _state          # noqa: F401,E402
from core import analytics as _analytics  # noqa: F401,E402
from core.module_manager import module_manager  # noqa: E402

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.theme import stylesheet_for  # noqa: E402


def show_setup_dialog(setup_result: dict):
    """Show first-run setup results to user."""
    app = QApplication.instance()
    if not app:
        app = QApplication(sys.argv)
    
    wizard = SetupWizard()
    # Re-run checks to get fresh results for display
    wizard.run_checks()
    report = wizard.format_report()
    
    msg = QMessageBox()
    msg.setWindowTitle("SAINT First Run Setup")
    msg.setText("First-time setup complete!")
    msg.setDetailedText(report)
    msg.setStandardButtons(QMessageBox.Ok)
    msg.exec()


def main():
    # Run first-run setup before creating the UI
    setup_result = run_first_run_setup()
    
    app = QApplication(sys.argv)
    app.setApplicationName("SAINT")

    theme = config.get("theme", "Dark")
    app.setStyleSheet(stylesheet_for(theme))

    window = MainWindow(app)
    window.show()

    # Show setup dialog after window is shown (if first run)
    if setup_result.get("first_run") and not setup_result.get("skipped"):
        # Use a timer to show dialog after event loop starts
        from PySide6.QtCore import QTimer
        QTimer.singleShot(500, lambda: show_setup_dialog(setup_result))

    sys.exit(app.exec())


# Need to import SetupWizard for the dialog
from core.setup import SetupWizard


if __name__ == "__main__":
    main()

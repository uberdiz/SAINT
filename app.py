#!/usr/bin/env python3
"""
app.py — SAINT entry point.

Boot order:
    1. Config + logging
    2. Module manager (enables modules per config, registers tools)
    3. Qt application (single instance, keeps running in the tray)
    4. Core runtime: TTS, conversation controller, wake-word listening,
       scheduler — independent of the window
    5. Main window (optional: `python app.py --background` starts hidden)
"""

import argparse
import sys

# Qt 6 owns per-monitor DPI awareness; only the rounding policy is set here
# (it must be configured before the QApplication exists).
try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
except Exception:
    pass

from core.config import config
from core.logger import init_logger

init_logger(config.get("logging.level", "Normal"), config.get("logging.debug", False))

import logging  # noqa: E402

from core import state as _state          # noqa: F401,E402
from core import analytics as _analytics  # noqa: F401,E402
from core import history as _history      # noqa: F401,E402  (local usage log for the History page)
from core.paths import data_path          # noqa: E402
from core.setup import run_first_run_setup, SetupWizard  # noqa: E402

log = logging.getLogger("saint.app")


def show_setup_dialog():
    from PySide6.QtWidgets import QMessageBox
    wizard = SetupWizard()
    wizard.run_checks()
    msg = QMessageBox()
    msg.setWindowTitle("SAINT first-run check")
    msg.setText("Welcome to SAINT. Here's what I found on this PC.")
    msg.setDetailedText(wizard.format_report())
    msg.setStandardButtons(QMessageBox.Ok)
    msg.exec()


def main():
    parser = argparse.ArgumentParser(description="SAINT local AI assistant")
    parser.add_argument("--background", action="store_true", help="start hidden in the system tray")
    args = parser.parse_args()

    from PySide6.QtCore import QLockFile, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    app.setApplicationName("SAINT")
    app.setQuitOnLastWindowClosed(False)   # the tray keeps SAINT alive

    lock = QLockFile(str(data_path("saint.lock")))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, "SAINT", "SAINT is already running (check the system tray).")
        return 0

    setup_result = run_first_run_setup()

    try:
        from core.device import log_diagnostics
        log_diagnostics(logging.getLogger("saint.device"))
    except Exception:
        pass

    from core.module_manager import module_manager  # noqa: F401  (enables modules, registers tools)
    from core.runtime import runtime
    runtime.start()

    from ui.main_window import MainWindow
    window = MainWindow(app, runtime)
    if not (args.background or config.get("notifications.start_minimized", False)):
        window.show()
    if setup_result.get("first_run") and not setup_result.get("skipped"):
        QTimer.singleShot(800, show_setup_dialog)

    code = app.exec()
    runtime.shutdown()
    lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())

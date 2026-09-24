"""
ui/main_window.py

Top-level window: sidebar navigation with a live state indicator, the page
stack, and a system-tray icon so SAINT keeps running (and listening) in the
background when the window is closed or minimised.
"""

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from core.assistant_state import assistant_state
from core.config import config
from core.events import event_bus, EventType
from ui.theme import build_stylesheet, current_palette, state_color
from ui.widgets import StatusDot

PAGES = [
    ("Dashboard", "◉"),
    ("Spotify", "♫"),
    ("Memory", "✦"),
    ("Automations", "◷"),
    ("Activity", "≡"),
    ("Modules", "▦"),
    ("Health", "♥"),
    ("Analytics", "↗"),
    ("Settings", "⛭"),
]


LOGO_PATH = "SAINT.png"       # the SAINT logo shipped in the repository root


def logo_pixmap(size: int = 64, dot_color: str = None) -> QPixmap:
    """The SAINT logo, optionally with a small status dot (tray icon)."""
    from core.paths import resolve_project_path
    src = QPixmap(str(resolve_project_path(LOGO_PATH)))
    if src.isNull():
        return make_icon(dot_color or "#feaa34", _fallback=True).pixmap(size, size)
    pm = src.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    if dot_color:
        out = QPixmap(size, size)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)
        p.drawPixmap((size - pm.width()) // 2, (size - pm.height()) // 2, pm)
        r = size // 3
        p.setPen(QColor("#101215"))
        p.setBrush(QColor(dot_color))
        p.drawEllipse(size - r - 1, size - r - 1, r, r)
        p.end()
        return out
    return pm


def make_icon(color: str, _fallback: bool = False) -> QIcon:
    if not _fallback:
        return QIcon(logo_pixmap(64, color))
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(6, 6, 52, 52)
    p.setPen(QColor("#ffffff"))
    f = QFont("Segoe UI", 26, QFont.Bold)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "S")
    p.end()
    return QIcon(pm)


class MainWindow(QMainWindow):
    def __init__(self, app, runtime=None):
        super().__init__()
        self.app = app
        self.runtime = runtime
        self._quitting = False
        self.setWindowTitle("SAINT")
        self.resize(1280, 820)
        self.setMinimumSize(980, 640)

        from ui.dashboard import Dashboard
        from ui.spotify_ui import SpotifyUI
        from ui.memory_ui import MemoryUI
        from ui.automations_ui import AutomationsUI
        from ui.console_ui import ConsoleUI
        from ui.module_manager_ui import ModuleManagerUI
        from ui.health_ui import HealthUI
        from ui.analytics_ui import AnalyticsUI
        from ui.settings_ui import SettingsUI

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setCentralWidget(central)

        # ---- sidebar ----------------------------------------------------
        self.side = QWidget()
        self.side.setObjectName("SidebarPanel")
        sl = QVBoxLayout(self.side)
        sl.setContentsMargins(0, 16, 0, 12)
        sl.setSpacing(6)
        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(18, 0, 12, 8)
        brand_row.setSpacing(10)
        self.brand_logo = QLabel()           # SAINT's "S" badge, in the accent colour
        self.brand = QLabel("SAINT")
        self.brand.setStyleSheet("font-size: 18px; font-weight: 700; letter-spacing: 3px;")
        brand_row.addWidget(self.brand_logo)
        brand_row.addWidget(self.brand, 1)
        sl.addLayout(brand_row)
        self.sidebar = QListWidget()
        self.sidebar.setObjectName("Sidebar")
        for name, icon in PAGES:
            it = QListWidgetItem(f"{icon}   {name}")
            it.setData(Qt.UserRole, name)
            it.setToolTip(name)
            self.sidebar.addItem(it)
        self.sidebar.currentRowChanged.connect(self._on_nav)
        self.sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sidebar.setTextElideMode(Qt.ElideRight)
        sl.addWidget(self.sidebar, 1)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(18, 0, 12, 0)
        self.status_dot = StatusDot()
        self.status_text = QLabel("Starting…")
        self.status_text.setObjectName("Faint")
        self.status_text.setWordWrap(True)
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_text, 1)
        sl.addLayout(status_row)
        layout.addWidget(self.side)

        # ---- pages -----------------------------------------------------------
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.dashboard = Dashboard(runtime)
        self.settings_ui = SettingsUI(on_appearance_changed=self.apply_appearance)
        self.spotify_ui = SpotifyUI()
        pages = [self.dashboard, self.spotify_ui, MemoryUI(), AutomationsUI(), ConsoleUI(), ModuleManagerUI(),
                 HealthUI(), AnalyticsUI(), self.settings_ui]
        for page in pages:
            self.stack.addWidget(page)

        self._build_tray()
        self.apply_appearance()
        self.sidebar.setCurrentRow(0)
        event_bus.event_occurred.connect(self._on_event)
        self._render_state(assistant_state.snapshot())
        event_bus.emit_event(EventType.APP_STARTED, {})

    # ------------------------------------------------------------------ #
    def _on_nav(self, index):
        self.stack.setCurrentIndex(index)
        event_bus.emit_event(EventType.UI_UPDATED, {"page": PAGES[index][0]})

    def navigate(self, name):
        for i, (n, _) in enumerate(PAGES):
            if n == name:
                self.sidebar.setCurrentRow(i)

    # ------------------------------------------------------------------ #
    # Appearance
    # ------------------------------------------------------------------ #
    def apply_appearance(self):
        a = config.get("appearance", {}) or {}
        self.app.setStyleSheet(build_stylesheet(a.get("theme", "Dark"), a.get("accent", "#feaa34"),
                                                a.get("font_family", "Segoe UI"), a.get("font_size", 13),
                                                a.get("compact", False)))
        self.setWindowOpacity(max(0.6, min(1.0, float(a.get("opacity", 1.0)))))
        on_top = bool(a.get("always_on_top", False))
        if bool(self.windowFlags() & Qt.WindowStaysOnTopHint) != on_top:
            visible = self.isVisible()
            self.setWindowFlag(Qt.WindowStaysOnTopHint, on_top)
            if visible:
                self.show()
        labels = a.get("sidebar_labels", True)
        for i, (name, icon) in enumerate(PAGES):
            self.sidebar.item(i).setText(f"{icon}   {name}" if labels else icon)
        self.side.setFixedWidth(210 if labels else 72)
        self.brand.setVisible(labels)
        self.brand_logo.setPixmap(logo_pixmap(32))
        self.status_text.setVisible(labels)
        self.dashboard.apply_settings()
        self._render_state(assistant_state.snapshot())

    # ------------------------------------------------------------------ #
    # State + tray
    # ------------------------------------------------------------------ #
    def _render_state(self, s):
        pal = current_palette()
        color = state_color(s.get("state", "offline"), pal)
        self.status_dot.set_color(color)
        label = s.get("label", "")
        self.status_text.setText(label)
        if getattr(self, "tray", None):
            self.tray.setIcon(make_icon(color))
            self.tray.setToolTip(f"SAINT — {label}")
        self.setWindowIcon(QIcon(logo_pixmap(64)))

    def _build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(make_icon(current_palette().accent), self)
        menu = QMenu()
        show = QAction("Open SAINT", self)
        show.triggered.connect(self.show_normal)
        self._listen_action = QAction("Stop listening", self)
        self._listen_action.triggered.connect(self._toggle_listening)
        quit_ = QAction("Quit SAINT", self)
        quit_.triggered.connect(self.quit)
        menu.addAction(show)
        menu.addAction(self._listen_action)
        menu.addSeparator()
        menu.addAction(quit_)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_normal()
                                    if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()

    def _toggle_listening(self):
        if not self.runtime:
            return
        if self.runtime.listening:
            self.runtime.stop_listening()
        else:
            self.runtime.start_listening()

    def show_normal(self):
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def _on_event(self, ev):
        t, p = ev.type, ev.payload
        if t == EventType.ASSISTANT_STATE:
            self._render_state(p)
        elif t in (EventType.VOICE_LISTENING_START, EventType.VOICE_LISTENING_STOP):
            if getattr(self, "_listen_action", None):
                self._listen_action.setText("Stop listening" if t == EventType.VOICE_LISTENING_START
                                            else "Start listening")
        elif t == EventType.NOTIFY and self.tray and config.get("notifications.tray", True):
            self.tray.showMessage(p.get("title", "SAINT"), p.get("message", ""),
                                  QSystemTrayIcon.Information, 8000)
        elif t == EventType.AUTOMATION_FAILED and self.tray and config.get("notifications.tray", True):
            self.tray.showMessage("SAINT automation failed", f"{p.get('title')}: {p.get('error')}",
                                  QSystemTrayIcon.Warning, 8000)

    # ------------------------------------------------------------------ #
    def closeEvent(self, event):
        if not self._quitting and self.tray and config.get("notifications.close_to_tray", True):
            event.ignore()
            self.hide()
            if not getattr(self, "_tray_hint_shown", False):
                self._tray_hint_shown = True
                self.tray.showMessage("SAINT is still running",
                                      "SAINT keeps listening for “Hey SAINT” in the background. "
                                      "Right-click the tray icon to quit.", QSystemTrayIcon.Information, 5000)
            return
        self._quitting = True
        if self.runtime:
            self.runtime.shutdown()
        if self.tray:
            self.tray.hide()
        event.accept()
        QApplication.instance().quit()

    def quit(self):
        self._quitting = True
        self.close()

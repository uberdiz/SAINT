"""
ui/main_window.py

The SAINT shell: sidebar + pages, and everything that lives outside the
window — tray icon, the Halo, the overlay, the floating mini player, the
global hotkey, the command palette, toasts and demo mode.
"""

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu,
                               QPushButton, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

from core.config import config
from core.events import event_bus, EventType
from ui import actions, icons, motion
from ui.reactive import ui_bus
from ui.theme import build_stylesheet, current_palette, qt_palette, state_color
from ui.widgets import ElidedLabel, IconButton, Orb

PAGES = [("Home", "home"), ("Music", "music"), ("Automations", "zap"), ("History", "history"),
         ("Memory", "memory"), ("Activity", "activity"), ("System", "cpu"), ("Settings", "settings")]

LOGO_PATH = "SAINT.png"       # the SAINT logo shipped in the repository root


def logo_pixmap(size: int = 64, dot_color: str = None) -> QPixmap:
    """The SAINT logo, optionally with a small status dot (tray icon)."""
    from core.paths import resolve_project_path
    src = QPixmap(str(resolve_project_path(LOGO_PATH)))
    if src.isNull():
        src = QPixmap(size, size)
        src.fill(Qt.transparent)
        p = QPainter(src)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor("#feaa34"))
        p.setPen(Qt.NoPen)
        p.drawEllipse(3, 3, size - 6, size - 6)
        p.setPen(QColor("#0b0c0e"))
        p.setFont(QFont("Segoe UI", int(size * 0.4), QFont.Bold))
        p.drawText(src.rect(), Qt.AlignCenter, "S")
        p.end()
    pm = src.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    if not dot_color:
        return pm
    out = QPixmap(size, size)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    p.drawPixmap((size - pm.width()) // 2, (size - pm.height()) // 2, pm)
    r = size // 3
    p.setPen(QColor("#0b0c0e"))
    p.setBrush(QColor(dot_color))
    p.drawEllipse(size - r - 1, size - r - 1, r, r)
    p.end()
    return out


class Sidebar(QFrame):
    navigated = Signal(int)
    search = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("Sidebar")
        self.pill = QFrame(self)
        self.pill.setObjectName("NavPill")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 18, 12, 12)
        lay.setSpacing(3)

        brand = QHBoxLayout()
        brand.setContentsMargins(6, 0, 0, 12)
        brand.setSpacing(9)
        self.logo = QLabel()
        self.word = QLabel("SAINT")
        self.word.setObjectName("Wordmark")
        brand.addWidget(self.logo)
        brand.addWidget(self.word, 1)
        lay.addLayout(brand)

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
        lay.addWidget(self.search_btn)
        lay.addSpacing(12)

        self.group = QButtonGroup(self)
        self.buttons = []
        for i, (name, _icon) in enumerate(PAGES):
            b = QPushButton(name)
            b.setObjectName("NavButton")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setIconSize(QSize(17, 17))
            b.setToolTip(f"{name}  (Ctrl+{i + 1})")
            self.group.addButton(b, i)
            self.buttons.append(b)
            if name == "Settings":
                lay.addStretch()
            lay.addWidget(b)
        self.group.idClicked.connect(self.navigated)

        lay.addSpacing(10)
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
        lay.addLayout(status)
        self._collapsed = False

    def select(self, i: int, animate: bool = True):
        b = self.buttons[i]
        b.setChecked(True)
        target = b.geometry()
        if animate and self.pill.isVisible() and self.pill.geometry().width() > 0:
            motion.animate(self.pill, b"geometry", self.pill.geometry(), target, motion.BASE)
        else:
            self.pill.setGeometry(target)
        self.pill.show()
        self.pill.lower()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        checked = self.group.checkedButton()
        if checked:
            self.pill.setGeometry(checked.geometry())

    def set_collapsed(self, collapsed: bool):
        self._collapsed = collapsed
        self.setFixedWidth(68 if collapsed else 232)
        for b, (name, _i) in zip(self.buttons, PAGES):
            b.setText("" if collapsed else name)
        self.word.setVisible(not collapsed)
        self.search_btn.setText("" if collapsed else "  Search")
        self.search_hint.setVisible(not collapsed)
        self.status.setVisible(not collapsed)
        self.mic.setVisible(not collapsed)

    def refresh(self):
        p = current_palette()
        self.logo.setPixmap(logo_pixmap(24))
        self.search_btn.setIcon(icons.icon("search", p.faint, 15))
        for b, (_n, name) in zip(self.buttons, PAGES):
            b.setIcon(icons.icon(name, p.muted, 17, active_color=p.text))

    def render_state(self, s):
        self.orb.set_state(s.get("state", "offline"))
        self.status.setText(s.get("label", ""))


class MainWindow(QMainWindow):
    def __init__(self, app, runtime=None):
        super().__init__()
        self.app = app
        self.runtime = runtime
        actions.runtime = runtime
        self._quitting = False
        self._last_notice = ("", 0.0)
        self.setWindowTitle("SAINT")
        self.resize(1360, 860)
        self.setMinimumSize(1040, 680)

        from ui.action_notice import ActionNotice
        from ui.demo import Demo
        from ui.halo import Halo
        from ui.overlay import Overlay
        from ui.pages.activity import ActivityPage
        from ui.pages.automations import AutomationsPage
        from ui.pages.history import HistoryPage
        from ui.pages.home import HomePage
        from ui.pages.memory import MemoryPage
        from ui.pages.music import MusicPage
        from ui.pages.system import SystemPage
        from ui.palette import CommandPalette
        from ui.settings_ui import SettingsUI
        from ui.spotify_widget import SpotifyWidget
        from ui.toast import ToastHost
        from ui.win import GlobalHotkey

        central = QWidget()
        self.setCentralWidget(central)
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.sidebar = Sidebar()
        self.sidebar.navigated.connect(lambda i: self.navigate(PAGES[i][0]))
        self.sidebar.search.connect(lambda: self.palette.open())
        self.sidebar.mic.clicked.connect(lambda: actions.toggle_listening(lambda _ok: self._sync_mic()))
        lay.addWidget(self.sidebar)
        content = QWidget()
        content.setObjectName("Content")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        cl.addWidget(self.stack)
        lay.addWidget(content, 1)

        self.home = HomePage(self)
        self.music = MusicPage(self)
        self.settings_ui = SettingsUI(on_appearance_changed=self.apply_appearance)
        self.pages = [self.home, self.music, AutomationsPage(), HistoryPage(), MemoryPage(), ActivityPage(),
                      SystemPage(), self.settings_ui]
        for page in self.pages:
            self.stack.addWidget(page)

        self.palette = CommandPalette(central, self._commands, self._ask)
        self.toasts = ToastHost(central)
        self.overlay = Overlay(self)
        self.widget = SpotifyWidget(self)
        self.halo = Halo(self)
        self.halo.open_requested.connect(self.open_overlay)
        self.halo.preview_ended.connect(self._update_halo)
        self.action_notice = ActionNotice()
        ui_bus.setting_changed.connect(self._live_setting)
        from modules.desktop.media import media
        media.start()                      # what's playing in any app, for the mini player
        self.hotkey = GlobalHotkey(self)
        self.hotkey.triggered.connect(self.toggle_overlay)
        self.hotkey.failed.connect(lambda msg: self.toast("Overlay hotkey", msg, "warn"))
        self.demo = Demo(self)

        QShortcut(QKeySequence("Ctrl+K"), self, activated=self.palette.open)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=lambda: self.navigate("Settings"))
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self._escape)
        for i, (name, _icon) in enumerate(PAGES):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda n=name: self.navigate(n))

        self._build_tray()
        self.apply_appearance()
        self.navigate("Home", animate=False)
        ui_bus.event.connect(self._on_event)
        self._render_state(ui_bus.state)
        event_bus.emit_event(EventType.APP_STARTED, {})

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    def navigate(self, name: str, animate: bool = True):
        idx = next((i for i, (n, _) in enumerate(PAGES) if n == name), 0)
        if self.stack.currentIndex() == idx and self.sidebar.group.checkedId() == idx:
            return
        self.stack.setCurrentIndex(idx)
        self.sidebar.select(idx, animate)
        if animate:
            motion.fade_in(self.stack.currentWidget(), motion.BASE)
        event_bus.emit_event(EventType.UI_UPDATED, {"page": name})

    def _escape(self):
        if self.palette.isVisible():
            self.palette.close_palette()
        elif self.demo.running:
            self.demo.stop()

    def _ask(self, text):
        self.navigate("Home")
        if not actions.submit(text):
            self.toast("Still starting", "SAINT isn't ready for requests yet — try again in a moment.", "warn")

    def _commands(self):
        from ui.palette import Command
        from modules.automation.scenes import scenes
        cmds = [Command(f"Go to {n}", f"Page · Ctrl+{i + 1}", ic, lambda n=n: self.navigate(n))
                for i, (n, ic) in enumerate(PAGES)]
        hk = config.get("overlay.hotkey", "")
        halo = config.get("overlay.halo", "minimized")
        dark = current_palette().dark
        cmds += [
            Command("Open the overlay", hk, "overlay", self.open_overlay),
            Command("Hide the mini player" if config.get("widgets.spotify") else "Show the mini player",
                    "Floating now-playing widget", "widget", lambda: self.set_widget(not config.get("widgets.spotify"))),
            Command("Turn the Halo off" if halo != "off" else "Turn the Halo on", "Screen-edge glow when minimized",
                    "halo", lambda: self.set_halo_mode("off" if halo != "off" else "minimized")),
            Command("Halo: always on", "Show the glow even with the window open", "halo",
                    lambda: self.set_halo_mode("always")),
            Command("Turn action notices off" if config.get("notifications.actions", True)
                    else "Turn action notices on", "A small notice at the bottom of the screen when SAINT acts",
                    "bell", lambda: self.set_action_notices(not config.get("notifications.actions", True))),
            Command("Stop listening" if actions.listening() else "Start listening", "Microphone", "mic",
                    lambda: actions.toggle_listening(lambda _ok: self._sync_mic())),
            Command("Stop speaking", "Interrupt SAINT", "x", actions.interrupt),
            Command("Play / pause", "Spotify", "play", lambda: actions.play_pause(self._spotify_error)),
            Command("Next track", "Spotify · or just say “skip”", "next",
                    lambda: actions.spotify("spotify.next", self._spotify_error)),
            Command("Previous track", "Spotify", "prev",
                    lambda: actions.spotify("spotify.previous", self._spotify_error)),
            Command("Start demo", "SAINT drives itself for 30 s — nothing real runs", "demo", self.start_demo),
            Command(f"Switch to {'light' if dark else 'dark'} theme", "Appearance", "sparkles",
                    lambda: self._set_theme("Light" if dark else "Dark")),
        ]
        cmds += [Command(f"Run scene · {s.name}", " → ".join(s.steps), "zap", lambda s=s: actions.run_scene(s))
                 for s in scenes.all()]
        return cmds

    # ------------------------------------------------------------------ #
    # Shell API (used by pages, overlay, widget, demo)
    # ------------------------------------------------------------------ #
    def toggle_overlay(self):
        import time
        now = time.monotonic()
        if now - getattr(self, "_last_toggle", 0.0) < 0.3:       # one press, one toggle
            return
        self._last_toggle = now
        self.overlay.toggle()

    def open_overlay(self):
        if not self.overlay.isVisible():
            self.overlay.open_overlay()

    def set_widget(self, on: bool):
        on = bool(on)
        if bool(config.get("widgets.spotify", False)) != on:
            config.set("widgets.spotify", on)
        if on and not self.widget.isVisible():
            self.widget.appear()
        elif not on:
            self.widget.hide()
        self.music.apply_theme()
        self._tray_sync()

    def set_halo_mode(self, mode: str, preview: bool = True):
        config.set("overlay.halo", mode)
        if preview and mode != "off":
            self.halo.preview()            # show it now, even with SAINT in front
        self._update_halo()
        self._tray_sync()

    def set_action_notices(self, on: bool):
        config.set("notifications.actions", bool(on))
        self._live_setting("notifications.actions")

    def _live_setting(self, key: str):
        """A switch flipped in Settings or the overlay: apply it and show it."""
        if key.startswith("overlay.halo"):
            self.halo.refresh()
            if config.get("overlay.halo", "minimized") != "off":
                self.halo.preview()
            self._update_halo()
            self._tray_sync()
        elif key == "notifications.actions":
            if config.get("notifications.actions", True):
                self.action_notice.preview()
            else:
                self.action_notice.leave()
        elif key == "widgets.spotify":
            self.set_widget(bool(config.get("widgets.spotify", False)))

    def start_demo(self):
        self.demo.start()

    def toast(self, title, message="", kind="info", icon=None):
        self.toasts.show(title, message, kind, icon)

    def show_normal(self):
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def _set_theme(self, theme):
        config.set("appearance.theme", theme)
        self.settings_ui.load()
        self.apply_appearance()

    def _spotify_error(self, msg):
        self.toast("Spotify", msg, "warn", "music")

    # ------------------------------------------------------------------ #
    # Appearance
    # ------------------------------------------------------------------ #
    def apply_appearance(self):
        from ui.win import style_titlebar
        a = config.get("appearance", {}) or {}
        if QApplication.style().name().lower() != "fusion":
            QApplication.setStyle("Fusion")
        p = current_palette()
        self.app.setPalette(qt_palette(p))
        self.app.setStyleSheet(build_stylesheet(a.get("theme", "Dark"), a.get("accent", "#feaa34"),
                                                a.get("font_family", "Segoe UI"), a.get("font_size", 13),
                                                a.get("compact", False)))
        style_titlebar(self, p.bg, p.text, p.border, p.dark)
        self.setWindowOpacity(max(0.6, min(1.0, float(a.get("opacity", 1.0)))))
        on_top = bool(a.get("always_on_top", False))
        if bool(self.windowFlags() & Qt.WindowStaysOnTopHint) != on_top:
            visible = self.isVisible()
            self.setWindowFlag(Qt.WindowStaysOnTopHint, on_top)
            if visible:
                self.show()
        self.sidebar.set_collapsed(not a.get("sidebar_labels", True))
        self.sidebar.refresh()
        for w in QApplication.allWidgets():
            if isinstance(w, IconButton):
                w.refresh()
        for page in self.pages:
            if hasattr(page, "apply_theme"):
                page.apply_theme()
        self.overlay.apply_theme()
        self.widget.update()
        self.setWindowIcon(QIcon(logo_pixmap(64)))
        self.hotkey.register(config.get("overlay.hotkey", "alt+`"))
        self.set_widget(bool(config.get("widgets.spotify", False)))
        self.halo.refresh()
        self._update_halo()
        self._render_state(ui_bus.state)
        self._sync_mic()

    # ------------------------------------------------------------------ #
    # State, tray, Halo
    # ------------------------------------------------------------------ #
    def _render_state(self, s):
        color = state_color(s.get("state", "offline"), current_palette())
        self.sidebar.render_state(s)
        if getattr(self, "tray", None):
            self.tray.setIcon(QIcon(logo_pixmap(64, color)))
            self.tray.setToolTip(f"SAINT — {s.get('label', '')}")

    def _sync_mic(self):
        on = actions.listening()
        self.sidebar.mic.set_icon("mic" if on else "mic-off")
        self.sidebar.mic.setToolTip("Stop listening" if on else "Start listening")
        if getattr(self, "_listen_action", None):
            self._listen_action.setText("Stop listening" if on else "Start listening")

    def _update_halo(self):
        mode = config.get("overlay.halo", "minimized")
        away = not self.isVisible() or self.isMinimized()
        self.halo.set_visible(self.halo.previewing or mode == "always" or (mode == "minimized" and away))

    def _build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(QIcon(logo_pixmap(64, current_palette().accent)), self)
        menu = QMenu()
        entries = [("Open SAINT", self.show_normal), ("Open overlay", self.open_overlay), None]
        for e in entries:
            if e is None:
                menu.addSeparator()
                continue
            act = QAction(e[0], self)
            act.triggered.connect(e[1])
            menu.addAction(act)
        self._widget_action = QAction("Mini player", self, checkable=True)
        self._widget_action.triggered.connect(self.set_widget)
        self._halo_action = QAction("Halo", self, checkable=True)
        self._halo_action.triggered.connect(lambda on: self.set_halo_mode("minimized" if on else "off"))
        self._listen_action = QAction("Stop listening", self)
        self._listen_action.triggered.connect(lambda: actions.toggle_listening(lambda _ok: self._sync_mic()))
        demo = QAction("Demo mode", self)
        demo.triggered.connect(self.start_demo)
        quit_ = QAction("Quit SAINT", self)
        quit_.triggered.connect(self.quit)
        for act in (self._widget_action, self._halo_action, self._listen_action, demo):
            menu.addAction(act)
        menu.addSeparator()
        menu.addAction(quit_)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_normal()
                                    if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()

    def _tray_sync(self):
        if getattr(self, "tray", None):
            self._widget_action.setChecked(bool(config.get("widgets.spotify", False)))
            self._halo_action.setChecked(config.get("overlay.halo", "minimized") != "off")

    def _notice(self, title, message, kind="info", icon=None):
        """Toast when the window is in front, tray balloon otherwise."""
        import time
        key = f"{title}|{message}"
        if self._last_notice[0] == key and time.time() - self._last_notice[1] < 30:
            return
        self._last_notice = (key, time.time())
        if self.isVisible() and not self.isMinimized() and self.isActiveWindow():
            self.toast(title, message, kind, icon)
        elif self.tray and config.get("notifications.tray", True):
            self.tray.showMessage(title, message, QSystemTrayIcon.Warning if kind == "error"
                                  else QSystemTrayIcon.Information, 8000)

    def _on_event(self, ev):
        t, p = ev.type, ev.payload or {}
        if t == EventType.ASSISTANT_STATE:
            self._render_state(p)
        elif t in (EventType.VOICE_LISTENING_START, EventType.VOICE_LISTENING_STOP):
            self._sync_mic()
        elif t == EventType.NOTIFY:
            self._notice(p.get("title", "SAINT"), p.get("message", ""), icon="bell")
        elif t == EventType.AUTOMATION_TRIGGERED and p.get("kind") == "scene":
            self._notice(f"Scene · {p.get('title')}", p.get("result", "") or "Done.", "ok", "zap")
        elif t == EventType.AUTOMATION_FAILED:
            self._notice("Automation failed", f"{p.get('title')}: {p.get('error')}", "error")
        elif t == EventType.SPOTIFY_ERROR and self.isActiveWindow():
            self._notice("Spotify", p.get("error", ""), "warn", "music")

    # ------------------------------------------------------------------ #
    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.WindowStateChange:
            self._update_halo()

    def showEvent(self, e):
        super().showEvent(e)
        self._update_halo()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._update_halo()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.toasts.relayout()
        if self.palette.isVisible():
            self.palette.setGeometry(self.centralWidget().rect())

    def closeEvent(self, event):
        if not self._quitting and self.tray and config.get("notifications.close_to_tray", True):
            event.ignore()
            self.hide()
            if not getattr(self, "_tray_hint_shown", False):
                self._tray_hint_shown = True
                self.tray.showMessage("SAINT is still running",
                                      "SAINT keeps listening for “Hey SAINT” — watch the Halo around your screen. "
                                      f"{config.get('overlay.hotkey', '')} opens the overlay.",
                                      QSystemTrayIcon.Information, 5000)
            return
        self._quitting = True
        self.demo.stop()
        self.hotkey.unregister()
        self.halo.shutdown()
        for w in (self.overlay, self.widget, self.action_notice):
            w.close()
        from modules.desktop.media import media
        media.stop()
        if self.runtime:
            self.runtime.shutdown()
        if self.tray:
            self.tray.hide()
        event.accept()
        QApplication.instance().quit()

    def quit(self):
        self._quitting = True
        self.close()

"""
ui/main_window.py

The SAINT shell: sidebar + pages, and everything that lives outside the
window — tray icon, the Halo, the overlay, the floating mini player, the
global hotkey, the command palette, toasts and demo mode.
"""

import logging

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import (QAction, QColor, QFont, QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap,
                           QShortcut)
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QMainWindow, QMenu, QStackedWidget, QSystemTrayIcon,
                               QVBoxLayout, QWidget)

from core.config import config
from core.events import event_bus, EventType
from ui import actions, motion
from ui.components.sidebar import PAGES, Sidebar, ordered_pages  # noqa: F401  (PAGES: the page registry)
from ui.reactive import ui_bus
from ui.theme import build_stylesheet, current_palette, qt_palette, state_color
from ui.widgets import IconButton

log = logging.getLogger("saint.ui")

# Old page names that still work (voice commands, demo, older settings).
PAGE_ALIASES = {"Home": "Overview", "Dashboard": "Overview"}

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


def app_icon() -> QIcon:
    """Window / taskbar icon. A hand-made ``saint.ico`` next to the logo wins;
    otherwise every standard size is rendered from the logo (smoothly scaled
    here rather than by Windows from one 64 px image)."""
    from core.paths import resolve_project_path
    ico = resolve_project_path("saint.ico")
    if ico.exists():
        return QIcon(str(ico))
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        icon.addPixmap(logo_pixmap(size))
    return icon


class MainWindow(QMainWindow):
    def __init__(self, app, runtime=None):
        super().__init__()
        self.app = app
        self.runtime = runtime
        actions.runtime = runtime
        self._quitting = False
        self._last_notice = ("", 0.0)
        self._widget_in_game = False          # the user asked for the mini player during a game
        from core.paths import profile_name
        # A test profile is never mistaken for the real thing (core/profiles.py).
        self._profile = profile_name()
        self.setWindowTitle(f"SAINT — {self._profile} profile (test data)" if self._profile else "SAINT")
        self._fit_to_screen()

        from ui.action_notice import ActionNotice
        from ui.demo import Demo
        from ui.halo import Halo
        from ui.overlay import Overlay
        from ui.pages.activity import ActivityPage
        from ui.pages.automations import AutomationsPage
        from ui.pages.history import HistoryPage
        from ui.pages.overview import OverviewPage
        from ui.pages.tasks import TasksPage
        from ui.components.agent_status import AgentStatusBar
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
        self.sidebar = Sidebar(logo_pixmap)
        self.sidebar.navigated.connect(lambda key: self.navigate(key))
        self.sidebar.search.connect(lambda: self.palette.open())
        self.sidebar.mic.clicked.connect(lambda: actions.toggle_listening(lambda _ok: self._sync_mic()))
        lay.addWidget(self.sidebar)
        content = QWidget()
        content.setObjectName("Content")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self.stack = QStackedWidget()
        cl.addWidget(self.stack, 1)
        self.status_bar = AgentStatusBar(self)
        cl.addWidget(self.status_bar)
        lay.addWidget(content, 1)

        self.music = MusicPage(self)
        self.settings_ui = SettingsUI(on_appearance_changed=self.apply_appearance)
        from ui.pages.devices import DevicesPage
        from ui.pages.storage import StoragePage
        self.overview = OverviewPage(self)
        self.page_map = {"Overview": self.overview, "Activity": ActivityPage(), "Tasks": TasksPage(self),
                         "Music": self.music, "Devices": DevicesPage(), "Memory": MemoryPage(),
                         "Automations": AutomationsPage(), "History": HistoryPage(), "Storage": StoragePage(),
                         "System": SystemPage(self), "Settings": self.settings_ui}
        self.pages = list(self.page_map.values())
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
        for i in range(9):                                         # Ctrl+1 ... Ctrl+9: the sidebar's order
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda i=i: self._nth_page(i))

        self._build_tray()
        self.apply_appearance()
        self.navigate("Overview", animate=False)
        ui_bus.event.connect(self._on_event)
        from core.ui_link import ui_link
        ui_link.attached = True            # voice can now drive the window ("open the dashboard")
        self._render_state(ui_bus.state)
        event_bus.emit_event(EventType.APP_STARTED, {})

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    def navigate(self, name: str, animate: bool = True):
        name = PAGE_ALIASES.get(name, name)
        page = self.page_map.get(name) or self.overview
        if self.stack.currentWidget() is page and self.sidebar.current() == name:
            return
        self.stack.setCurrentWidget(page)
        self.sidebar.select(name, animate)
        if animate:
            motion.fade_in(page, motion.BASE)
        event_bus.emit_event(EventType.UI_UPDATED, {"page": name})

    def _nth_page(self, i: int):
        keys = self.sidebar.keys()
        if 0 <= i < len(keys):
            self.navigate(keys[i])

    def open_settings(self, section: str = ""):
        """Settings, open at ``section`` ("Appearance", "Voice", ...)."""
        if not self.isVisible() or self.isMinimized():
            self.show_normal()
        if section in self.page_map:                 # "Devices" is a page, not a Settings category
            self.navigate(section)
            return
        self.navigate("Settings")
        if section:
            self.settings_ui.show_section(section)

    def _fit_to_screen(self):
        """A size that fits the screen it opens on (the old 1040 x 680 minimum didn't fit a 125 %
        laptop), on the monitor chosen in Settings › Appearance › Layout if there is one."""
        screens = QGuiApplication.screens()
        want = int(config.get("layout.start_monitor", 0) or 0)
        screen = screens[want - 1] if 1 <= want <= len(screens) else QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        self.setMinimumSize(760, 520)
        if avail is None:
            self.resize(1360, 860)
            return
        w = min(1360, int(avail.width() * 0.9))
        h = min(880, int(avail.height() * 0.9))
        self.resize(max(760, w), max(520, h))
        if want:
            self.move(avail.x() + (avail.width() - self.width()) // 2, avail.y() + (avail.height() - self.height()) // 2)

    def _update_badges(self):
        try:
            t = actions.current_task()
            busy = t and t.get("status") in ("understanding", "observing", "planning", "executing", "verifying",
                                             "recovering", "waiting_for_user")
            self.sidebar.set_badge("Tasks", "!" if t and t.get("status") == "waiting_for_user" else "1" if busy else "")
        except Exception:
            pass
        try:
            if config.get("link.enabled", False):
                from modules.link.service import get_link
                link = get_link()
                n = len(link.node.connected_ids()) if link.running else 0
                self.sidebar.set_badge("Devices", str(n) if n else "")
            else:
                self.sidebar.set_badge("Devices", "")
        except Exception:
            pass

    def _escape(self):
        if self.palette.isVisible():
            self.palette.close_palette()
        elif self.demo.running:
            self.demo.stop()

    def _ask(self, text):
        self.navigate("Overview")
        if not actions.submit(text):
            self.toast("Still starting", "SAINT isn't ready for requests yet — try again in a moment.", "warn")

    def _commands(self):
        from ui.palette import Command
        from modules.automation.scenes import scenes
        cmds = [Command(f"Go to {label}", f"Page · Ctrl+{i + 1}" if i < 9 else "Page", ic,
                        lambda k=key: self.navigate(k))
                for i, (key, label, ic, _sec) in enumerate(ordered_pages())]
        cmds += [Command("Continue the task", "Agent task", "play", lambda: actions.task_control("resume")),
                 Command("Pause the task", "Agent task", "pause-task", lambda: actions.task_control("pause")),
                 Command("Stop the task", "Agent task", "stop", lambda: actions.task_control("stop")),
                 Command("Customize the layout", "Appearance", "layout", lambda: self.open_settings("Appearance"))]
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

    def set_widget(self, on: bool, explicit: bool = True):
        """``explicit``: the user asked (voice, a button, a menu), so it shows even
        in Game Mode — "turn on the mini player" during Roblox said "on" and
        showed nothing (2026-09-30). Restoring it at startup still respects Game Mode."""
        on = bool(on)
        if bool(config.get("widgets.spotify", False)) != on:
            config.set("widgets.spotify", on)
        from core.game_mode import game_mode
        if on and explicit and not game_mode.mini_player_allowed:
            self._widget_in_game = True
        if on and not self.widget.isVisible() and (game_mode.mini_player_allowed or self._widget_in_game):
            self.widget.appear()
        elif not on:
            self._widget_in_game = False
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
        elif key == "widgets.lyrics":
            self.widget.set_lyrics(bool(config.get("widgets.lyrics", False)))
        elif key.startswith("layout."):
            self.sidebar.rebuild()
            self.overview.arrange(force=True)
            self.status_bar.setVisible(bool(config.get("layout.status_bar", True)))
            self.navigate(self.sidebar.current() or "Overview", animate=False)

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
        self.status_bar.setVisible(bool(config.get("layout.status_bar", True)))
        for w in QApplication.allWidgets():
            if isinstance(w, IconButton):
                w.refresh()
        for page in self.pages:
            if hasattr(page, "apply_theme"):
                page.apply_theme()
        self.overlay.apply_theme()
        self.widget.update()
        icon = app_icon()
        self.setWindowIcon(icon)
        QApplication.instance().setWindowIcon(icon)
        self.hotkey.register(config.get("overlay.hotkey", "alt+`"))
        self.set_widget(bool(config.get("widgets.spotify", False)), explicit=False)
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
            from core.game_mode import game_mode
            self.tray.setIcon(QIcon(logo_pixmap(64, color)))
            playing = f" ({game_mode.game})" if game_mode.game else ""
            game = f" · Gaming Mode{playing}" if game_mode.active else ""
            prof = f" [{self._profile} profile]" if self._profile else ""
            self.tray.setToolTip(f"SAINT{prof} — {s.get('label', '')}{game}")

    def _sync_mic(self):
        on = actions.listening()
        self.sidebar.mic.set_icon("mic" if on else "mic-off")
        self.sidebar.mic.setToolTip("Stop listening" if on else "Start listening")
        self.status_bar.sync_mic()
        if getattr(self, "_listen_action", None):
            self._listen_action.setText("Stop listening" if on else "Start listening")

    def _update_halo(self):
        from core.game_mode import game_mode
        mode = config.get("overlay.halo", "minimized")
        away = not self.isVisible() or self.isMinimized()
        if game_mode.overlays_blocked:
            # A top-most see-through window over a game is what anti-cheat
            # looks for (and it breaks true fullscreen): never show it then.
            self.halo.set_visible(False)
            return
        self.halo.set_visible(self.halo.previewing or mode == "always" or (mode == "minimized" and away))

    def _on_game_mode(self, p: dict):
        """A game started / stopped, or Gaming Mode changed (core/game_mode.py):
        hide / restore the overlays, show the mini player if Gaming Mode keeps
        it, and move SAINT off the game's monitor."""
        from core.game_mode import game_mode
        blocked = bool(p.get("overlays_blocked"))
        self._update_halo()
        if not p.get("running"):
            # Only when the game is gone: "gaming mode off" with the game still running kept
            # hiding the mini player the user had just turned on (2026-10-05).
            self._widget_in_game = False
        if blocked:
            self.action_notice.hide()
        want = bool(config.get("widgets.spotify", False)) or (game_mode.active and game_mode.feature("mini_player"))
        if self.widget.isVisible() and (not want or not (game_mode.mini_player_allowed or self._widget_in_game)):
            self.widget.hide()
        elif want and not self.widget.isVisible() and game_mode.mini_player_allowed:
            self.widget.appear()
        if p.get("changed"):
            if self.widget.isVisible():
                self.widget.place()                  # its Gaming Mode / normal position
            if p.get("active") and game_mode.feature("reposition_ui"):
                self.move_off_game()
        self._render_state(ui_bus.state)
        self._tray_sync()
        if p.get("changed") and config.get("game_mode.announce", True):
            if p.get("active"):
                what = f"{p.get('game')} is running. " if p.get("game") else ""
                self._notice("Gaming Mode on", what + "SAINT follows your Gaming Mode settings; "
                             "say “gaming mode off” any time.", "info", "zap")
            else:
                self._notice("Gaming Mode off", "Everything is back to normal.", "ok", "zap")

    def move_off_game(self) -> str:
        """Put SAINT's windows on the monitor it uses while gaming. Returns where, or ""."""
        from core.game_mode import game_mode
        from ui import placement
        target = placement.gaming_screen(game_mode.game_rect())
        if target is None:
            return ""
        moved = []
        if self.isVisible() and not self.isMinimized() and placement.move_to_screen(self, target):
            moved.append("SAINT")
        if self.overlay.isVisible() and placement.move_to_screen(self.overlay, target):
            moved.append("the overlay")
        corner = "" if config.get("widgets.spotify_pos_game") else "bottom-right"
        if self.widget.isVisible() and placement.move_to_screen(self.widget, target, corner):
            moved.append("the mini player")
        if moved:
            log.info("ui.moved_off_game %s -> screen %d", moved, placement.screen_number(target))
        return f"monitor {placement.screen_number(target)}" if moved else ""

    def place_window(self, what: str, monitor, corner: str = "", dx: int = 0, dy: int = 0) -> str:
        """"Move the mini player to my second monitor" / "... to the top left of my second
        screen" / "right a bit" (voice / menus). Moving the mini player never hides it."""
        from ui import placement
        widget = {"mini_player": self.widget, "overlay": self.overlay}.get(what, self)
        if widget is self.widget and not self.widget.isVisible():
            self.set_widget(True)
        if widget is self and (not self.isVisible() or self.isMinimized()):
            self.show_normal()
        if dx or dy:
            target = widget.screen() or QGuiApplication.primaryScreen()
            placement.nudge(widget, int(dx), int(dy))
        else:
            target = placement.screen_for(monitor) if monitor else (widget.screen() or
                                                                    QGuiApplication.primaryScreen())
            if target is None:
                raise ValueError(f"I can't find monitor {monitor}.")
            if not corner and widget is self.widget:
                corner = "bottom-right"
            # The mini player's frame includes its transparent shadow: inset the card itself ~12 px.
            inset = 0 if widget is self.widget else 12
            placement.move_to_screen(widget, target, corner, inset=inset)
        if widget is self.widget:
            self.widget.remember_position()
        return f"monitor {placement.screen_number(target)}"

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
        self._gaming_action = QAction("Gaming Mode", self, checkable=True)
        self._gaming_action.triggered.connect(self.set_gaming_mode)
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
        for act in (self._gaming_action, self._widget_action, self._halo_action, self._listen_action, demo):
            menu.addAction(act)
        menu.addSeparator()
        menu.addAction(quit_)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_normal()
                                    if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()

    def set_gaming_mode(self, on: bool):
        from core.game_mode import game_mode
        game_mode.set_manual(bool(on))

    def _tray_sync(self):
        if getattr(self, "tray", None):
            from core.game_mode import game_mode
            self._gaming_action.setChecked(game_mode.active)
            self._widget_action.setChecked(bool(config.get("widgets.spotify", False)))
            self._halo_action.setChecked(config.get("overlay.halo", "minimized") != "off")

    def _notice(self, title, message, kind="info", icon=None):
        """Toast when the window is in front, tray balloon otherwise."""
        import time
        from core.game_mode import game_mode
        level = game_mode.feature("notifications")          # Gaming Mode: all / minimal / off
        if level == "off" or (level == "minimal" and kind != "error" and icon != "bell"):
            return
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
        if t == EventType.UI_COMMAND:
            self._handle_ui_command(p)
        elif t == EventType.AGENT_TASK:
            self._update_badges()
            if p.get("status") == "waiting_for_user":
                self._notice("SAINT is waiting for you", p.get("reason", ""), "warn", "bell")
            elif p.get("status") == "failed" and p.get("result"):
                self._notice(f"{p.get('goal', 'A task')} didn't finish", p.get("result", ""), "error")
        elif t == EventType.ASSISTANT_STATE:
            self._render_state(p)
        elif t == EventType.GAME_MODE:
            self._on_game_mode(p)
        elif t == EventType.STARTUP_STATUS and p.get("status") == "failed":
            self._notice(f"{p.get('label', 'Something')} didn't start",
                         f"{p.get('detail', '')}. SAINT keeps running — retry it on the System page.", "error")
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

    def _handle_ui_command(self, p: dict):
        """A voice command about SAINT itself (modules/ui_control/tools.py)."""
        from core.ui_link import ui_link
        cmd, args, cid = p.get("cmd"), p.get("args") or {}, p.get("id", "")
        if self.demo.running:
            self.demo.stop()
        try:
            message = ""
            if cmd == "navigate":
                page = args.get("page", "Overview")
                if not self.isVisible() or self.isMinimized():
                    self.show_normal()
                self.navigate(page)
            elif cmd == "set":
                feature, value = args.get("feature"), str(args.get("value", "")).lower()
                if feature == "mini_player":
                    on = (not bool(config.get("widgets.spotify", False))) if value == "toggle" else value == "on"
                    self.set_widget(on)
                elif feature == "lyrics":
                    # The lyrics live in the mini player: turning them on shows it too.
                    on = (not bool(config.get("widgets.lyrics", False))) if value == "toggle" else value == "on"
                    self.widget.set_lyrics(on, save=True)
                    if on and not self.widget.isVisible():
                        self.set_widget(True)
                elif feature == "halo":
                    cur = config.get("overlay.halo", "minimized")
                    mode = {"on": "always", "toggle": "off" if cur != "off" else "minimized"}.get(value, value)
                    self.set_halo_mode(mode)
                elif feature == "overlay":
                    if value == "on":
                        self.open_overlay()
                    elif value == "off":
                        if self.overlay.isVisible():
                            self.overlay.close_overlay()
                    else:
                        self.toggle_overlay()
                elif feature == "action_notices":
                    on = (not config.get("notifications.actions", True)) if value == "toggle" else value == "on"
                    self.set_action_notices(on)
                elif feature == "theme":
                    if value == "toggle":
                        dark = str(config.get("appearance.theme", "Dark")).lower() != "light"
                        value = "light" if dark else "dark"
                    self._set_theme(value.capitalize())
            elif cmd == "place":
                message = self.place_window(args.get("what", "saint"), args.get("monitor", "other"),
                                            args.get("corner", ""), args.get("dx", 0), args.get("dy", 0))
            elif cmd == "mini_size":
                if not self.widget.isVisible():
                    self.set_widget(True)
                self.widget.voice_size(args.get("size", "normal"))
            elif cmd == "gaming_workspace":
                message = self.move_off_game()
            elif cmd == "window":
                action = args.get("action")
                if action == "show":
                    self.show_normal()
                elif action == "minimize":
                    self.showMinimized()
                elif action == "hide":
                    self.hide()
            else:
                ui_link.ack(cid, False, f"Unknown window command {cmd}.")
                return
            ui_link.ack(cid, True, message)
        except Exception as e:                  # never leave the voice turn waiting
            ui_link.ack(cid, False, f"That didn't work: {e}")

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
        from core.ui_link import ui_link
        ui_link.attached = False
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

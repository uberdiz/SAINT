"""
core/game_mode.py

Game detection and Gaming Mode: SAINT gets out of the way of games.

Two separate things (2026-10-01 — "a user may play a single-player game while
keeping Gaming Mode off"):

* **A game is running** (detected). Only the anti-cheat safety rules apply:
  no see-through top-most overlay (Halo, edge tab, action pill) and no
  synthetic clicks/typing into the game window.
* **Gaming Mode** is on — by hand ("gaming mode on", the switch in Settings or
  the tray) or, with *Auto Gaming Mode* on, whenever a game starts. While it is
  on, each SAINT feature follows its own Gaming Mode setting
  (``game_mode.features``: wake word, voice, mini player, Spotify,
  notifications, vision, screen automation, overlay, AI chat, performance,
  moving SAINT's windows off the game's monitor).

The original notes:

The Halo (and SAINT's other always-on-top, click-through windows) sat on top
of games. A transparent top-most layered window over a game is exactly what
anti-cheat overlay heuristics look for, and it also stops the game from
running in true fullscreen. Confirmed by the user on 2026-09-28: games
refused to start or play properly while the Halo was showing.

While a game runs (or is about to start) SAINT:
    - hides the Halo, the edge tab, the action pill and the mini player
    - stops the periodic screen fingerprint (modules/watch)
    - refuses screen capture and synthetic clicks/typing into the game window
    - won't start watch-and-learn recording
and restores everything when the game exits. The wake word, Spotify, volume,
timers and speech keep working.

Detection is cheap and needs no screen capture: every few seconds the list of
process ids is compared with the last one, and only *new* processes are looked
at once (name + exe path). A process is a game when it lives in a Steam
library's ``steamapps\\common``, in the user's games folder, or matches a known
game / anti-cheat process name (plus game_mode.processes). Separately, when
Windows reports a fullscreen app in front (SHQueryUserNotificationState), the
overlays are hidden too, without the rest of Game Mode.
"""

import logging
import os
import threading
import time
from typing import Dict, Iterable, List, Optional, Tuple

from core.config import config
from core.events import event_bus, EventType

log = logging.getLogger("saint.gamemode")

# Games and anti-cheat processes that don't live in a Steam library. Only
# processes that run *while a game runs* — never always-on services such as
# Vanguard's vgc.exe or the FACEIT client.
KNOWN_GAME_PROCESSES = {
    "easyanticheat.exe", "easyanticheat_eos.exe", "beservice.exe", "beservice_x64.exe",
    "robloxplayerbeta.exe", "valorant-win64-shipping.exe", "fortniteclient-win64-shipping.exe",
    "r5apex.exe", "r5apex_dx12.exe", "cs2.exe", "discovery.exe", "leagueclient.exe", "league of legends.exe",
    "overwatch.exe", "rocketleague.exe", "cod.exe", "modernwarfare.exe", "gta5.exe", "gta5_enhanced.exe",
    "eldenring.exe", "destiny2.exe", "tslgame.exe", "rainbowsix.exe", "rainbowsix_be.exe", "dota2.exe",
    "minecraft.windows.exe", "javaw.exe_minecraft",
}

# Things installed through Steam (or in a games folder) that aren't games and
# may run all day. They must never hold Game Mode on.
DEFAULT_IGNORE = {
    "wallpaper32.exe", "wallpaper64.exe", "webwallpaper32.exe", "ui32.exe",
    "vrmonitor.exe", "vrserver.exe", "vrcompositor.exe", "vrdashboard.exe", "steamvr.exe",
    "unitycrashhandler64.exe", "unitycrashhandler32.exe", "crashreportclient.exe", "crashpad_handler.exe",
    "steamwebhelper.exe", "steam.exe", "steamservice.exe", "gameoverlayui.exe", "gameoverlayui64.exe",
    "soundpad.exe", "lossless scaling.exe", "losslessscaling.exe", "fpsmonitor.exe",
}

# Non-game apps sold on Steam, by their steamapps\common folder (their helper
# processes have all sorts of names — e.g. Wallpaper Engine's winrtutil64.exe).
DEFAULT_IGNORE_FOLDERS = {"wallpaper_engine", "steamvr", "steamworks shared", "soundpad", "lossless scaling",
                          "obs studio", "fpsvr", "ovr advanced settings", "voicemod", "aseprite", "blender"}

# What each SAINT feature does while Gaming Mode is on (game_mode.features overrides).
# notifications: "all" | "minimal" (errors and reminders only) | "off".
FEATURE_DEFAULTS = {
    "wake_word": True, "voice": True, "mini_player": True, "spotify": True, "notifications": "minimal",
    "vision": False, "screen_automation": False, "overlay": False, "ai_chat": True, "performance": True,
    "reposition_ui": True,
}
FEATURE_LABELS = {
    "wake_word": "Wake word", "voice": "Spoken replies", "mini_player": "Mini player", "spotify": "Spotify",
    "notifications": "Notifications", "vision": "Vision (screen capture)", "screen_automation": "Screen automation",
    "overlay": "Halo and overlays", "ai_chat": "AI chat", "performance": "Performance mode",
    "reposition_ui": "Move SAINT off the game's monitor",
}

# SHQueryUserNotificationState results that mean "something fullscreen is in front".
_QUNS_BUSY, _QUNS_D3D_FULLSCREEN, _QUNS_PRESENTATION = 2, 3, 4


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p)).rstrip("\\/") + os.sep if p else ""


def is_game_process(name: str, exe: str, game_dirs: Iterable[str],
                    extra: Iterable[str] = (), ignore: Iterable[str] = ()) -> bool:
    """Pure classification (tested in tests/test_game_mode.py)."""
    n = (name or "").lower()
    if not n:
        return False
    ignored = {x.lower() for x in DEFAULT_IGNORE} | {x.lower() for x in ignore}
    if n in ignored:
        return False
    if n in KNOWN_GAME_PROCESSES or n in {x.lower() for x in extra}:
        return True
    if not exe:
        return False
    path = os.path.normcase(os.path.normpath(exe))
    for d in game_dirs:
        if d and path.startswith(d):
            top = path[len(d):].split(os.sep, 1)[0]
            return top not in DEFAULT_IGNORE_FOLDERS
    return False


def game_directories() -> List[str]:
    """Steam libraries' steamapps\\common plus the user's games folder(s)."""
    dirs = []
    try:
        from modules.steam.library import libraries
        for lib in libraries(extra=config.get("steam.extra_libraries", []) or []):
            dirs.append(os.path.join(lib["path"], "steamapps", "common"))
    except Exception:
        log.debug("game_mode.steam_libraries_failed", exc_info=True)
    games = config.get("files.games_dir", "") or ""
    if games:
        dirs.append(games)
    dirs += list(config.get("game_mode.folders", []) or [])
    return [_norm(d) for d in dirs if d]


def _fullscreen_in_front() -> bool:
    try:
        import ctypes
        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
        return state.value in (_QUNS_BUSY, _QUNS_D3D_FULLSCREEN, _QUNS_PRESENTATION)
    except Exception:
        return False


def _foreground_pid() -> int:
    try:
        import win32gui
        import win32process
        hwnd = win32gui.GetForegroundWindow()
        return win32process.GetWindowThreadProcessId(hwnd)[1] if hwnd else 0
    except Exception:
        return 0


def _windows_of(pids) -> List[Tuple[int, Tuple[int, int, int, int]]]:
    """Visible top-level windows of these processes: [(hwnd, (l, t, r, b))]."""
    out = []
    try:
        import win32gui
        import win32process

        def each(hwnd, _):
            try:
                if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                    return True
                if win32process.GetWindowThreadProcessId(hwnd)[1] in pids:
                    l, t, r, b = win32gui.GetWindowRect(hwnd)
                    if r - l > 200 and b - t > 150:
                        out.append((hwnd, (l, t, r, b)))
            except Exception:
                pass
            return True
        win32gui.EnumWindows(each, None)
    except Exception:
        log.debug("game_mode.windows_failed", exc_info=True)
    return out


class GameMode:
    """Tracks running games (``running``) and Gaming Mode (``active``)."""

    def __init__(self, interval: float = 2.0):
        self._interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._known: Dict[int, Tuple[str, bool]] = {}     # pid -> (name, is_game)
        self._games: Dict[int, str] = {}                  # running game pids -> name
        self._dirs: List[str] = []
        self._dirs_at = 0.0
        self._armed_until = 0.0                           # a launch SAINT started, before its process appears
        self._armed_name = ""
        self._manual: Optional[bool] = None               # "game mode on/off" by voice
        self._fullscreen = False
        self._last = (False, "", False)
        self.active = False                               # Gaming Mode
        self.running = False                              # a game is running (or about to)
        self.game = ""                                    # the running game's name

    # ------------------------------------------------------------------ #
    # Queries used by the rest of SAINT (thread-safe, no side effects)
    # ------------------------------------------------------------------ #
    def feature(self, name: str):
        """What a SAINT feature should do now: its Gaming Mode setting while
        Gaming Mode is on, otherwise fully on (True / "all")."""
        if not self.active:
            return "all" if name == "notifications" else True
        value = self.features().get(name, True)
        if name == "notifications":
            return value if value in ("all", "minimal", "off") else ("all" if value else "off")
        return bool(value)

    def features(self) -> Dict[str, object]:
        feats = dict(FEATURE_DEFAULTS)
        saved = config.get("game_mode.features", {}) or {}
        feats.update({k: v for k, v in saved.items() if k in FEATURE_DEFAULTS})
        return feats

    @property
    def busy(self) -> bool:
        """A game is running or Gaming Mode is on: no background screen work."""
        return self.running or self.active

    @property
    def overlays_blocked(self) -> bool:
        """Hide the Halo / edge tab / action pill: they'd sit on top of a game
        (anti-cheat), a fullscreen app, or Gaming Mode turned them off."""
        if self._fullscreen:
            return True
        if self.running and config.get("game_mode.protect_overlays", True):
            return True
        return self.active and not self.feature("overlay")

    @property
    def mini_player_allowed(self) -> bool:
        """The mini player may show: nothing is in the way, or Gaming Mode keeps it on."""
        if self.active:
            return bool(self.feature("mini_player"))
        return not self.overlays_blocked

    def is_game_pid(self, pid: int) -> bool:
        with self._lock:
            return pid in self._games

    def game_in_front(self) -> bool:
        """The foreground window belongs to a running game (Gaming Mode or not)."""
        return self.running and self.is_game_pid(_foreground_pid())

    def game_rect(self) -> Optional[Tuple[int, int, int, int]]:
        """Screen rectangle (l, t, r, b) of the running game's main window, if it has one."""
        with self._lock:
            pids = set(self._games)
        wins = _windows_of(pids) if pids else []
        if not wins:
            return None
        return max(wins, key=lambda w: (w[1][2] - w[1][0]) * (w[1][3] - w[1][1]))[1]

    def refuse_capture(self) -> Optional[str]:
        """A spoken reason when screen capture must not run, else None."""
        if self.active and not self.feature("vision") and config.get("game_mode.block_capture", True):
            return "Vision is off in Gaming Mode, so I'm not capturing the screen. Say “gaming mode off” to use it."
        return None

    def refuse_input(self) -> Optional[str]:
        """A spoken reason when synthetic clicks/typing must not run, else None."""
        if config.get("game_mode.block_input", True) and self.game_in_front():
            return (f"{self.game or 'Your game'} is in front, and I don't click or type into games — "
                    "anti-cheat can treat that as a bot.")
        if self.active and not self.feature("screen_automation"):
            return "Screen automation is off in Gaming Mode. Say “gaming mode off”, or turn it on in Settings."
        return None

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #
    def start(self):
        if os.name != "nt":
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="game-mode")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def expect_launch(self, name: str, seconds: float = 90.0):
        """SAINT is starting a game: get out of the way *before* its anti-cheat
        starts, instead of up to one scan later."""
        if not config.get("game_mode.detect", True):
            return
        self._armed_until = time.monotonic() + seconds
        self._armed_name = name or ""
        self._update()

    def set_manual(self, on: Optional[bool]):
        """True/False forces Game Mode on/off; None goes back to automatic."""
        self._manual = on
        if on is False:
            self._armed_until = 0.0
        self._update()

    # ------------------------------------------------------------------ #
    def _run(self):
        import psutil
        while not self._stop.is_set():
            try:
                self._scan(psutil)
            except Exception:
                log.debug("game_mode.scan_failed", exc_info=True)
            self._stop.wait(self._interval)

    def _game_dirs(self) -> List[str]:
        if time.monotonic() - self._dirs_at > 300 or not self._dirs_at:
            self._dirs = game_directories()
            self._dirs_at = time.monotonic()
        return self._dirs

    def _classify(self, psutil, pid: int) -> Tuple[str, bool]:
        try:
            proc = psutil.Process(pid)
            name = proc.name()
        except Exception:
            return "", False
        try:
            exe = proc.exe()
        except Exception:
            exe = ""
        game = is_game_process(name, exe, self._game_dirs(), config.get("game_mode.processes", []) or [],
                               config.get("game_mode.ignore", []) or [])
        return name, game

    def _scan(self, psutil):
        if not config.get("game_mode.detect", True):
            # Detection switched off: nothing is suspended (a manual "gaming mode on" still works).
            with self._lock:
                self._games = {}
            self._fullscreen = False
            self._update()
            return
        pids = set(psutil.pids())
        own = os.getpid()
        for pid in list(self._known):
            if pid not in pids:
                self._known.pop(pid, None)
        for pid in pids - set(self._known):
            if pid == own:
                continue
            self._known[pid] = self._classify(psutil, pid)
        games = {pid: name for pid, (name, game) in self._known.items() if game}
        with self._lock:
            self._games = games
        self._fullscreen = _fullscreen_in_front() and _foreground_pid() != own
        self._update()

    def _display_name(self) -> str:
        with self._lock:
            names = list(self._games.values())
        # The anti-cheat helper is not what the user calls the game.
        real = [n for n in names if not n.lower().startswith(("easyanticheat", "beservice"))]
        name = (real or names or [self._armed_name])[0]
        return os.path.splitext(name)[0] if name.lower().endswith(".exe") else name

    def _update(self):
        with self._lock:
            running = bool(self._games)
        if running:
            self._armed_until = 0.0            # the real process took over
        running = running or time.monotonic() < self._armed_until
        auto = running and bool(config.get("game_mode.enabled", True))       # Auto Gaming Mode
        if self._manual is False and not auto:
            self._manual = None                # the game switched off by hand has exited: automatic again
        active = auto if self._manual is None else self._manual
        game = (self._display_name() or "your game") if running else ""
        was_active = self.active
        self.active, self.running, self.game = active, running, game
        blocked = self.overlays_blocked
        if (active, running, game, blocked) == self._last:
            return
        self._last = (active, running, game, blocked)
        if active != was_active:
            log.info("game_mode.%s game=%r manual=%s", "on" if active else "off", game, self._manual)
        event_bus.emit_event(EventType.GAME_MODE, {"active": active, "running": running, "game": game,
                                                   "overlays_blocked": blocked, "changed": active != was_active,
                                                   "manual": self._manual is not None})


game_mode = GameMode()

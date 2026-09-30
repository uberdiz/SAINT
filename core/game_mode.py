"""
core/game_mode.py

Game Mode: SAINT gets out of the way of games.

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


class GameMode:
    """Tracks running games; ``active`` drives what SAINT suspends."""

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
        self.active = False
        self.game = ""

    # ------------------------------------------------------------------ #
    # Queries used by the rest of SAINT (thread-safe, no side effects)
    # ------------------------------------------------------------------ #
    @property
    def overlays_blocked(self) -> bool:
        """Hide the Halo / edge tab / action pill / mini player."""
        return self.active or self._fullscreen

    def is_game_pid(self, pid: int) -> bool:
        with self._lock:
            return pid in self._games

    def game_in_front(self) -> bool:
        """The foreground window belongs to a running game."""
        return self.active and self.is_game_pid(_foreground_pid())

    def refuse_capture(self) -> Optional[str]:
        """A spoken reason when screen capture must not run, else None."""
        if self.active and config.get("game_mode.block_capture", True):
            return f"Game Mode is on while {self.game or 'your game'} runs, so I'm not capturing the screen."
        return None

    def refuse_input(self) -> Optional[str]:
        """A spoken reason when synthetic clicks/typing must not go to the game."""
        if config.get("game_mode.block_input", True) and self.game_in_front():
            return (f"{self.game or 'Your game'} is in front, and I don't click or type into games — "
                    "anti-cheat can treat that as a bot.")
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
        if not config.get("game_mode.enabled", True):
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
        if not config.get("game_mode.enabled", True):
            # Switched off in Settings: nothing is suspended (a manual "game mode on" still works).
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
        auto = running or time.monotonic() < self._armed_until
        if self._manual is False and not auto:
            self._manual = None                # the game switched off by hand has exited: automatic again
        active = auto if self._manual is None else self._manual
        game = (self._display_name() or "your game") if active else ""
        blocked = active or self._fullscreen
        was_active = self.active
        self.active, self.game = active, game
        if (active, game, blocked) == self._last:
            return
        self._last = (active, game, blocked)
        if active != was_active:
            log.info("game_mode.%s game=%r manual=%s", "on" if active else "off", game, self._manual)
        event_bus.emit_event(EventType.GAME_MODE, {"active": active, "game": game, "overlays_blocked": blocked,
                                                   "changed": active != was_active,
                                                   "manual": self._manual is not None})


game_mode = GameMode()

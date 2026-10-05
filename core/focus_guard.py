"""
core/focus_guard.py

Don't pull the user away from what they're doing. While a game is in front,
or they're typing in a code editor, SAINT only brings a window forward when
the request asked for one ("open Discord", "search YouTube for X", "switch
to Spotify"). Everything else — a guessed plan, a model's tool call, a
background step — runs without touching the foreground, or not at all:

    "play Fancy That by PinkPantheress"   Spotify API, nothing on screen moves
    (a plan that would open YouTube)       refused: "you're in Roblox"

The agent sets the request being handled (``begin``); the tool registry asks
``refuse(tool)`` before any tool that changes the foreground window.
"""

import re
import threading
import time
from typing import Optional

from core.config import config

# Tools that put a window in front of the user.
FOCUS_TOOLS = {
    "desktop.open_app", "desktop.focus_window", "desktop.web_search", "desktop.open_site", "desktop.open_url",
    "desktop.new_tab", "youtube.search", "youtube.play", "steam.open_library", "steam.store_search",
    "steam.launch", "files.open_folder", "files.open", "ui.show_dashboard", "desktop.move_window",
    "desktop.arrange_window", "desktop.maximize", "desktop.restore",
}
# Requests that ask for something on screen, in so many words.
_EXPLICIT = re.compile(
    r"\b(?:open|launch|start|run|show|switch|go to|go back to|bring up|pull up|put|move|snap|search|google|"
    r"look up|watch|navigate|visit|focus|maximi[sz]e|restore|full ?screen|take me|bring|display|view|check my|"
    r"read my|reopen|load|play .+ on (?:youtube|twitch|netflix))\b", re.I)
_EDITORS = {"code", "code - insiders", "cursor", "devenv", "pycharm64", "idea64", "rider64", "webstorm64",
            "clion64", "sublime_text", "notepad++", "windowsterminal", "wt", "nvim-qt", "zed", "studio64",
            "robloxstudiobeta", "unity", "unrealeditor", "blender", "obsidian", "winword", "excel"}


class FocusGuard:
    def __init__(self):
        self._lock = threading.Lock()
        self._request = ""
        self._explicit = False
        self._at = 0.0

    def begin(self, text: str, explicit: Optional[bool] = None):
        """The request being handled now (scenes pass explicit=True: the user
        asked for the scene by name)."""
        with self._lock:
            self._request = text or ""
            self._explicit = bool(_EXPLICIT.search(text or "")) if explicit is None else explicit
            self._at = time.time()

    def explicit(self) -> bool:
        with self._lock:
            # A request older than two minutes (a reminder firing, a background
            # task finishing) never counts as asking for a window.
            return self._explicit and time.time() - self._at < 120

    @staticmethod
    def busy() -> Optional[str]:
        """What the user is in the middle of, if it shouldn't be interrupted."""
        try:
            from core.game_mode import game_mode
            if game_mode.game_in_front():
                return f"you're playing {game_mode.game or 'a game'}"
        except Exception:
            pass
        try:
            import psutil
            import win32gui
            import win32process
            hwnd = win32gui.GetForegroundWindow()
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            name = psutil.Process(pid).name().lower().replace(".exe", "")
        except Exception:
            return None
        if name in _EDITORS and _idle_seconds() < float(config.get("desktop.focus_guard_typing_sec", 30)):
            return "you're working in " + ("your editor" if name not in ("code", "cursor") else "VS Code")
        return None

    def refuse(self, tool: str) -> Optional[str]:
        """A reason not to run ``tool`` now, or None."""
        if tool not in FOCUS_TOOLS or not config.get("desktop.focus_guard", True) or self.explicit():
            return None
        why = self.busy()
        if not why:
            return None
        return f"I didn't switch windows because {why}. Say “show it” or ask me to open it when you're ready."


def _idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input anywhere."""
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
        return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0
    except Exception:
        return 1e9


focus_guard = FocusGuard()

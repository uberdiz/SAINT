"""
modules/desktop/controller.py

Structured, validated desktop operations (Windows).

Every operation is an explicit method with bounded inputs; there is no
"run arbitrary command" path. Tools in modules/desktop/tools.py wrap these
methods, and the agent only ever reaches the desktop through those tools.

Safety rails:
  * master switch + per-capability switches in Settings > Desktop Control
  * mouse coordinates are validated against the virtual screen
  * key names are validated; session-ending combos (Alt+F4, Win+L, ...) are refused
  * typed text is length-limited
  * pyautogui's fail-safe stays on: slam the mouse into a screen corner to abort
"""

import ctypes
import difflib
import logging
import os
import re
import time
from ctypes import wintypes
from dataclasses import dataclass, asdict
from typing import List, Optional

from core.config import config
from modules.automation.tools import ToolError

log = logging.getLogger("saint.desktop")


def _enable_dpi_awareness():
    """Use physical pixels everywhere (win32 rects, UI Automation, pyautogui).

    With mixed scaling (e.g. 150 % laptop + 100 % monitor) a DPI-unaware
    process gets virtualised coordinates and clicks land in the wrong place.
    Qt already sets per-monitor-v2 awareness in the app; this covers tools and
    tests that import the controller first. Failing (already set) is fine.
    """
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


if os.name == "nt":
    _enable_dpi_awareness()

BROWSER_PROCESSES = ("chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "arc", "librewolf", "waterfox")

try:
    import win32api
    import win32con
    import win32gui
    import win32process
    HAS_WIN32 = True
except Exception:  # pragma: no cover - non-Windows
    HAS_WIN32 = False

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    process: str
    left: int
    top: int
    width: int
    height: int
    monitor: int          # 1-based monitor index
    minimized: bool
    maximized: bool
    foreground: bool

    def to_dict(self):
        return asdict(self)


@dataclass
class MonitorInfo:
    index: int            # 1-based, sorted left-to-right
    left: int
    top: int
    right: int
    bottom: int
    work: tuple           # (left, top, right, bottom) excluding taskbar
    primary: bool
    scale: float = 1.0    # Windows display scaling (1.5 = 150 %)
    position: str = ""    # "left", "right", "above", "below", "center" relative to the primary

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top


# Combos that end the session / close things without confirmation.
_BLOCKED_COMBOS = {
    frozenset({"alt", "f4"}), frozenset({"win", "l"}), frozenset({"ctrl", "alt", "delete"}),
    frozenset({"ctrl", "alt", "del"}), frozenset({"win", "x"}), frozenset({"win", "r"}),
}
_KEY_ALIASES = {"control": "ctrl", "windows": "win", "super": "win", "cmd": "win", "escape": "esc",
                "return": "enter", "del": "delete", "page up": "pageup", "page down": "pagedown",
                "spacebar": "space", "arrow up": "up", "arrow down": "down",
                "arrow left": "left", "arrow right": "right"}


def _require(flag: str, what: str):
    if not config.get("desktop.enabled", True):
        raise ToolError("Desktop control is turned off in Settings.", "DESKTOP_OFF")
    if not config.get(f"desktop.{flag}", True):
        raise ToolError(f"{what} is turned off in Settings > Desktop Control.", "DESKTOP_OFF")


class DesktopController:
    # ------------------------------------------------------------------ #
    # Monitors
    # ------------------------------------------------------------------ #
    def monitors(self) -> List[MonitorInfo]:
        if not HAS_WIN32:
            raise ToolError("Window control needs pywin32 (Windows only).", "UNSUPPORTED")
        mons = []
        for hmon, _hdc, rect in win32api.EnumDisplayMonitors():
            info = win32api.GetMonitorInfo(hmon)
            scale = 1.0
            try:
                dx, dy = ctypes.c_uint(), ctypes.c_uint()
                ctypes.windll.shcore.GetDpiForMonitor(ctypes.c_void_p(int(hmon)), 0,
                                                      ctypes.byref(dx), ctypes.byref(dy))
                scale = round(dx.value / 96.0, 2)
            except Exception:
                pass
            mons.append((rect, info.get("Work", rect), bool(info.get("Flags", 0) & 1), scale))
        mons.sort(key=lambda m: (m[0][0], m[0][1]))
        out = [MonitorInfo(i + 1, r[0], r[1], r[2], r[3], tuple(w), p, sc)
               for i, (r, w, p, sc) in enumerate(mons)]
        prim = next((m for m in out if m.primary), out[0] if out else None)
        for m in out:
            if prim is None or m is prim:
                m.position = "main"
            elif m.left >= prim.right:
                m.position = "right"
            elif m.right <= prim.left:
                m.position = "left"
            elif m.bottom <= prim.top:
                m.position = "above"
            else:
                m.position = "below"
        return out

    def monitor_label(self, m: MonitorInfo) -> str:
        """'your main screen' / 'your second screen (on the right)'."""
        if m.primary:
            return "your main screen"
        n = 2 if len(self.monitors()) == 2 else m.index
        ordinal = {1: "first", 2: "second", 3: "third", 4: "fourth"}.get(n, f"number {n}")
        where = f", on the {m.position}" if m.position in ("left", "right") else \
            (f", {m.position} the main one" if m.position in ("above", "below") else "")
        return f"your {ordinal} screen{where}"

    def windows_on(self, monitor_index: int, include_minimized: bool = False) -> List[WindowInfo]:
        """Windows on one monitor, front-most first."""
        return [w for w in self.list_windows()
                if w.monitor == monitor_index and (include_minimized or not w.minimized)]

    def _monitor_index_for(self, left, top, width, height) -> int:
        cx, cy = left + width // 2, top + height // 2
        for m in self.monitors():
            if m.left <= cx < m.right and m.top <= cy < m.bottom:
                return m.index
        return 1

    def resolve_monitor(self, target, current: int) -> MonitorInfo:
        mons = self.monitors()
        if not mons:
            raise ToolError("No monitors found.", "NOT_FOUND")
        t = str(target).strip().lower() if target is not None else "next"
        words = {"first": 1, "one": 1, "main": None, "primary": None, "second": 2, "two": 2,
                 "third": 3, "three": 3, "left": "left", "right": "right", "other": "next",
                 "next": "next", "previous": "prev", "secondary": "other_than_primary"}
        t = words.get(re.sub(r"^(my|the)\s+", "", t), t)
        if isinstance(t, str) and t.isdigit():
            t = int(t)
        if t == "other_than_primary" or (t == 2 and len(mons) == 2):
            # With two screens "my second screen" means the non-main one,
            # whichever side it is on.
            return next((m for m in mons if not m.primary), mons[-1])
        if t == 1 and len(mons) == 2:
            return next((m for m in mons if m.primary), mons[0])
        if t is None:
            return next((m for m in mons if m.primary), mons[0])
        if t == "next":
            return mons[current % len(mons)]
        if t == "prev":
            return mons[(current - 2) % len(mons)]
        if t == "left":
            return mons[0]
        if t == "right":
            return mons[-1]
        try:
            idx = int(t)
        except (TypeError, ValueError):
            raise ToolError(f"I don't know which monitor '{target}' is.", "INVALID")
        if not 1 <= idx <= len(mons):
            raise ToolError(f"There is no monitor {idx}; you have {len(mons)}.", "NOT_FOUND")
        return mons[idx - 1]

    # ------------------------------------------------------------------ #
    # Windows
    # ------------------------------------------------------------------ #
    def _proc_name(self, hwnd) -> str:
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if psutil:
                return psutil.Process(pid).name()
        except Exception:
            pass
        return ""

    def _info(self, hwnd) -> WindowInfo:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        placement = win32gui.GetWindowPlacement(hwnd)
        return WindowInfo(
            hwnd=hwnd, title=win32gui.GetWindowText(hwnd), process=self._proc_name(hwnd),
            left=l, top=t, width=r - l, height=b - t,
            monitor=self._monitor_index_for(l, t, r - l, b - t),
            minimized=bool(win32gui.IsIconic(hwnd)),
            maximized=placement[1] == win32con.SW_SHOWMAXIMIZED,
            foreground=hwnd == win32gui.GetForegroundWindow(),
        )

    def list_windows(self) -> List[WindowInfo]:
        if not HAS_WIN32:
            raise ToolError("Window control needs pywin32 (Windows only).", "UNSUPPORTED")
        own_pid = os.getpid()
        out: List[WindowInfo] = []

        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):
                return True
            if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
                return True
            ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if ex & win32con.WS_EX_TOOLWINDOW:
                return True
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = 0
            title = win32gui.GetWindowText(hwnd)
            if title in ("Program Manager", "Windows Input Experience") or pid == own_pid and title != "SAINT":
                return True
            if _is_cloaked(hwnd):          # suspended UWP apps report "visible" but aren't
                return True
            try:
                out.append(self._info(hwnd))
            except Exception:
                pass
            return True

        win32gui.EnumWindows(cb, None)
        return out

    def find_window(self, query: str) -> WindowInfo:
        """Find a window by title or process name. 'this'/'active' = foreground."""
        q = (query or "").strip().lower()
        wins = self.list_windows()
        if q in ("it", "that", "that window", "that one", "there"):
            from modules.agent.context import desktop_context
            ref = desktop_context.window()
            w = next((x for x in wins if x.hwnd == ref), None)
            if w:
                return w
        if q in ("", "this", "this window", "active", "current", "the active window", "it", "that",
                 "the window", "window", "that window", "that one", "the window i'm looking at",
                 "the window i am looking at", "the one i'm looking at", "what i'm looking at"):
            w = self.target_window(wins)
            if w:
                return w
            raise ToolError("There's no active window.", "NOT_FOUND")
        m = re.match(r"^hwnd:(\d+)$", q)
        if m:
            w = next((x for x in wins if x.hwnd == int(m.group(1))), None)
            if w is None:
                raise ToolError("That window has closed.", "NOT_FOUND")
            return w
        q = re.sub(r"^(the|my)\s+", "", q)
        q = re.sub(r"\s+(window|app|application)$", "", q)
        # An app reference ("the browser", "spotify", "chrome"): its windows,
        # disambiguated by context, or ask which one.
        if q in ("browser", "web browser") or not any(q in w.title.lower() for w in wins):
            apps = self.app_windows(q)
            if apps:
                chosen = self.pick_window(apps)
                if chosen is None:
                    raise AmbiguousWindow(q, apps)
                return chosen
        scored = []
        for w in wins:
            title, proc = w.title.lower(), w.process.lower().replace(".exe", "")
            score = 0.0
            if q == proc:
                score = 1.0
            elif q in title.split(" - ")[-1] or q == title:
                score = 0.95
            elif q in proc:
                score = 0.9
            elif q in title:
                score = 0.8
            else:
                score = max(difflib.SequenceMatcher(None, q, proc).ratio(),
                            difflib.SequenceMatcher(None, q, title.split(" - ")[-1]).ratio()) * 0.7
            if score >= 0.6:
                scored.append((score, not w.minimized, w))
        if not scored:
            raise ToolError(f"I couldn't find a window for {query}.", "NOT_FOUND")
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return scored[0][2]

    @staticmethod
    def _is_own(w: WindowInfo) -> bool:
        try:
            return win32process.GetWindowThreadProcessId(w.hwnd)[1] == os.getpid()
        except Exception:
            return False

    def target_window(self, wins: Optional[List[WindowInfo]] = None,
                      activate: bool = False) -> Optional[WindowInfo]:
        """The window a command like "click the first video" should act on.

        1. The window SAINT is working with (it just searched / opened / moved
           it) — as long as the user hasn't switched to another window since.
           Without this, a multi-step request would drift to whatever window
           happens to be in front.
        2. Otherwise the foreground window.
        3. When SAINT's own window has focus (the command was typed into its
           chat), the window the user was using just before: the topmost
           visible non-SAINT window (EnumWindows returns z-order).
        With ``activate`` the window is brought to the front and verified, so
        keyboard/mouse input lands there and nowhere else.
        """
        wins = self.list_windows() if wins is None else wins
        fg = next((w for w in wins if w.foreground), None)
        try:
            from modules.agent.context import desktop_context
            ref = desktop_context.window()
            fg_then = desktop_context.foreground_at_note()
        except Exception:
            ref = fg_then = None
        if ref:
            user_switched = fg is not None and not self._is_own(fg) and fg.hwnd not in (ref, fg_then)
            w = next((x for x in wins if x.hwnd == ref and not x.minimized), None)
            if w is not None and not user_switched:
                return self._activate(w) if activate and not w.foreground else w
        if fg and not self._is_own(fg):
            return fg
        other = next((w for w in wins if not w.minimized and not self._is_own(w)), None)
        if other is None or not activate:
            return other or fg
        return self._activate(other)

    def focus(self, query: str) -> WindowInfo:
        _require("allow_window_control", "Window control")
        return self._activate(self.find_window(query))

    def _activate(self, w: WindowInfo) -> WindowInfo:
        query = w.title
        hwnd = w.hwnd
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            # Windows only lets the foreground process change focus; a
            # synthetic Alt press satisfies the foreground-lock rule.
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            try:
                win32gui.SetForegroundWindow(hwnd)
            finally:
                win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.15)
        info = self._info(hwnd)
        if not info.foreground:
            raise ToolError(f"Windows didn't let me bring {w.title or query} to the front.", "FOCUS_BLOCKED")
        self._note(info)
        return info

    @staticmethod
    def _note(info):
        try:
            from modules.agent.context import desktop_context
            desktop_context.note_window(info.hwnd, info.title)
        except Exception:
            pass

    def move_to_monitor(self, query: str, monitor) -> WindowInfo:
        _require("allow_window_control", "Window control")
        w = self.find_window(query)
        target = self.resolve_monitor(monitor, w.monitor)
        was_max = w.maximized
        hwnd = w.hwnd
        if w.minimized or was_max:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.05)
            w = self._info(hwnd)
        src = next((m for m in self.monitors() if m.index == w.monitor), target)
        wl, wt, wr, wb = target.work
        sl, st, sr, sb = src.work
        rel_x = (w.left - sl) / max(1, sr - sl)
        rel_y = (w.top - st) / max(1, sb - st)
        width = min(w.width, wr - wl)
        height = min(w.height, wb - wt)
        x = int(wl + max(0.0, min(rel_x, 1.0)) * (wr - wl - width))
        y = int(wt + max(0.0, min(rel_y, 1.0)) * (wb - wt - height))
        win32gui.SetWindowPos(hwnd, 0, x, y, width, height,
                              win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        if was_max:
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        time.sleep(0.1)
        info = self._info(hwnd)
        if info.monitor != target.index:
            raise ToolError(f"I tried, but {w.title} is still on monitor {info.monitor}.", "MOVE_FAILED")
        self._note(info)
        return info

    def arrange(self, query: str, action: str) -> WindowInfo:
        """maximize | minimize | restore | snap_left | snap_right | center"""
        _require("allow_window_control", "Window control")
        w = self.find_window(query)
        hwnd = w.hwnd
        if action == "maximize":
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        elif action == "minimize":
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        elif action == "restore":
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        elif action in ("snap_left", "snap_right", "center"):
            if w.maximized or w.minimized:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            mon = next(m for m in self.monitors() if m.index == w.monitor)
            l, t, r, b = mon.work
            if action == "center":
                width, height = min(w.width, r - l), min(w.height, b - t)
                x, y = l + (r - l - width) // 2, t + (b - t - height) // 2
            else:
                width, height = (r - l) // 2, b - t
                x, y = (l if action == "snap_left" else l + width), t
            win32gui.SetWindowPos(hwnd, 0, x, y, width, height, win32con.SWP_NOZORDER)
        else:
            raise ToolError(f"Unknown window action '{action}'.", "INVALID")
        time.sleep(0.1)
        info = self._info(hwnd)
        if action == "maximize" and not info.maximized or action == "minimize" and not info.minimized:
            raise ToolError(f"{w.title} didn't {action}.", "ARRANGE_FAILED")
        if action != "minimize":
            self._note(info)
        return info

    def scale_window(self, query: str, factor: float) -> WindowInfo:
        """Make a window bigger/smaller around its centre (stays on its monitor)."""
        _require("allow_window_control", "Window control")
        w = self.find_window(query)
        if w.maximized or w.minimized:
            win32gui.ShowWindow(w.hwnd, win32con.SW_RESTORE)
            time.sleep(0.05)
            w = self._info(w.hwnd)
        mon = next(m for m in self.monitors() if m.index == w.monitor)
        l, t, r, b = mon.work
        nw = int(max(320, min(r - l, w.width * factor)))
        nh = int(max(240, min(b - t, w.height * factor)))
        if nw >= (r - l) * 0.97 and nh >= (b - t) * 0.97 and factor > 1:
            win32gui.ShowWindow(w.hwnd, win32con.SW_MAXIMIZE)
            time.sleep(0.1)
            return self._info(w.hwnd)
        cx, cy = w.left + w.width // 2, w.top + w.height // 2
        x = min(max(l, cx - nw // 2), r - nw)
        y = min(max(t, cy - nh // 2), b - nh)
        win32gui.SetWindowPos(w.hwnd, 0, x, y, nw, nh, win32con.SWP_NOZORDER)
        time.sleep(0.1)
        info = self._info(w.hwnd)
        if (factor > 1) != (info.width * info.height > w.width * w.height) and info.width * info.height != w.width * w.height:
            raise ToolError(f"{w.title} didn't change size.", "RESIZE_FAILED")
        return info

    def place_beside(self, query: str, other: str, side: str = "right") -> dict:
        """Put window ``query`` next to window ``other`` (sharing its monitor)."""
        _require("allow_window_control", "Window control")
        a = self.find_window(query)
        b = self.find_window(other)
        if a.hwnd == b.hwnd:
            raise ToolError("That's the same window.", "INVALID")
        mon = next(m for m in self.monitors() if m.index == b.monitor)
        l, t, r, bt = mon.work
        half = (r - l) // 2
        for w in (a, b):
            if w.maximized or w.minimized:
                win32gui.ShowWindow(w.hwnd, win32con.SW_RESTORE)
        a_x, b_x = (l + half, l) if side == "right" else (l, l + half)
        win32gui.SetWindowPos(b.hwnd, 0, b_x, t, half, bt - t, win32con.SWP_NOZORDER)
        win32gui.SetWindowPos(a.hwnd, 0, a_x, t, half, bt - t, win32con.SWP_NOZORDER)
        time.sleep(0.1)
        ai, bi = self._info(a.hwnd), self._info(b.hwnd)
        if ai.monitor != bi.monitor:
            raise ToolError("The windows didn't end up side by side.", "MOVE_FAILED")
        return {"window": ai.title, "beside": bi.title, "side": side, "monitor": ai.monitor}

    def resize(self, query: str, width: int, height: int) -> WindowInfo:
        _require("allow_window_control", "Window control")
        w = self.find_window(query)
        mon = next(m for m in self.monitors() if m.index == w.monitor)
        l, t, r, b = mon.work
        width = max(200, min(int(width), r - l))
        height = max(150, min(int(height), b - t))
        if w.maximized:
            win32gui.ShowWindow(w.hwnd, win32con.SW_RESTORE)
        win32gui.SetWindowPos(w.hwnd, 0, w.left, w.top, width, height,
                              win32con.SWP_NOZORDER | win32con.SWP_NOMOVE)
        return self._info(w.hwnd)

    def close(self, query: str) -> dict:
        _require("allow_window_control", "Window control")
        return self._close(self.find_window(query))

    def close_hwnd(self, hwnd: int) -> dict:
        _require("allow_window_control", "Window control")
        return self._close(self._info(hwnd))

    def _close(self, w: WindowInfo) -> dict:
        # WM_CLOSE is the polite close: apps with unsaved work show their own
        # save prompt instead of losing it. Some apps only honour the title
        # bar's close command (SC_CLOSE), so that is tried next.
        for msg, wparam in ((win32con.WM_CLOSE, 0), (win32con.WM_SYSCOMMAND, win32con.SC_CLOSE)):
            win32gui.PostMessage(w.hwnd, msg, wparam, 0)
            for _ in range(15):
                time.sleep(0.1)
                if not win32gui.IsWindow(w.hwnd) or not win32gui.IsWindowVisible(w.hwnd):
                    return {"closed": True, "title": w.title, "process": w.process}
        return {"closed": False, "title": w.title, "process": w.process,
                "note": "The window asked to stay open (it may be showing a save prompt)."}

    # ------------------------------------------------------------------ #
    # Apps
    # ------------------------------------------------------------------ #
    @staticmethod
    def is_browser(w: WindowInfo) -> bool:
        return any(w.process.lower().startswith(b) for b in BROWSER_PROCESSES)

    def app_windows(self, name: str) -> List[WindowInfo]:
        """Existing top-level windows that belong to the app called ``name``."""
        q = re.sub(r"^(the|my)\s+", "", (name or "").strip().lower())
        q = re.sub(r"\s+(app|application|window|windows)$", "", q)
        wins = [w for w in self.list_windows() if not self._is_own(w)]
        if q in ("browser", "web browser", "internet", "browsers"):
            return [w for w in wins if self.is_browser(w)]
        from modules.desktop.apps import app_catalog, process_names_for
        entry = app_catalog.resolve(q)
        procs = process_names_for(q, entry)
        out = []
        for w in wins:
            stem = w.process.lower().replace(".exe", "")
            if stem in procs or any(p and stem.startswith(p) for p in procs if len(p) >= 4):
                out.append(w)
        return out

    def pick_window(self, candidates: List[WindowInfo], hint: str = "") -> Optional[WindowInfo]:
        """Choose one window from several when the context makes it obvious."""
        if len(candidates) == 1:
            return candidates[0]
        from modules.agent.context import desktop_context
        ref = desktop_context.window()
        visible = [w for w in candidates if not w.minimized]
        words = [x for x in re.findall(r"[a-z0-9]+", (hint or "").lower()) if len(x) > 2]
        if words:
            hits = [w for w in candidates if any(x in w.title.lower() for x in words)]
            if len(hits) == 1:
                return hits[0]
        if ref and any(w.hwnd == ref for w in candidates):
            return next(w for w in candidates if w.hwnd == ref)
        fg = next((w for w in candidates if w.foreground), None)
        if fg:
            return fg
        if len(visible) == 1:
            return visible[0]
        if config.get("desktop.multi_window_policy", "ask") == "recent" and visible:
            return visible[0]                  # z-order: most recently used
        return None

    def open_app(self, name: str, wait: bool = True, new_window: bool = False, hint: str = "") -> dict:
        """Open an app — or, if it is already running, bring its window forward.

        A new instance is launched only when the app isn't running or
        ``new_window`` is set. With several windows and nothing in the
        request to choose between them, raises AMBIGUOUS_WINDOW with the
        candidates so the agent can ask which one.
        """
        _require("allow_app_launch", "Launching apps")
        if not new_window and HAS_WIN32:
            existing = self.app_windows(name)
            if existing:
                chosen = self.pick_window(existing, hint)
                if chosen is None:
                    raise AmbiguousWindow(name, existing)
                info = self._activate(chosen)
                return {"app": name, "launched": False, "reused": True, "window": info.title,
                        "hwnd": info.hwnd, "count": len(existing)}
        if (name or "").strip().lower() in ("browser", "my browser", "web browser", "the browser", "internet"):
            running = self.app_windows("browser")
            if new_window and running and psutil:
                # Re-launching a running browser just focuses it; --new-window
                # (Chromium/Firefox) really opens a separate window.
                try:
                    _, pid = win32process.GetWindowThreadProcessId(running[0].hwnd)
                    exe = psutil.Process(pid).exe()
                    before = {x.hwnd for x in running}
                    import subprocess
                    subprocess.Popen([exe, "--new-window"])
                    deadline = time.time() + 8
                    while time.time() < deadline:
                        time.sleep(0.3)
                        new = [x for x in self.app_windows("browser") if x.hwnd not in before]
                        if new:
                            self._note(new[0])
                            return {"app": running[0].process.replace(".exe", "").title(), "launched": True,
                                    "window": new[0].title, "hwnd": new[0].hwnd}
                except Exception as e:
                    log.warning("desktop.new_browser_window_failed %s", e)
            name = default_browser_name() or name
        from modules.desktop.apps import app_catalog, launch
        entry = app_catalog.resolve(name)
        if entry is None:
            hints = app_catalog.suggestions(name)
            extra = f" Did you mean {', '.join(hints)}?" if hints else ""
            raise ToolError(f"I couldn't find an app called {name} on this PC.{extra}", "NOT_FOUND")
        before = {w.hwnd for w in self.list_windows()} if (wait and HAS_WIN32) else set()
        try:
            launch(entry)
        except OSError as e:
            raise ToolError(f"Windows couldn't start {entry.name}: {e.strerror or e}", "LAUNCH_FAILED")
        result = {"app": entry.name, "source": entry.source, "launched": True, "window": None}
        if wait and HAS_WIN32:
            hint = (entry.process_hint or entry.name.split(" ")[0]).lower()
            deadline = time.time() + 8
            from modules.desktop.apps import process_names_for
            procs = process_names_for(name, entry)
            while time.time() < deadline:
                time.sleep(0.25)
                for w in self.list_windows():
                    stem = w.process.lower().replace(".exe", "")
                    if (w.hwnd not in before or w.foreground) and (
                            hint in w.process.lower() or hint in w.title.lower() or stem in procs
                            or entry.name.lower() in w.title.lower()):
                        result["window"] = w.title
                        result["hwnd"] = w.hwnd
                        self._note(w)          # "search YouTube ..." next should use this new window
                        return result
        return result

    # ------------------------------------------------------------------ #
    # Keyboard
    # ------------------------------------------------------------------ #
    def type_text(self, text: str, target: Optional[str] = None, press_enter: bool = False) -> dict:
        _require("allow_keyboard", "Keyboard control")
        limit = int(config.get("desktop.max_type_length", 500))
        if len(text) > limit:
            raise ToolError(f"That's {len(text)} characters; the limit is {limit} (Settings > Desktop Control).",
                            "TOO_LONG")
        win = self._input_target()
        focused = None
        if target:
            from modules.desktop import uia
            focused = uia.focus_input(target)
            time.sleep(0.05)
        _send_unicode(text)
        if press_enter:
            self._tap("enter")
        return {"typed": len(text), "target": focused or (win.title if win else "active window"),
                "enter": press_enter}

    def _input_target(self) -> Optional[WindowInfo]:
        """Make sure keyboard input goes to the intended window, not SAINT's."""
        # _activate verifies the window really came to the front (FOCUS_BLOCKED otherwise).
        return self.target_window(activate=True) if HAS_WIN32 else None

    def press_keys(self, keys: str) -> dict:
        _require("allow_keyboard", "Keyboard control")
        import pyautogui
        parts = [p.strip().lower() for p in re.split(r"[+\s]+(?=\S)", keys.replace(" + ", "+")) if p.strip()]
        parts = [_KEY_ALIASES.get(p, p) for p in parts]
        if not parts or len(parts) > 4:
            raise ToolError("Give me one key or a combination like ctrl+shift+t.", "INVALID")
        for p in parts:
            if p not in pyautogui.KEYBOARD_KEYS:
                raise ToolError(f"'{p}' isn't a key I know.", "INVALID")
        if frozenset(parts) in _BLOCKED_COMBOS:
            raise ToolError(f"I won't press {'+'.join(parts)} — use a close/lock command instead.", "BLOCKED")
        self._input_target()
        if len(parts) == 1:
            pyautogui.press(parts[0])
        else:
            pyautogui.hotkey(*parts)
        return {"keys": "+".join(parts)}

    def _tap(self, key: str):
        import pyautogui
        pyautogui.press(key)

    # ------------------------------------------------------------------ #
    # Mouse
    # ------------------------------------------------------------------ #
    @staticmethod
    def virtual_screen():
        user32 = ctypes.windll.user32
        x, y = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        w, h = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        return x, y, x + w, y + h

    def _check_point(self, x: int, y: int):
        l, t, r, b = self.virtual_screen()
        if not (l <= x < r and t <= y < b):
            raise ToolError(f"({x}, {y}) is outside your screens.", "OUT_OF_BOUNDS")

    def mouse_move(self, x: int, y: int, duration: float = 0.2) -> dict:
        _require("allow_mouse", "Mouse control")
        import pyautogui
        self._check_point(x, y)
        pyautogui.moveTo(x, y, duration=max(0.0, min(duration, 2.0)))
        return {"x": x, "y": y}

    def mouse_click(self, x: Optional[int] = None, y: Optional[int] = None, button: str = "left",
                    clicks: int = 1) -> dict:
        _require("allow_mouse", "Mouse control")
        import pyautogui
        if (x is None) != (y is None):
            raise ToolError("Give both x and y, or neither to click where the mouse is.", "INVALID")
        if button not in ("left", "right", "middle"):
            raise ToolError("Button must be left, right or middle.", "INVALID")
        clicks = max(1, min(3, int(clicks)))
        if x is not None:
            self._check_point(x, y)
            pyautogui.click(x, y, button=button, clicks=clicks, interval=0.06)
        else:
            pyautogui.click(button=button, clicks=clicks, interval=0.06)
        pos = pyautogui.position()
        return {"x": pos.x, "y": pos.y, "button": button, "clicks": clicks}

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left", duration: float = 0.4) -> dict:
        """Press at (x1, y1), move to (x2, y2), release — drag and drop / text selection."""
        _require("allow_mouse", "Mouse control")
        import pyautogui
        self._check_point(x1, y1)
        self._check_point(x2, y2)
        pyautogui.moveTo(x1, y1, duration=0.1)
        pyautogui.dragTo(x2, y2, duration=max(0.1, min(duration, 2.0)), button=button)
        return {"from": [x1, y1], "to": [x2, y2]}

    def scroll(self, amount: int, x: Optional[int] = None, y: Optional[int] = None) -> dict:
        """Scroll the target window. The wheel goes to the window under the
        cursor, so the cursor is first moved over the intended window."""
        _require("allow_mouse", "Mouse control")
        import pyautogui
        if x is None or y is None:
            w = self.target_window(activate=True) if HAS_WIN32 else None
            if w is not None:
                x, y = w.left + w.width // 2, w.top + w.height // 2
        if x is not None and y is not None:
            self._check_point(x, y)
            pyautogui.moveTo(x, y, duration=0.05)
        pyautogui.scroll(int(amount))
        return {"scrolled": int(amount)}


class AmbiguousWindow(ToolError):
    """Several windows could be meant; ``candidates`` lets the agent ask."""

    def __init__(self, what: str, candidates: List["WindowInfo"]):
        super().__init__(f"I found {len(candidates)} {what} windows. Which one should I use?", "AMBIGUOUS_WINDOW")
        self.what = what
        self.candidates = candidates
        # The tool registry only passes the message on; the agent reads the
        # candidates from here to ask a proper question.
        global last_ambiguity
        last_ambiguity = (what, list(candidates), time.time())


last_ambiguity = None


_PROGID_BROWSER = {"chrome": "chrome", "msedge": "edge", "opera": "opera", "firefox": "firefox",
                   "brave": "brave", "vivaldi": "vivaldi"}


def default_browser_name() -> str:
    """'chrome' / 'opera' / ... from the https handler registered in Windows."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as k:
            progid = str(winreg.QueryValueEx(k, "ProgId")[0]).lower()
    except OSError:
        return ""
    for key, name in _PROGID_BROWSER.items():
        if key in progid or (key == "msedge" and "msedgehtm" in progid) or (key == "chrome" and "chromehtml" in progid):
            return name
    return ""


def _is_cloaked(hwnd) -> bool:
    try:
        val = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(val),
                                                   ctypes.sizeof(val))
        return val.value != 0
    except Exception:
        return False


# ---------------------------------------------------------------------- #
# Unicode typing via SendInput (works for any character, unlike pyautogui)
# ---------------------------------------------------------------------- #
if os.name == "nt":
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send_unicode(text: str):
    if os.name != "nt":
        import pyautogui
        pyautogui.write(text, interval=0.01)
        return
    KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x0004, 0x0002
    VK_RETURN, VK_TAB = 0x0D, 0x09
    inputs = []
    for ch in text.replace("\r\n", "\n"):
        if ch in "\n\t":
            vk = VK_RETURN if ch == "\n" else VK_TAB
            for flags in (0, KEYEVENTF_KEYUP):
                inputs.append(_INPUT(1, _INPUTUNION(ki=_KEYBDINPUT(vk, 0, flags, 0, 0))))
            continue
        code = ord(ch)
        units = [code] if code <= 0xFFFF else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
        for u in units:
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                inputs.append(_INPUT(1, _INPUTUNION(ki=_KEYBDINPUT(0, u, flags, 0, 0))))
    # Send in small batches so long text doesn't overflow the input queue.
    for i in range(0, len(inputs), 64):
        batch = inputs[i:i + 64]
        arr = (_INPUT * len(batch))(*batch)
        sent = ctypes.windll.user32.SendInput(len(batch), arr, ctypes.sizeof(_INPUT))
        if sent != len(batch):
            raise ToolError("Windows blocked the keyboard input (is an admin window focused?).", "INPUT_BLOCKED")
        time.sleep(0.005)


desktop = DesktopController()

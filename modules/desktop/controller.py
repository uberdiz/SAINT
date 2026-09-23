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
            mons.append((rect, info.get("Work", rect), bool(info.get("Flags", 0) & 1)))
        mons.sort(key=lambda m: (m[0][0], m[0][1]))
        return [MonitorInfo(i + 1, r[0], r[1], r[2], r[3], tuple(w), p)
                for i, (r, w, p) in enumerate(mons)]

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
                 "next": "next", "previous": "prev"}
        t = words.get(t, t)
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
        if q in ("", "this", "this window", "active", "current", "the active window", "it", "that"):
            fg = win32gui.GetForegroundWindow()
            for w in wins:
                if w.hwnd == fg:
                    return w
            if wins:
                return wins[0]
            raise ToolError("There's no active window.", "NOT_FOUND")
        q = re.sub(r"^(the|my)\s+", "", q)
        q = re.sub(r"\s+(window|app|application)$", "", q)
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

    def focus(self, query: str) -> WindowInfo:
        _require("allow_window_control", "Window control")
        w = self.find_window(query)
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
        return info

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
        return self._info(hwnd)

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
        w = self.find_window(query)
        win32gui.PostMessage(w.hwnd, win32con.WM_CLOSE, 0, 0)
        for _ in range(20):
            time.sleep(0.1)
            if not win32gui.IsWindow(w.hwnd) or not win32gui.IsWindowVisible(w.hwnd):
                return {"closed": True, "title": w.title, "process": w.process}
        return {"closed": False, "title": w.title, "process": w.process,
                "note": "The window asked to stay open (it may be showing a save prompt)."}

    # ------------------------------------------------------------------ #
    # Apps
    # ------------------------------------------------------------------ #
    def open_app(self, name: str, wait: bool = True) -> dict:
        _require("allow_app_launch", "Launching apps")
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
            while time.time() < deadline:
                time.sleep(0.25)
                for w in self.list_windows():
                    if (w.hwnd not in before or w.foreground) and (
                            hint in w.process.lower() or hint in w.title.lower()
                            or entry.name.lower() in w.title.lower()):
                        result["window"] = w.title
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
        focused = None
        if target:
            from modules.desktop import uia
            focused = uia.focus_input(target)
        _send_unicode(text)
        if press_enter:
            self._tap("enter")
        return {"typed": len(text), "target": focused or "active window", "enter": press_enter}

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
        if x is not None:
            self._check_point(x, y)
            pyautogui.click(x, y, button=button, clicks=clicks)
        else:
            pyautogui.click(button=button, clicks=clicks)
        pos = pyautogui.position()
        return {"x": pos.x, "y": pos.y, "button": button, "clicks": clicks}

    def scroll(self, amount: int) -> dict:
        _require("allow_mouse", "Mouse control")
        import pyautogui
        pyautogui.scroll(int(amount))
        return {"scrolled": int(amount)}


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

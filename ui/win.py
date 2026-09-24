"""
ui/win.py

Windows-native touches: a title bar that matches the theme (Windows 11 DWM
colours — keeps native snap, resize and shadows) and a system-wide hotkey.
Both are no-ops on other platforms or when the call fails.
"""

import ctypes
import logging
import sys
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor

log = logging.getLogger("saint.ui")

_DWMWA_DARK, _DWMWA_BORDER, _DWMWA_CAPTION, _DWMWA_TEXT = 20, 34, 35, 36


def style_titlebar(widget, bg: str, fg: str, border: str, dark: bool):
    if sys.platform != "win32":
        return
    try:
        from ctypes import wintypes
        hwnd = wintypes.HWND(int(widget.winId()))
        dwm = ctypes.windll.dwmapi

        def put(attr, value):
            v = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        def ref(c):
            q = QColor(c)
            return (q.blue() << 16) | (q.green() << 8) | q.red()
        put(_DWMWA_DARK, 1 if dark else 0)
        put(_DWMWA_CAPTION, ref(bg))
        put(_DWMWA_TEXT, ref(fg))
        put(_DWMWA_BORDER, ref(border))
    except Exception as e:  # older Windows builds ignore unknown attributes
        log.debug("titlebar.style_failed %s", e)


_MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
_KEYS = {"space": 0x20, "tab": 0x09, "enter": 0x0D, "`": 0xC0, "backquote": 0xC0, "insert": 0x2D,
         "home": 0x24, "end": 0x23, "pause": 0x13, "\\": 0xDC, ";": 0xBA, "/": 0xBF}


def parse_hotkey(text: str):
    """"ctrl+alt+space" → (modifiers, virtual key) or None."""
    mods, vk = 0, None
    for part in (p.strip().lower() for p in (text or "").split("+")):
        if not part:
            return None
        if part in _MODS:
            mods |= _MODS[part]
        elif part in _KEYS:
            vk = _KEYS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part[0] == "f" and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x6F + int(part[1:])
        else:
            return None
    return (mods, vk) if vk and mods else None


class GlobalHotkey(QObject):
    """RegisterHotKey on a tiny message-loop thread; ``triggered`` arrives on the GUI thread."""
    triggered = Signal()
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._tid = 0
        self._text = ""

    def register(self, text: str) -> bool:
        if text == self._text and self._thread and self._thread.is_alive():
            return True
        self.unregister()
        self._text = text
        if sys.platform != "win32" or not text:
            return False
        parsed = parse_hotkey(text)
        if not parsed:
            self.failed.emit(f"I can't read the hotkey “{text}”. Try something like alt+` or ctrl+alt+s.")
            return False
        mods, vk = parsed
        ready, result = threading.Event(), {}

        def loop():
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
            result["ok"] = bool(user32.RegisterHotKey(None, 1, mods | 0x4000, vk))   # MOD_NOREPEAT
            ready.set()
            if not result["ok"]:
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:                                            # WM_HOTKEY
                    self.triggered.emit()
            user32.UnregisterHotKey(None, 1)

        self._thread = threading.Thread(target=loop, daemon=True, name="hotkey")
        self._thread.start()
        ready.wait(2.0)
        if not result.get("ok"):
            self.failed.emit(f"{text} is taken by another app — choose a different overlay hotkey in Settings.")
            return False
        log.info("hotkey.registered %s", text)
        return True

    def unregister(self):
        if self._thread and self._thread.is_alive() and self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)            # WM_QUIT
            self._thread.join(1.0)
        self._thread, self._tid, self._text = None, 0, ""


if __name__ == "__main__":
    assert parse_hotkey("ctrl+alt+space") == (0x3, 0x20)
    assert parse_hotkey("alt+`") == (0x1, 0xC0)
    assert parse_hotkey("Win+Shift+F9") == (0xC, 0x78)
    assert parse_hotkey("space") is None and parse_hotkey("ctrl+nope") is None and parse_hotkey("") is None
    print("hotkey parse ok")

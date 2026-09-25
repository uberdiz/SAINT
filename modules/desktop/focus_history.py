"""
modules/desktop/focus_history.py

When was each window last in front?

"Which browser window?" was SAINT's most common question in the logs: three
Opera windows open, none in front because SAINT's own window or the overlay
had focus. A light background poll of the foreground window lets the
"smart" window policy pick the one you were actually using (within
desktop.recent_focus_sec), and only ask when none of them was used lately.
"""

import logging
import threading
import time
from typing import Dict, Iterable, Optional

log = logging.getLogger("saint.desktop")


class FocusHistory:
    def __init__(self, interval: float = 0.5):
        self._interval = interval
        self._seen: Dict[int, float] = {}       # hwnd -> last time it was the foreground window
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        try:
            import win32gui  # noqa: F401
        except ImportError:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="focus-history")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        import win32gui
        import win32process
        import os
        own = os.getpid()
        while not self._stop.wait(self._interval):
            try:
                hwnd = win32gui.GetForegroundWindow()
                if not hwnd:
                    continue
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid == own:               # SAINT's own window / overlay never counts
                    continue
                self.note(hwnd)
            except Exception:
                continue

    def note(self, hwnd: int, when: Optional[float] = None):
        with self._lock:
            self._seen[int(hwnd)] = when if when is not None else time.time()
            if len(self._seen) > 400:
                for h, _ in sorted(self._seen.items(), key=lambda kv: kv[1])[:200]:
                    self._seen.pop(h, None)

    def last_focused(self, hwnd: int) -> float:
        with self._lock:
            return self._seen.get(int(hwnd), 0.0)

    def most_recent(self, windows: Iterable, within_sec: float = 600.0):
        """The window among ``windows`` used most recently (within
        ``within_sec``), or None when none of them was used lately."""
        now = time.time()
        best, best_t = None, 0.0
        for w in windows:
            t = self.last_focused(getattr(w, "hwnd", 0))
            if t and now - t <= within_sec and t > best_t:
                best, best_t = w, t
        return best


focus_history = FocusHistory()


def smart_pick(windows, visible=None):
    """The "smart" multi-window policy: the visible window used most recently,
    else any recently used one. None means SAINT should ask."""
    from core.config import config
    within = float(config.get("desktop.recent_focus_sec", 600))
    return (focus_history.most_recent(visible or [], within)
            or focus_history.most_recent(windows, within))

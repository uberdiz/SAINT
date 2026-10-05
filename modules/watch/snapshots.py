"""
modules/watch/snapshots.py

"SAINT, what changed?" — every watch.snapshot_sec SAINT notes which windows
are open (and their titles) plus a tiny fingerprint of each screen, keeping
watch.keep_min minutes. Asking compares now with then:

    "While you were away: Discord opened, the build window now says
     'Build succeeded', and your right screen changed."

"Since I left" uses the moment you stopped touching the mouse and keyboard.
Only window titles and 64-bit screen fingerprints are kept, in memory only.
"""

import ctypes
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from core.config import config


@dataclass
class Snapshot:
    at: float
    windows: Dict[int, tuple]                  # hwnd -> (process, title, monitor)
    screens: Dict[int, int] = field(default_factory=dict)   # monitor -> dhash
    idle_sec: float = 0.0


def idle_seconds() -> float:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
        return max(0.0, (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0)
    except Exception:
        return 0.0


def dhash(img) -> int:
    small = img.convert("L").resize((9, 8))
    px = list(small.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def hamming(a: int, b: int) -> int:
    return bin((a or 0) ^ (b or 0)).count("1")


def take() -> Snapshot:
    from modules.desktop.controller import desktop
    wins = {w.hwnd: (w.process, w.title, w.monitor) for w in desktop.list_windows() if w.title != "SAINT"}
    screens = {}
    from core.game_mode import game_mode
    # No screen capture while a game runs: it stutters the game and some
    # anti-cheats flag programs that grab the screen.
    if config.get("watch.screen_hash", True) and not game_mode.busy:
        try:
            from PIL import ImageGrab
            for m in desktop.monitors():
                screens[m.index] = dhash(ImageGrab.grab(bbox=(m.left, m.top, m.right, m.bottom), all_screens=True))
        except Exception:
            pass
    return Snapshot(time.time(), wins, screens, idle_seconds())


class SnapshotLog:
    def __init__(self, taker: Callable[[], Snapshot] = take):
        self._take = taker
        self._items: deque = deque()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="snapshots")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while True:
            try:
                self.add(self._take())
            except Exception:
                pass
            if self._stop.wait(max(3.0, float(config.get("watch.snapshot_sec", 10) or 10))):
                return

    def add(self, snap: Snapshot):
        keep = float(config.get("watch.keep_min", 30) or 30) * 60
        with self._lock:
            self._items.append(snap)
            while self._items and snap.at - self._items[0].at > keep:
                self._items.popleft()

    def items(self) -> List[Snapshot]:
        with self._lock:
            return list(self._items)

    def baseline(self, minutes: Optional[float] = None, since_left: bool = False) -> Optional[Snapshot]:
        items = self.items()
        if len(items) < 2:
            return None
        if since_left:
            # The snapshot just before the latest stretch of being away (idle > 60 s).
            for i in range(len(items) - 1, 0, -1):
                if items[i].idle_sec >= 60 and items[i - 1].idle_sec < items[i].idle_sec:
                    start = items[i].at - items[i].idle_sec
                    before = [s for s in items if s.at <= start]
                    return before[-1] if before else items[0]
            return None
        target = items[-1].at - (minutes or 5) * 60
        before = [s for s in items if s.at <= target]
        return before[-1] if before else items[0]


def describe_changes(old: Snapshot, new: Snapshot, label: Callable[[tuple], str] = None) -> str:
    def name(entry):
        if label:
            return label(entry)
        from modules.vision.screen import app_label
        return app_label({"process": entry[0], "title": entry[1]})

    opened = [name(new.windows[h]) for h in new.windows if h not in old.windows]
    closed = [name(old.windows[h]) for h in old.windows if h not in new.windows]
    retitled = [(name(new.windows[h]), new.windows[h][1]) for h in new.windows
                if h in old.windows and new.windows[h][1] != old.windows[h][1]]
    screens = [m for m in new.screens if m in old.screens and hamming(new.screens[m], old.screens[m]) > 10]
    parts = []
    if opened:
        parts.append(_join(sorted(set(opened))) + " opened")
    if closed:
        parts.append(_join(sorted(set(closed))) + " closed")
    for app, title in retitled[:3]:
        parts.append(f"{app} now says “{title[:50]}”")
    if screens and not (opened or closed or retitled):
        parts.append(("screen " + _join([str(m) for m in screens]) + " changed") if len(screens) < 3
                     else "your screens changed")
    mins = max(1, round((new.at - old.at) / 60))
    if not parts:
        return f"Nothing's changed in the last {mins} minute{'s' if mins != 1 else ''}."
    return f"In the last {mins} minute{'s' if mins != 1 else ''}: " + "; ".join(parts) + "."


def _join(items: List[str]) -> str:
    items = [i for i in items if i]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


snapshot_log = SnapshotLog()

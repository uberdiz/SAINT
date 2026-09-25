"""
modules/watch/watchers.py

Things SAINT keeps an eye on for you, then tells you about:

    title     "tell me when this window changes"        the window's title changes
    closed    "tell me when Steam closes"               the window / program goes away
    download  "tell me when this download finishes"    a new finished file lands in Downloads
    change    "tell me when this changes"               the window's picture changes noticeably
    finished  "tell me when Claude finishes"           it changes, then stays still for a few seconds

Each check is cheap (a title read, a folder listing or a 64x36 greyscale
thumbnail of the window) and runs once a second on one background thread.
Watches expire after watch.expire_min. When one fires SAINT says so and
remembers the window, so "take me back to it" brings it up.
"""

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from core.config import config
from core.events import event_bus, EventType

log = logging.getLogger("saint.watch")

_PARTIAL = (".crdownload", ".part", ".tmp", ".download", ".opdownload", ".partial")


@dataclass
class Watch:
    kind: str                      # title | closed | download | change | finished
    label: str                     # "Claude", "your download"
    hwnd: int = 0
    pid: int = 0
    folder: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: float = field(default_factory=time.time)
    state: Dict = field(default_factory=dict)


def thumb(hwnd: int):
    """A tiny greyscale picture of a window (None when it can't be seen)."""
    try:
        import win32gui
        from PIL import ImageGrab
        if not win32gui.IsWindow(hwnd) or win32gui.IsIconic(hwnd):
            return None
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        if r - l < 20 or b - t < 20:
            return None
        img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True).convert("L").resize((64, 36))
        return list(img.getdata())
    except Exception:
        return None


def diff(a, b) -> float:
    """Mean absolute difference of two thumbnails, 0-255."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _title(hwnd: int) -> Optional[str]:
    try:
        import win32gui
        return win32gui.GetWindowText(hwnd) if win32gui.IsWindow(hwnd) else None
    except Exception:
        return None


def _alive(w: Watch) -> bool:
    if w.hwnd:
        try:
            import win32gui
            return bool(win32gui.IsWindow(w.hwnd))
        except Exception:
            return False
    if w.pid:
        try:
            import psutil
            return psutil.pid_exists(w.pid)
        except Exception:
            return False
    return True


def _finished_files(folder: str) -> Dict[str, int]:
    out = {}
    try:
        for e in os.scandir(folder):
            if e.is_file() and not e.name.lower().endswith(_PARTIAL) and not e.name.startswith("~$"):
                out[e.name] = e.stat().st_size
    except OSError:
        pass
    return out


class WatchManager:
    CHANGE_THRESHOLD = 6.0          # mean greyscale difference that counts as "changed"
    SETTLE_SEC = 8.0                # "finished": still for this long after changing

    def __init__(self, clock: Callable[[], float] = time.time, grab: Callable = thumb, title: Callable = _title,
                 files: Callable = _finished_files, alive: Callable = _alive):
        self._lock = threading.Lock()
        self._watches: Dict[str, Watch] = {}
        self._clock, self._grab, self._title, self._files, self._alive = clock, grab, title, files, alive
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.on_fire: Optional[Callable[[Watch, str], None]] = None
        self.last_fired: Optional[Watch] = None
        event_bus.subscribe(self._on_event)

    # ------------------------------------------------------------------ #
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="watchers")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _on_event(self, ev):
        if ev.type == EventType.STOP_ALL:
            self.clear()

    def add(self, w: Watch) -> Watch:
        limit = int(config.get("watch.max_watchers", 5) or 5)
        with self._lock:
            if len(self._watches) >= limit:
                oldest = min(self._watches.values(), key=lambda x: x.created)
                self._watches.pop(oldest.id, None)
            self._prime(w)
            self._watches[w.id] = w
        from core.activity import activity
        activity.add_background(f"watch-{w.id}", f"watching {w.label}")
        self.start()
        return w

    def _prime(self, w: Watch):
        if w.kind in ("title", "finished"):
            w.state["title"] = self._title(w.hwnd)
        if w.kind in ("change", "finished"):
            w.state["base"] = self._grab(w.hwnd)
            w.state["last"] = w.state["base"]
            w.state["last_change"] = None
        if w.kind == "download":
            w.state["before"] = set(self._files(w.folder))
            w.state["sizes"] = {}

    def remove(self, wid: str):
        with self._lock:
            w = self._watches.pop(wid, None)
        if w:
            from core.activity import activity
            activity.remove_background(f"watch-{w.id}")

    def clear(self):
        for wid in list(self._watches):
            self.remove(wid)

    def active(self) -> List[Watch]:
        with self._lock:
            return list(self._watches.values())

    # ------------------------------------------------------------------ #
    def _run(self):
        while not self._stop.wait(1.0):
            try:
                self.tick()
            except Exception:
                log.exception("watch.tick_failed")

    def tick(self):
        now = self._clock()
        expire = float(config.get("watch.expire_min", 120) or 120) * 60
        for w in self.active():
            if now - w.created > expire:
                self.remove(w.id)
                continue
            msg = self._check(w, now)
            if msg:
                self.remove(w.id)
                self.last_fired = w
                self._fire(w, msg)

    def _check(self, w: Watch, now: float) -> Optional[str]:
        if w.kind == "closed":
            return None if self._alive(w) else f"{w.label} closed."
        if w.kind == "download":
            files = self._files(w.folder)
            new = {n: s for n, s in files.items() if n not in w.state["before"]}
            for name, size in new.items():
                prev = w.state["sizes"].get(name)
                w.state["sizes"][name] = (size, prev[1] if prev and prev[0] == size else now)
                if prev and prev[0] == size and now - prev[1] >= 3 and size > 0:
                    w.state["file"] = name
                    return f"Your download finished: {name}."
            return None
        if not self._alive(w):
            return f"{w.label} closed."
        if w.kind == "title":
            t = self._title(w.hwnd)
            return f"{w.label} changed — it now says “{t[:60]}”." if t and t != w.state["title"] else None
        pic = self._grab(w.hwnd)
        title = self._title(w.hwnd) if w.kind == "finished" else None
        if w.kind == "change":
            return f"{w.label} changed." if pic and diff(pic, w.state["base"]) >= self.CHANGE_THRESHOLD else None
        # finished: changed at some point, then still for SETTLE_SEC
        moving = (pic is not None and diff(pic, w.state["last"]) >= 2.0) or (title and title != w.state["title"])
        if moving:
            w.state["last_change"] = now
            w.state["title"] = title or w.state["title"]
        if pic is not None:
            w.state["last"] = pic
        changed_at = w.state.get("last_change")
        if changed_at and now - changed_at >= self.SETTLE_SEC:
            return f"{w.label} looks finished."
        return None

    def _fire(self, w: Watch, msg: str):
        log.info("watch.fired kind=%s label=%r", w.kind, w.label)
        event_bus.emit_event(EventType.WATCH_FIRED, {"id": w.id, "kind": w.kind, "label": w.label, "message": msg,
                                                     "hwnd": w.hwnd})
        event_bus.emit_event(EventType.NOTIFY, {"title": "SAINT", "message": msg})
        if self.on_fire:
            self.on_fire(w, msg)
        else:
            try:
                from core.runtime import runtime
                if runtime.controller:
                    extra = " Say “take me back” to switch to it." if w.hwnd else ""
                    runtime.controller.announce(msg + extra, source="watch")
            except Exception:
                pass


watch_manager = WatchManager()

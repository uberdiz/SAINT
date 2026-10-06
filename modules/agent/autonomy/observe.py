"""
modules/agent/autonomy/observe.py

What's true on the PC right now, asked the cheapest reliable way first:

    structured APIs   Spotify's player state, the file system, sockets, HTTP
    the OS            windows (title, process, monitor), processes, installed apps, monitors,
                      the clipboard
    UI Automation     text and controls an app exposes (and a browser's address bar)
    OCR               Windows' own OCR on a screenshot (modules/vision/ocr.py) — apps that
                      expose nothing
    vision model      only when asked for something none of the above can answer

Every call returns an ``Observation`` and never raises; results are cached for a moment so a
plan step and its verification don't ask Windows the same thing twice. Nothing here acts.
"""

import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("saint.agent.observe")

CACHE_SEC = 0.8


@dataclass
class Observation:
    kind: str                    # window | process | app | file | port | http | spotify | monitors | ...
    ok: bool                     # the thing asked about is true / was found
    value: Any = None
    source: str = ""             # api | os | uia | ocr | vision | unavailable
    detail: str = ""             # a short human description for the trail
    at: float = field(default_factory=time.time)

    def __bool__(self):
        return self.ok


class Observer:
    def __init__(self):
        self._cache: Dict[tuple, Observation] = {}
        self._lock = threading.Lock()

    # ---- plumbing -------------------------------------------------------------------------------
    def _cached(self, key: tuple, fn: Callable[[], Observation], fresh: bool = False) -> Observation:
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None and not fresh and now - hit.at < CACHE_SEC:
            return hit
        try:
            obs = fn()
        except Exception as e:                       # an observer never breaks a task
            log.debug("observe.failed %s %s", key, e)
            obs = Observation(key[0], False, None, "unavailable", f"couldn't check ({e})")
        with self._lock:
            self._cache[key] = obs
            if len(self._cache) > 200:
                self._cache.clear()
        return obs

    def forget(self):
        with self._lock:
            self._cache.clear()

    @staticmethod
    def _desktop():
        from modules.desktop.controller import desktop
        return desktop

    # ---- windows and apps -----------------------------------------------------------------------
    def windows(self, fresh: bool = False) -> Observation:
        def run():
            wins = [w for w in self._desktop().list_windows() if not self._desktop()._is_own(w)]
            return Observation("windows", True, wins, "os", f"{len(wins)} windows open")
        return self._cached(("windows",), run, fresh)

    def foreground(self, fresh: bool = False) -> Observation:
        def run():
            w = self._desktop().target_window()
            if not w:
                return Observation("foreground", False, None, "os", "nothing in front")
            return Observation("foreground", True, w, "os", f"{_app(w)} — {w.title[:60]}")
        return self._cached(("foreground",), run, fresh)

    def window(self, name: str, fresh: bool = False) -> Observation:
        """Is a window of the app called ``name`` open? value: the windows (front-most first)."""
        def run():
            wins = self._desktop().app_windows(name)
            if not wins:
                # A title match too: "the SAINT project" open in VS Code, a page title in a browser.
                words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 2]
                if words:
                    wins = [w for w in self.windows(fresh).value or []
                            if all(x in w.title.lower() for x in words)]
            return Observation("window", bool(wins), wins, "os",
                               f"{_app(wins[0])} window open" if wins else f"no {name} window")
        return self._cached(("window", name.lower()), run, fresh)

    def process(self, name: str, fresh: bool = False) -> Observation:
        def run():
            import psutil
            from modules.desktop.apps import app_catalog, process_names_for
            want = process_names_for(name, app_catalog.resolve(name)) or {name.lower()}
            found = []
            for p in psutil.process_iter(["name"]):
                stem = (p.info.get("name") or "").lower().replace(".exe", "")
                if stem in want:
                    found.append(p.pid)
            return Observation("process", bool(found), found, "os",
                               f"{name} is running" if found else f"{name} isn't running")
        return self._cached(("process", name.lower()), run, fresh)

    def app_installed(self, name: str) -> Observation:
        def run():
            from modules.desktop.apps import app_catalog
            entry = app_catalog.resolve(name)
            return Observation("app", entry is not None, entry, "os",
                               f"{entry.name} is installed" if entry else f"no app called {name}")
        return self._cached(("app", name.lower()), run)

    def monitors(self) -> Observation:
        def run():
            mons = self._desktop().monitors()
            text = ", ".join(f"{m.index}{' (main)' if m.primary else ''} {m.width}x{m.height}" for m in mons)
            return Observation("monitors", bool(mons), mons, "os", f"{len(mons)} monitor(s): {text}")
        return self._cached(("monitors",), run)

    def window_monitor(self, name: str) -> Observation:
        """Which monitor the app's front window is on (1-based)."""
        w = self.window(name, fresh=True)
        if not w.ok:
            return Observation("window_monitor", False, None, w.source, w.detail)
        win = w.value[0]
        return Observation("window_monitor", True, win.monitor, "os", f"{_app(win)} is on monitor {win.monitor}")

    # ---- files, processes, network ------------------------------------------------------------------
    def file(self, path: str) -> Observation:
        p = os.path.expandvars(os.path.expanduser(path or ""))
        ok = bool(p) and os.path.exists(p)
        return Observation("file", ok, p, "os", f"{p} exists" if ok else f"{p} doesn't exist")

    def port(self, port: int, host: str = "127.0.0.1", fresh: bool = True) -> Observation:
        def run():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                ok = s.connect_ex((host, int(port))) == 0
            return Observation("port", ok, int(port), "os",
                               f"something is listening on {host}:{port}" if ok else f"nothing on port {port}")
        return self._cached(("port", host, int(port)), run, fresh)

    def http(self, url: str) -> Observation:
        def run():
            import requests
            r = requests.get(url, timeout=2.5)
            ok = r.status_code < 500
            return Observation("http", ok, r.status_code, "api", f"{url} answered {r.status_code}")
        return self._cached(("http", url), run, fresh=True)

    def clipboard(self) -> Observation:
        def run():
            from modules.desktop.clipboard import read_plain_text
            text = read_plain_text() or ""
            return Observation("clipboard", bool(text), text, "os", f"{len(text)} characters copied")
        return self._cached(("clipboard",), run)

    # ---- apps with an API ------------------------------------------------------------------------------
    def spotify(self, fresh: bool = False) -> Observation:
        def run():
            from core.module_manager import module_manager
            sp = module_manager.get("spotify")
            if sp is None or not sp.enabled:
                return Observation("spotify", False, {}, "unavailable", "Spotify isn't enabled")
            ok, why = sp.availability()
            if not ok:
                return Observation("spotify", False, {}, "unavailable", why)
            st = sp.tools._state(force=fresh)
            st = {k: v for k, v in st.items() if k != "item"}
            playing = "playing" if st.get("is_playing") else "paused"
            return Observation("spotify", True, st, "api",
                               f"Spotify {playing}: {st.get('track') or 'nothing'} (volume {st.get('volume')})")
        return self._cached(("spotify",), run, fresh)

    def browser(self, hint: str = "") -> Observation:
        """The browser window a request means and, when it can be read, its address."""
        def run():
            from modules.desktop import browser
            try:
                w = browser.choose(hint, remember_hint=False)
            except Exception as e:
                return Observation("browser", False, None, "os", str(e) or "no browser open")
            url = _address_bar(w.hwnd)
            return Observation("browser", True, {"window": w, "title": w.title, "url": url},
                               "uia" if url else "os", f"{_app(w)}: {url or w.title[:60]}")
        return self._cached(("browser", hint.lower()), run, fresh=True)

    # ---- what's on screen: UIA -> OCR -> vision ------------------------------------------------------------
    def screen_text(self, use_ocr: bool = True) -> Observation:
        """Readable text of the window in front: UI Automation first, OCR if it exposes nothing."""
        try:
            from modules.desktop import uia
            data = uia.read_text(60)
            lines = data.get("text") or []
            if len(" ".join(lines)) >= 20:
                return Observation("screen_text", True, "\n".join(lines), "uia",
                                   f"{len(lines)} lines from {data.get('window', 'the window')}")
        except Exception as e:
            log.debug("observe.uia_text_failed %s", e)
        if not use_ocr:
            return Observation("screen_text", False, "", "uia", "the window exposes no text")
        img = self._grab_foreground()
        if img is None:
            return Observation("screen_text", False, "", "unavailable", "couldn't capture the window")
        from modules.vision import ocr
        res = ocr.recognize(img[0])
        return Observation("screen_text", bool(res["text"]), res["text"], "ocr",
                           f"{len(res['lines'])} lines read with OCR")

    def locate(self, target: str, allow_vision: bool = False) -> Observation:
        """Where ``target`` (a button, a link, a word) is on screen. value: {"x", "y", "rect", "tier"}.
        The ladder stops at the first rung that answers; the tier is in the trail."""
        try:
            from modules.desktop import uia
            r = uia.locate(target)
            return Observation("locate", True, dict(r, tier="uia"), "uia", f"found “{r.get('name') or target}”")
        except Exception as e:
            log.debug("observe.uia_locate_failed %s", e)
        img = self._grab_foreground()
        if img is not None:
            from modules.vision import ocr
            rect = ocr.find_text(target, img[0], img[1])
            if rect:
                x, y, w, h = rect
                return Observation("locate", True, {"x": x + w // 2, "y": y + h // 2, "rect": rect, "tier": "ocr"},
                                   "ocr", f"read “{target}” on screen")
        if allow_vision:
            try:
                from modules.vision.screen import VisualAnalyzer
                va = VisualAnalyzer()
                ok, _why = va.available()
                if ok and img is not None:
                    answer = va.analyze(img[2], f"Is there a '{target}' on this screen? Answer yes or no, "
                                                f"then where (top/middle/bottom, left/center/right).")
                    found = answer.strip().lower().startswith("yes")
                    return Observation("locate", found, {"tier": "vision", "answer": answer}, "vision", answer[:120])
            except Exception as e:
                log.debug("observe.vision_failed %s", e)
        return Observation("locate", False, None, "ocr" if img is not None else "uia", f"no “{target}” on screen")

    def _grab_foreground(self):
        """(png bytes, (left, top), PIL image) of the front window — or None (Game Mode, no window)."""
        try:
            from core.game_mode import game_mode
            if game_mode.refuse_capture():
                return None
            fg = self.foreground(fresh=True)
            if not fg.ok:
                return None
            w = fg.value
            from PIL import ImageGrab
            import io
            bbox = (w.left, w.top, w.left + max(1, w.width), w.top + max(1, w.height))
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue(), (w.left, w.top), img
        except Exception as e:
            log.debug("observe.grab_failed %s", e)
            return None

    # ---- a compact picture for prompts and the trail ----------------------------------------------------------
    def snapshot(self, spotify: bool = False) -> str:
        """One or two short lines — what a planner or recovery prompt gets, never the whole state."""
        parts = []
        fg = self.foreground()
        if fg.ok:
            parts.append(f"In front: {fg.detail}")
        wins = self.windows()
        if wins.ok:
            names = []
            for w in wins.value:
                a = _app(w)
                if a and a not in names:
                    names.append(a)
            if names:
                parts.append("Open: " + ", ".join(names[:12]))
        mons = self.monitors()
        if mons.ok and len(mons.value) > 1:
            parts.append(mons.detail)
        if spotify:
            sp = self.spotify()
            if sp.ok:
                parts.append(sp.detail)
        return "; ".join(parts)


def _app(w) -> str:
    try:
        from modules.vision.screen import app_label
        return app_label({"title": w.title, "process": w.process})
    except Exception:
        return (getattr(w, "process", "") or "").replace(".exe", "")


_ADDRESS_NAMES = ("address and search bar", "search or enter address", "search with google or enter address",
                  "address field", "address bar")


def _address_bar(hwnd: int) -> str:
    """The URL in a browser window's address bar (Chrome, Edge, Brave, Opera, Firefox), via UI
    Automation, without focusing the window. "" when it can't be read."""
    try:
        import uiautomation as auto
        top = auto.ControlFromHandle(hwnd)
        if top is None:
            return ""
        edit = top.EditControl(searchDepth=14, Compare=lambda c, d: (c.Name or "").strip().lower() in _ADDRESS_NAMES)
        if not edit.Exists(0.4, 0.1):
            return ""
        value = edit.GetValuePattern().Value or ""
        return value.strip()
    except Exception:
        return ""


observer = Observer()

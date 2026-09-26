"""
modules/learning/demonstration.py

Watch the user do something once, then do it for them next time.

After SAINT fails at a request (or the user says "watch me" / "let me show
you"), it watches for up to two minutes and turns what the user does into
commands:

    user opens Disk Cleanup from the Start menu     -> "open Disk Cleanup"
    user double-clicks the Recycle Bin              -> "double click Recycle Bin"
    user clicks "Clean up system files"             -> "click Clean up system files"
    user presses Ctrl+Shift+Esc                     -> "press ctrl+shift+esc"

It stops when the user says "done" / "that's it", after 15 quiet seconds, or
after two minutes. The steps are saved as a skill for the original request.

Only what's needed to repeat the steps is looked at: which app opened, which
window came to the front, the *name* of the button or icon clicked (Windows'
accessibility info), and keyboard shortcuts. Typed text is never recorded,
and nothing but the resulting commands is kept.
"""

import ctypes
import ctypes.wintypes
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from core.config import config
from core.events import event_bus, EventType

log = logging.getLogger("saint.learning")

_SHELL_START = {"searchhost", "startmenuexperiencehost", "shellexperiencehost", "searchapp", "searchui"}
_MODS = {0x11: "ctrl", 0x12: "alt", 0x10: "shift", 0x5B: "win", 0x5C: "win"}
_KEYS = {**{0x41 + i: chr(ord("a") + i) for i in range(26)}, **{0x30 + i: str(i) for i in range(10)},
         **{0x70 + i: f"f{i + 1}" for i in range(12)},
         0x0D: "enter", 0x1B: "escape", 0x2E: "delete", 0x09: "tab", 0x20: "space", 0x08: "backspace",
         0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down", 0x21: "pageup", 0x22: "pagedown",
         0x24: "home", 0x23: "end", 0x2C: "printscreen"}
# Pressed alone these are steps; letters/digits alone are typing (never recorded).
_ALONE_OK = {"enter", "escape", "delete", "tab", "printscreen"} | {f"f{i}" for i in range(1, 13)}


@dataclass
class Event:
    kind: str                 # launch | focus | click | keys | moved | state
    at: float
    app: str = ""             # display name of the app / window
    exe: str = ""             # process stem, e.g. "cleanmgr"
    name: str = ""            # clicked element's name
    button: str = "left"
    clicks: int = 1
    where: str = "window"     # window | desktop | taskbar | start   (moved: "left"/"right"/... screen)
    keys: str = ""
    x: int = 0
    y: int = 0
    path: str = ""            # launch: the program's .exe (so "open X" can find it next time)
    title: str = ""           # click: the window's title (a click on the title itself isn't a step)


# Names of whole areas, not buttons: clicking them only focuses the window.
_CONTAINER = re.compile(r"^(?:items view|shell folder view|folder ?view|content|workspace|document|pane|panel|client|"
                        r"grouping|list|tree|scroll ?bar|vertical|horizontal|main|window|chrome legacy window|"
                        r"desktop \d*|shell_defview|running applications)$", re.I)


# ---------------------------------------------------------------------- #
# Events -> commands
# ---------------------------------------------------------------------- #
def _step_for_click(e: Event) -> Optional[str]:
    name = re.sub(r"\s+", " ", (e.name or "")).strip()
    if e.where == "taskbar":
        from modules.desktop.uia import _clean_name
        if re.match(r"^show desktop", name, re.I):
            return "show the desktop"
        if re.match(r"^(?:start|search|task view|widgets|copilot|chat|show hidden icons|notification|action center|"
                    r"clock|volume|speakers|network|battery|wi-?fi|input indicator|system tray|quick settings|"
                    r"\d{1,2}:\d{2})", name, re.I):
            return None                      # Windows' own buttons, not an app to switch to
        app = _clean_name(name)
        return f"switch to {app}" if app else None
    if e.where == "desktop":
        if not name or name.lower() in ("desktop", "folderview", "shell_defview"):
            return "right click the desktop" if e.button == "right" else None
        verb = "right click" if e.button == "right" else "double click" if e.clicks >= 2 else "click"
        return f"{verb} {name} on the desktop"
    if not name or len(name) > 60 or _CONTAINER.match(name):
        return None
    if e.title and (name == e.title or name in e.title and len(name) > 12):
        return None                          # the title bar / tab strip: a focus, not a button
    verb = "right click" if e.button == "right" else "double click" if e.clicks >= 2 else "click"
    return f"{verb} {name}"


def _how_it_opened(e: Event) -> bool:
    """Start-menu / taskbar clicks and keys that were only the way to open an app."""
    return (e.kind == "click" and e.where in ("start", "taskbar")) or \
        (e.kind == "keys" and (e.exe in _SHELL_START or e.keys.startswith("win")))


def _is_user_input(e: Event) -> bool:
    """Something the user did (not something an app did by itself)."""
    return e.kind in ("click", "keys")


def summarize(events: List[Event]) -> List[str]:
    """What the user did, as commands SAINT can repeat (unrepeatable bits dropped)."""
    steps: List[tuple] = []                     # (command, event)
    pending_start: Optional[Event] = None       # a Start-menu click waiting to see what it opens
    last_launch: Optional[Event] = None
    input_since_launch = False                  # did the user click / press anything named since?
    for e in events:
        if e.kind == "launch":
            name = pending_start.name if pending_start and pending_start.name else e.app
            # Started by the app just opened (Bloxstrap starting Roblox, an updater
            # restarting itself) — not a step the user took.
            if last_launch is not None and not input_since_launch and pending_start is None \
                    and e.at - last_launch.at < 15:
                continue
            while steps and _how_it_opened(steps[-1][1]):
                steps.pop()
            pending_start = None
            last_launch, input_since_launch = e, False
            if name and not any(s.lower() == f"open {name}".lower() for s, _ in steps):
                steps.append((f"open {name}", e))
            continue
        if _is_user_input(e) and (e.kind == "keys" or e.name or e.where != "window"):
            input_since_launch = True
        if e.kind == "moved" and e.app:
            s = f"move {e.app} to my {e.where} screen"
            steps = [x for x in steps if not x[0].startswith(f"move {e.app} to ")]   # only where it ended up
            steps.append((s, e))
            continue
        if e.kind == "state" and e.app:
            steps.append((f"{e.name} {e.app}", e))     # "maximize Spotify"
            continue
        if e.kind == "click" and e.where == "start":
            pending_start = e
            continue
        if pending_start is not None and e.at - pending_start.at > 6:
            if pending_start.name:
                steps.append((f"open {pending_start.name}", pending_start))
            pending_start = None
        if e.kind == "click":
            s = _step_for_click(e)
        elif e.kind == "keys":
            s = f"press {e.keys}"
        elif e.kind == "focus":
            s = f"switch to {e.app}" if e.app else None
        else:
            s = None
        if s and (not steps or steps[-1][0] != s):
            steps.append((s, e))
    if pending_start is not None and pending_start.name:
        steps.append((f"open {pending_start.name}", pending_start))
    out: List[str] = []
    for s, _e in steps:
        # A switch straight after opening the same app is part of the launch.
        if out and s.startswith("switch to ") and out[-1].lower() == "open " + s[10:].lower():
            continue
        out.append(s)
    return out[:8]


# ---------------------------------------------------------------------- #
# Watching (Windows)
# ---------------------------------------------------------------------- #
def _app_name_for(exe: str, title: str) -> str:
    """'cleanmgr' + 'Disk Cleanup : Drive Selection' -> 'Disk Cleanup'."""
    try:
        from modules.desktop.apps import app_catalog
        entries = app_catalog.entries()
        for e in entries.values():
            tgt = (e.target or "").lower()
            if tgt.endswith("\\" + exe + ".exe") or os.path.splitext(os.path.basename(tgt))[0] == exe:
                return e.name
        head = re.split(r"\s*[:\-–—|]\s*", title or "")[0].strip()
        if head and app_catalog.resolve(head) is not None:
            return head
    except Exception:
        pass
    try:
        from modules.vision.screen import app_name
        return app_name(exe, title)
    except Exception:
        return exe


class Recorder:
    POLL = 0.03

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.phrase = ""
        self.events: List[Event] = []
        self.started = 0.0
        self._on_done: Optional[Callable[[List[str]], None]] = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, phrase: str, on_done: Optional[Callable[[List[str]], None]] = None) -> bool:
        if os.name != "nt" or not config.get("learning.watch_and_learn", True):
            return False
        if self.active:
            self.stop("restart")
        self.phrase, self.events, self.started = phrase, [], time.time()
        self._on_done = on_done
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="saint-demo")
        self._thread.start()
        log.info("learning.watch.start phrase=%r", phrase)
        return True

    def stop(self, reason: str = "user") -> List[str]:
        """Stop watching; returns the commands learned from what was seen."""
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)
        self._thread = None
        with self._lock:
            events = list(self.events)
        steps = summarize(events)
        log.info("learning.watch.stop reason=%s events=%d steps=%r", reason, len(events), steps)
        return steps

    # ------------------------------------------------------------------ #
    def _run(self):
        try:
            self._loop()
        except Exception:
            log.exception("learning.watch.crashed")
        finally:
            if self._on_done is not None and not self._stop.is_set():
                # Ended on its own (quiet / time limit): report back.
                cb, self._on_done = self._on_done, None
                self._stop.set()
                with self._lock:
                    events = list(self.events)
                try:
                    cb(summarize(events))
                except Exception:
                    log.exception("learning.watch.on_done_failed")

    def _add(self, e: Event):
        with self._lock:
            self.events.append(e)
        log.debug("learning.watch.event %s", e)

    def _loop(self):
        import win32gui
        import win32process
        from modules.desktop.controller import desktop
        user32 = ctypes.windll.user32
        own = os.getpid()
        max_sec = float(config.get("learning.watch_max_sec", 120))
        idle_sec = float(config.get("learning.watch_idle_sec", 15))
        try:
            from modules.desktop import uia
            auto = uia._auto()
        except Exception:
            auto = None

        def exe_of_pid(pid):
            try:
                import psutil
                return psutil.Process(pid).name().lower().replace(".exe", "")
            except Exception:
                return ""

        start_wins = desktop.list_windows()
        base = {w.hwnd for w in start_wins}
        running = {w.process.lower().replace(".exe", "") for w in start_wins}
        # Where each window is, to notice "moved Claude to my right screen" / "maximized Spotify".
        placed = {w.hwnd: (w.monitor, w.maximized, w.minimized) for w in start_wins}
        try:
            screen_name = {m.index: m.position for m in desktop.monitors()}
        except Exception:
            screen_name = {}
        fg = user32.GetForegroundWindow()
        held = {vk: bool(user32.GetAsyncKeyState(vk) & 0x8000) for vk in list(_KEYS) + list(_MODS) + [1, 2]}
        press_pos = {}
        seen = {}
        last_act = time.time()
        last_scan = 0.0
        last_pill = 0.0
        typed = False

        while not self._stop.is_set():
            now = time.time()
            if now - self.started > max_sec or (self.events and now - last_act > idle_sec) \
                    or (not self.events and now - self.started > 45):
                return
            if now - last_pill >= 1.0:
                # On screen: SAINT is watching (the ring fills towards the time limit).
                last_pill = now
                n = len(self.events)
                event_bus.emit_event(EventType.TASK_PROGRESS, {
                    "id": TASK_ID, "description": "watching how you do it",
                    "progress": min(0.99, (now - self.started) / max_sec),
                    "text": (f"{n} step{'s' if n != 1 else ''} so far · " if n else "") + "say “done” when finished"})
            # ---- mouse ------------------------------------------------------------
            for vk, button in ((1, "left"), (2, "right")):
                down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                if down and not held[vk]:
                    pt = ctypes.wintypes.POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    press_pos[vk] = (pt.x, pt.y, now)
                elif not down and held[vk] and vk in press_pos:
                    x, y, t0 = press_pos.pop(vk)
                    e = self._describe_click(x, y, button, own, auto, win32gui, win32process, exe_of_pid)
                    if e is not None:
                        prev = self.events[-1] if self.events else None
                        if (prev is not None and prev.kind == "click" and button == "left" and prev.button == "left"
                                and abs(prev.x - x) < 6 and abs(prev.y - y) < 6 and t0 - prev.at < 0.5):
                            prev.clicks = 2
                        else:
                            e.at = t0
                            self._add(e)
                        last_act = now
                held[vk] = down
            # ---- keyboard shortcuts (never plain typing) ---------------------------------
            mods = sorted({name for vk, name in _MODS.items() if user32.GetAsyncKeyState(vk) & 0x8000},
                          key=["ctrl", "alt", "shift", "win"].index)
            for vk, key in _KEYS.items():
                down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                if down and not held[vk]:
                    fg_hwnd = user32.GetForegroundWindow()
                    if not self._is_own(fg_hwnd, own, win32process):
                        try:
                            fg_exe = exe_of_pid(win32process.GetWindowThreadProcessId(fg_hwnd)[1])
                        except Exception:
                            fg_exe = ""
                        real = [m for m in mods if m != "shift"]
                        if real:
                            self._add(Event("keys", now, keys="+".join(mods + [key]), exe=fg_exe))
                            last_act = now
                        elif key in _ALONE_OK:
                            self._add(Event("keys", now, keys=key, exe=fg_exe))
                            last_act = now
                        else:
                            typed = True
                held[vk] = down
            for vk in _MODS:
                held[vk] = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            # ---- windows: new apps, focus changes ---------------------------------------
            if now - last_scan > 0.4:
                last_scan = now
                try:
                    wins = desktop.list_windows()
                except Exception:
                    wins = []
                recent_act = bool(self.events) and now - self.events[-1].at < 3
                for w in wins:
                    if w.hwnd in base or desktop._is_own(w):
                        continue
                    stem = w.process.lower().replace(".exe", "")
                    first = seen.setdefault(w.hwnd, now)
                    if stem in _SHELL_START or (not w.foreground and now - first < 5):
                        continue                  # wait until it comes to the front (or give up)
                    base.add(w.hwnd)
                    if not w.foreground:
                        continue                  # background windows (updaters, helpers) aren't steps
                    new_app = stem not in running
                    running.add(stem)
                    if new_app or not recent_act:
                        self._add(Event("launch", now, app=_app_name_for(stem, w.title), exe=stem,
                                        path=_exe_path(w.hwnd)))
                        last_act = now
                for w in wins:
                    if desktop._is_own(w):
                        continue
                    was = placed.get(w.hwnd)
                    placed[w.hwnd] = (w.monitor, w.maximized, w.minimized)
                    if was is None or not (w.foreground or was[2] != w.minimized):
                        continue
                    from modules.vision.screen import app_label
                    label = app_label({"title": w.title, "process": w.process})
                    if was[0] != w.monitor and not w.minimized and screen_name.get(w.monitor):
                        self._add(Event("moved", now, app=label, exe=w.process, where=screen_name[w.monitor]))
                        last_act = now
                    elif not was[1] and w.maximized:
                        self._add(Event("state", now, app=label, name="maximize"))
                        last_act = now
                    elif not was[2] and w.minimized:
                        self._add(Event("state", now, app=label, name="minimize"))
                        last_act = now
                cur = user32.GetForegroundWindow()
                if cur != fg:
                    fg = cur
                    w = next((x for x in wins if x.hwnd == cur), None)
                    caused = self.events and now - self.events[-1].at < 1.5
                    if w is not None and not caused and not desktop._is_own(w):
                        from modules.vision.screen import app_label
                        self._add(Event("focus", now, app=app_label({"title": w.title, "process": w.process}),
                                        exe=w.process.lower().replace(".exe", "")))
                        last_act = now
            time.sleep(self.POLL)
        if typed:
            log.info("learning.watch typing was seen (not recorded)")

    @staticmethod
    def _is_own(hwnd, own, win32process) -> bool:
        try:
            return win32process.GetWindowThreadProcessId(hwnd)[1] == own
        except Exception:
            return False

    def _describe_click(self, x, y, button, own, auto, win32gui, win32process, exe_of_pid) -> Optional[Event]:
        try:
            root = win32gui.GetAncestor(win32gui.WindowFromPoint((x, y)), 2)      # GA_ROOT
            cls = win32gui.GetClassName(root)
            pid = win32process.GetWindowThreadProcessId(root)[1]
        except Exception:
            return None
        if pid == own:
            return None                                   # SAINT's own windows
        exe = exe_of_pid(pid)
        if cls in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            where = "taskbar"
        elif cls in ("Progman", "WorkerW"):
            where = "desktop"
        elif exe in _SHELL_START:
            where = "start"
        else:
            where = "window"
        name = ""
        if auto is not None:
            try:
                c = auto.ControlFromPoint(x, y)
                for _ in range(3):
                    if c is None:
                        break
                    n = (c.Name or "").strip()
                    if n and n.lower() not in ("desktop", "running applications", "folderview"):
                        name = n
                        break
                    c = c.GetParentControl()
            except Exception:
                pass
        title = ""
        try:
            title = win32gui.GetWindowText(root)
        except Exception:
            pass
        return Event("click", time.time(), app=_app_name_for(exe, title) if where == "window" else "",
                     exe=exe, name=name, button=button, where=where, x=x, y=y, title=title)


def _exe_path(hwnd: int) -> str:
    try:
        import psutil
        import win32process
        return psutil.Process(win32process.GetWindowThreadProcessId(hwnd)[1]).exe()
    except Exception:
        return ""


recorder = Recorder()


# ---------------------------------------------------------------------- #
# The spoken flow
# ---------------------------------------------------------------------- #
TASK_ID = "learning-watch"


def announce(text: str, speak: bool = True):
    """Say what was learned (and close the "watching" pill)."""
    event_bus.emit_event(EventType.TASK_DONE, {"id": TASK_ID, "summary": text, "announce": speak,
                                               "status": "done", "description": "watching how you do it"})


def _remember_programs(steps: List[str], events: List[Event]):
    """'open Bloxstrap' must work next time even though Bloxstrap is only pinned
    to the taskbar: programs seen starting that SAINT can't find by name are
    added to its app list (desktop.apps) with the .exe that ran."""
    try:
        from modules.desktop.apps import app_catalog
        known = dict(config.get("desktop.apps", {}) or {})
        added = False
        for e in events:
            if e.kind != "launch" or not e.app or not e.path or f"open {e.app}".lower() not in \
                    [s.lower() for s in steps]:
                continue
            if app_catalog.resolve(e.app) is None and os.path.isfile(e.path):
                known[e.app] = e.path
                added = True
                log.info("learning.watch.app_added %r -> %s", e.app, e.path)
        if added:
            config.set("desktop.apps", known)
    except Exception:
        log.exception("learning.watch.app_add_failed")


def learn_from(phrase: str, steps: List[str], events: Optional[List[Event]] = None) -> str:
    """Save what was seen as a skill and say what was learned."""
    from modules.learning.planner import understood
    from modules.learning.skills import skills
    if events:
        _remember_programs(steps, events)
    good = [s for s in steps if understood(s)]
    if not good:
        return "I didn't see anything I know how to repeat, so I haven't learned that one."
    skill = skills.learn(phrase, good, "shown")
    if skill is None:
        return "Okay."
    what = ", then ".join(good)
    note = " Some of what you did I can't repeat, so I left it out." if len(good) < len(steps) else ""
    text = (f"Got it. Next time you say “{skill.said or phrase}”, I'll {what}.{note} "
            "Say “forget that” if I got it wrong.")
    global _last_result
    _last_result = (time.time(), text)
    return text


_last_result = (0.0, "")


def just_finished(within: float = 90) -> str:
    """What SAINT said when it stopped watching on its own a moment ago (so a
    late "I'm all done" gets an answer instead of going to the chat model)."""
    at, text = _last_result
    return text if text and time.time() - at < within else ""


def watch_for(phrase: str) -> bool:
    """Start watching the user do ``phrase``; announces the result when it ends on its own."""
    def done(steps):
        with recorder._lock:
            events = list(recorder.events)
        announce(learn_from(phrase, steps, events))
    return recorder.start(phrase, on_done=done)


def offer(phrase: str) -> bool:
    """After a failure: *ask* to watch ("want to show me?") instead of starting
    to record whatever the user does next. "Yes" starts watching."""
    if os.name != "nt" or not config.get("learning.watch_and_learn", True):
        return False
    from modules.agent.confirm import PendingAction, confirmations

    def run():
        if not watch_for(phrase):
            return "I can't watch the screen on this computer."
        return "Okay, I'm watching. Do it now, then say “done”."
    confirmations.ask(PendingAction(description="watch you do it", run=run, tool="learning.watch"))
    return True


def finish() -> str:
    """The user said "done": stop watching and learn."""
    phrase = recorder.phrase
    recorder._on_done = None
    steps = recorder.stop("user")
    with recorder._lock:
        events = list(recorder.events)
    text = learn_from(phrase, steps, events)
    announce(text, speak=False)             # the reply says it; this only closes the pill
    return text

"""
modules/workspace/store.py

"Save this workspace as Coding" records every open window — which program,
which monitor, where, and whether it was maximized — plus what Spotify was
playing. "Restore Coding" puts existing windows back, starts programs that
aren't open, and resumes the music. Stored in data/workspaces.json.

Browser tabs and files open inside programs aren't captured (Windows doesn't
expose them); programs reopen however they normally start.
"""

import difflib
import json
import logging
import os
import threading
import time
from typing import Callable, Dict, List, Optional

from core.paths import data_path
from modules.automation.tools import ToolError

log = logging.getLogger("saint.workspace")

# Windows that are part of the shell or SAINT itself — never saved.
_SKIP_PROCESSES = {"explorer.exe", "textinputhost.exe", "shellexperiencehost.exe", "searchhost.exe",
                   "startmenuexperiencehost.exe", "applicationframehost.exe", "systemsettings.exe",
                   "lockapp.exe", "python.exe", "pythonw.exe"}


def _norm_name(name: str) -> str:
    return " ".join((name or "").lower().split())


class WorkspaceStore:
    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return self._path or str(data_path("workspaces.json"))

    def all(self) -> Dict[str, Dict]:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return {}

    def _write(self, data: Dict):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)

    def get(self, name: str) -> Optional[Dict]:
        data = self.all()
        key = _norm_name(name)
        if key in data:
            return data[key]
        close = difflib.get_close_matches(key, list(data), n=1, cutoff=0.8)
        return data[close[0]] if close else None

    def save(self, name: str, snapshot: Dict):
        data = self.all()
        data[_norm_name(name)] = dict(snapshot, name=name.strip(), saved_at=time.time())
        with self._lock:
            self._write(data)

    def delete(self, name: str) -> bool:
        data = self.all()
        key = _norm_name(name)
        if key not in data:
            return False
        data.pop(key)
        with self._lock:
            self._write(data)
        return True


workspaces = WorkspaceStore()


# ---------------------------------------------------------------------- #
# Capture
# ---------------------------------------------------------------------- #
def _exe_for(hwnd: int) -> str:
    try:
        import psutil
        import win32process
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).exe()
    except Exception:
        return ""


def capture() -> Dict:
    import win32gui
    from modules.desktop.controller import desktop
    windows = []
    for w in desktop.list_windows():
        if w.process.lower() in _SKIP_PROCESSES or w.title == "SAINT":
            continue
        try:
            flags, show, pt_min, pt_max, normal = win32gui.GetWindowPlacement(w.hwnd)
        except Exception:
            continue
        windows.append({"process": w.process, "exe": _exe_for(w.hwnd), "title": w.title, "monitor": w.monitor,
                        "normal": list(normal), "state": "max" if w.maximized else "min" if w.minimized else "normal"})
    snap = {"windows": windows, "spotify": None}
    try:
        from modules.automation.tools import get_tool_registry
        res = get_tool_registry().execute("spotify.current")
        if res.success and res.result.get("uri"):
            snap["spotify"] = {"context_uri": res.result.get("context_uri") or "", "uri": res.result["uri"],
                               "track": res.result.get("track"), "artists": res.result.get("artists"),
                               "was_playing": bool(res.result.get("is_playing"))}
    except Exception:
        pass
    return snap


# ---------------------------------------------------------------------- #
# Restore
# ---------------------------------------------------------------------- #
def plan_matches(saved: List[Dict], current: List) -> List[tuple]:
    """Pair each saved window with an open window of the same program (best
    title match first). Returns [(saved, current_or_None)]."""
    used = set()
    pairs = []
    for s in saved:
        cands = [w for w in current if w.process.lower() == s["process"].lower() and w.hwnd not in used]
        best = max(cands, key=lambda w: difflib.SequenceMatcher(None, w.title, s["title"]).ratio(), default=None)
        if best is not None:
            used.add(best.hwnd)
        pairs.append((s, best))
    return pairs


def restore(snap: Dict, progress: Optional[Callable] = None, cancel=None) -> Dict:
    import win32con
    import win32gui
    from modules.desktop.controller import desktop
    current = desktop.list_windows()
    pairs = plan_matches(snap.get("windows", []), current)
    monitors = {m.index for m in desktop.monitors()}
    placed, launched, missing = 0, [], []
    launched_exes = set()
    for i, (s, w) in enumerate(pairs):
        if cancel is not None and cancel.is_set():
            break
        if progress:
            progress(i / max(1, len(pairs)), f"arranging {s['process']}")
        if w is None:
            exe = s.get("exe") or ""
            if not exe or exe.lower() in launched_exes:
                missing.append(s["process"])
                continue
            launched_exes.add(exe.lower())
            w = _launch_and_wait(exe, s["process"], {x.hwnd for x in current})
            if w is None:
                missing.append(s["process"])
                continue
            launched.append(os.path.splitext(s["process"])[0])
        if s.get("monitor") not in monitors:
            continue                                   # that screen isn't connected now
        show = {"max": win32con.SW_SHOWMAXIMIZED, "min": win32con.SW_SHOWMINIMIZED}.get(s["state"],
                                                                                      win32con.SW_SHOWNORMAL)
        try:
            if s["state"] == "max" and win32gui.IsZoomed(w.hwnd):
                win32gui.ShowWindow(w.hwnd, win32con.SW_RESTORE)   # so it re-maximizes on the right screen
            win32gui.SetWindowPlacement(w.hwnd, (0, show, (-1, -1), (-1, -1), tuple(s["normal"])))
            placed += 1
        except Exception as e:
            log.info("workspace.place_failed %s %s", s["process"], e)
    music = ""
    sp = snap.get("spotify")
    if sp and sp.get("was_playing"):
        try:
            from modules.automation.tools import get_tool_registry
            uri = sp.get("context_uri") or sp.get("uri")
            if get_tool_registry().execute("spotify.play", uri=uri).success:
                music = " and put your music back on"
        except Exception:
            pass
    name = snap.get("name", "the workspace")
    parts = [f"Restored {name}: {placed} window{'s' if placed != 1 else ''} in place"]
    if launched:
        parts.append("opened " + ", ".join(sorted(set(launched))))
    text = ", ".join(parts) + music + "."
    if missing:
        text += f" I couldn't bring back {', '.join(sorted(set(missing))[:4])}."
    text += " Browser tabs aren't saved, so those open as usual."
    return {"placed": placed, "launched": launched, "missing": missing, "summary": text}


def _launch_and_wait(exe: str, process: str, before: set, timeout: float = 10.0):
    from modules.desktop.controller import desktop
    try:
        if "\\windowsapps\\" in exe.lower():
            from modules.desktop.apps import app_catalog, launch
            entry = app_catalog.resolve(os.path.splitext(process)[0])
            if entry is None:
                return None
            launch(entry)
        else:
            os.startfile(exe)
    except OSError as e:
        log.info("workspace.launch_failed %s %s", exe, e)
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.4)
        for w in desktop.list_windows():
            if w.hwnd not in before and w.process.lower() == process.lower():
                time.sleep(0.6)                        # let it finish its own first layout
                return w
    return None


def describe(snap: Dict) -> str:
    procs = [os.path.splitext(w["process"])[0] for w in snap.get("windows", [])]
    names = sorted(set(procs), key=procs.index)
    extra = f", and Spotify ({snap['spotify']['track']})" if snap.get("spotify") else ""
    return ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "") + extra

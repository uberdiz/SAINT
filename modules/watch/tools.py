"""Watch tools: start / list / cancel watchers, what changed, take me back, follow a screen."""

import os

from core.config import config
from modules.automation.tools import P, PermissionLevel, Tool, ToolError
from modules.watch.watchers import Watch, watch_manager


def _available():
    return (True, "") if config.get("modules.watch", True) else (False, "Watching is turned off in Settings.")


def _window(target: str):
    from modules.desktop.controller import desktop
    q = (target or "this").strip()
    w = desktop.target_window() if q in ("", "this", "it", "that", "this window", "the window") else \
        desktop.find_window(q)
    if w is None:
        raise ToolError("I don't see a window to watch.", "NOT_FOUND")
    return w


def add(kind: str, target: str = "this"):
    kind = (kind or "finished").lower()
    if kind == "download":
        from modules.files.paths import known_folder
        folder = known_folder("downloads")
        if not folder:
            raise ToolError("I can't find your Downloads folder.", "NOT_FOUND")
        w = watch_manager.add(Watch("download", "your download", folder=folder))
        return {"id": w.id, "summary": "I'll tell you when the download finishes."}
    win = _window(target)
    from modules.vision.screen import app_label
    label = app_label(win.to_dict())
    w = watch_manager.add(Watch(kind, label, hwnd=win.hwnd))
    said = {"finished": f"I'll tell you when {label} finishes.", "closed": f"I'll tell you when {label} closes.",
            "title": f"I'll tell you when {label} changes.", "change": f"I'll tell you when {label} changes."}
    return {"id": w.id, "label": label, "summary": said.get(kind, f"Watching {label}.")}


def list_watches():
    items = watch_manager.active()
    return {"watches": [{"id": w.id, "kind": w.kind, "label": w.label} for w in items],
            "summary": ("I'm watching " + ", ".join(f"{w.label} ({w.kind})" for w in items) + ".") if items
            else "I'm not watching anything."}


def cancel(label: str = ""):
    items = watch_manager.active()
    hits = [w for w in items if not label or label.lower() in w.label.lower()]
    for w in hits:
        watch_manager.remove(w.id)
    return {"cancelled": len(hits), "summary": f"Stopped watching {hits[0].label}." if len(hits) == 1 else
            f"Stopped {len(hits)} watches." if hits else "I wasn't watching that."}


def what_changed(minutes: int = 0, since_left: bool = False):
    from modules.watch.snapshots import describe_changes, snapshot_log, take
    base = snapshot_log.baseline(minutes or None, since_left=since_left)
    if base is None:
        if since_left:
            base = snapshot_log.baseline(None)
        if base is None:
            raise ToolError("I haven't been watching long enough to compare yet — ask again in a minute.",
                            "NO_HISTORY")
    return {"summary": describe_changes(base, take())}


def take_me_back():
    w = watch_manager.last_fired
    if w is None or not w.hwnd:
        raise ToolError("There's nothing I was watching to go back to.", "NOT_FOUND")
    from modules.desktop.controller import desktop
    try:
        info = desktop._activate(desktop._info(w.hwnd))
    except Exception:
        raise ToolError(f"{w.label} isn't open any more.", "NOT_FOUND")
    return {"focused": w.label, "summary": f"Here's {w.label}."}


def follow_screen(which: str = ""):
    from modules.agent.context import desktop_context
    which = (which or "").strip().lower()
    if which in ("", "all", "both", "none", "off", "every", "any"):
        desktop_context.set_follow_monitor(None)
        return {"summary": "Okay, I'll use whichever screen you're working on."}
    from modules.desktop.controller import desktop
    mon = desktop.resolve_monitor(which, 1)
    desktop_context.set_follow_monitor(mon.index)
    return {"monitor": mon.index, "summary": f"Okay — “click”, “read this” and “what's on "
                                             f"screen” now mean {desktop.monitor_label(mon)}."}


def register_watch_tools(registry):
    tools = [
        Tool("watch.add", "Watch a window or the Downloads folder and say when it finishes, closes or changes",
             {"kind": "string"}, PermissionLevel.LOW, add,
             parameters={"kind": P("string", enum=["finished", "closed", "title", "change", "download"]),
                         "target": P("string", "window/app name; 'this' for the one in front", required=False,
                                     default="this")}, llm_exposed=True, category="watch"),
        Tool("watch.list", "What SAINT is watching", {}, PermissionLevel.LOW, list_watches, parameters={},
             llm_exposed=True, category="watch"),
        Tool("watch.cancel", "Stop watching something (or everything)", {}, PermissionLevel.LOW, cancel,
             parameters={"label": P("string", required=False, default="")}, llm_exposed=True, category="watch"),
        Tool("watch.what_changed", "What changed on screen recently or since the user stepped away", {},
             PermissionLevel.LOW, what_changed,
             parameters={"minutes": P("integer", required=False, default=0, minimum=0, maximum=120),
                         "since_left": P("boolean", required=False, default=False)},
             llm_exposed=True, category="watch"),
        Tool("watch.take_me_back", "Switch to the window a watch just reported on", {}, PermissionLevel.LOW,
             take_me_back, parameters={}, llm_exposed=True, category="watch"),
        Tool("watch.follow_screen", "Make screen commands use one monitor (left/right/main/second) or all",
             {"which": "string"}, PermissionLevel.LOW, follow_screen,
             parameters={"which": P("string", required=False, default="")}, llm_exposed=True, category="watch"),
    ]
    for t in tools:
        t.availability = _available
        registry.register(t)

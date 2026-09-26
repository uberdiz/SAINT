"""
modules/files/tools.py

File and storage tools. Read-only ones (drive space, biggest folders, junk
report, finding games/emulators) are safe for the LLM; the ones that change
things either create new files (extract, compress) or always ask first
(recycle, move — core/permissions.ALWAYS_CONFIRM) and are never offered to
the LLM. Long work (scans, extraction, big moves) runs as background tasks
that report progress and announce when they finish.
"""

import logging
import os
import threading
import time
from typing import Dict, List, Optional

from modules.automation.tools import P, PermissionLevel, Tool, ToolError
from modules.files import archives, junk, ops, scan
from modules.files.paths import (EMULATOR_NAMES, ROM_EXTENSIONS, denied, fixed_drives, known_folder,
                                 resolve_folder)
from modules.files.scan import human

log = logging.getLogger("saint.files")

# The last cleanup SAINT proposed, so "yes" / "only the duplicates" can act on it.
_plan_lock = threading.Lock()
_last_plan: Dict = {}


def last_plan(max_age: float = 600) -> Optional[Dict]:
    with _plan_lock:
        if _last_plan and time.time() - _last_plan.get("at", 0) < max_age:
            return dict(_last_plan)
    return None


def _remember_plan(rep: Dict):
    with _plan_lock:
        _last_plan.clear()
        _last_plan.update(rep, at=time.time())


def _tasks():
    from modules.automation.tasks import task_manager
    return task_manager


def _folder(name: str) -> str:
    path = resolve_folder(name)
    if not path:
        raise ToolError(f"I don't know where {name} is. You can name folders in Settings, or say the full path.",
                        "NOT_FOUND")
    return path


# ---------------------------------------------------------------------- #
def drive_overview(drive: str = ""):
    drives = scan.drive_overview()
    if drive:
        one = [d for d in drives if d["letter"] == drive.strip()[:1].upper()]
        if one:
            d = one[0]
            return {"drives": one, "summary": f"{d['letter']}: has {human(d['free'])} free of "
                                              f"{human(d['total'])} ({d['percent']:.0f}% used)."}
    parts = [f"{d['letter']}: has {human(d['free'])} free of {human(d['total'])}" for d in drives]
    low = [d for d in drives if d["free"] < 0.05 * d["total"] or d["free"] < 10e9]
    summary = "; ".join(parts) + "."
    if low:
        summary += " " + " and ".join(f"{d['letter']}:" for d in low) + (" is" if len(low) == 1 else " are") + \
                   " nearly full."
    return {"drives": drives, "summary": summary}


def biggest(target: str = "", refresh: bool = False):
    """What's using the space on a drive or in a folder (background scan, cached)."""
    root = _folder(target) if target else "C:\\"
    if not refresh:
        hit = scan.cached(root)
        if hit:
            return {"cached": True, "root": root, "top": scan.top_folders(hit, 8), "total": hit["total"],
                    "summary": _biggest_summary(root, hit)}

    def work(progress, cancel):
        res = scan.scan_tree(root, depth=3, progress=progress, cancel=cancel)
        scan.remember(res)
        return {"root": root, "top": scan.top_folders(res, 8), "total": res["total"],
                "summary": _biggest_summary(root, res)}
    task = _tasks().create_callable_task(f"scanning {root.rstrip(chr(92))}", work)
    return {"started": task.id, "root": root,
            "summary": f"Scanning {root.rstrip(chr(92))} — I'll tell you what's using the space when it's done."}


def _biggest_summary(root: str, res: Dict) -> str:
    top = scan.top_folders(res, 5)
    if not top:
        return f"{root} is nearly empty."
    items = ", ".join(f"{_short(f['path'], root)} ({human(f['size'])})" for f in top)
    return f"On {root.rstrip(chr(92))}, the biggest are {items}."


def _short(path: str, root: str) -> str:
    rel = os.path.relpath(path, root)
    return rel if rel != "." else path


def junk_report(scope: str = "downloads"):
    """Find what could be cleaned up in Downloads, on a drive, or everywhere."""
    downloads = known_folder("downloads")
    if scope in ("", "downloads"):
        rep = junk.report("downloads", downloads=downloads)
        _remember_plan(rep)
        return dict(rep, summary=junk.summarize(rep))
    if scope != "all":
        scope = _folder(scope)
        if os.path.splitdrive(scope)[1].strip("\\/"):
            raise ToolError("I can check your Downloads, a whole drive, or everything.", "INVALID")

    def work(progress, cancel):
        rep = junk.report(scope, downloads=downloads, progress=progress, cancel=cancel)
        _remember_plan(rep)
        return dict(rep, summary=_report_speech(rep))

    def done(task):
        rep = (task.result or {})
        if rep.get("recyclable"):
            _ask_to_recycle(rep)
    task = _tasks().create_callable_task(f"checking {scope.rstrip(chr(92)) if scope != 'all' else 'your drives'} "
                                         f"for junk", work, on_done=done)
    return {"started": task.id, "summary": "Looking for things you can clear — I'll tell you what I find."}


def _report_speech(rep: Dict) -> str:
    text = junk.summarize(rep)
    for f in [f for f in rep["findings"] if f["category"] == "old_windows"]:
        drive = f["path"].rstrip(chr(92))
        if any(p.lower().endswith(("windows.old", "$windows.~bt", "$windows.~ws")) for p in f.get("paths", [])) \
                and not any(p.lower().endswith("\\users") for p in f.get("paths", [])):
            text += (f" {drive} has {human(f['size'])} left over from a Windows upgrade — Disk Cleanup's "
                     f"“Previous Windows installations” removes it safely.")
        else:
            text += (f" {drive} holds an old Windows installation using {human(f['size'])}. That's too big for the "
                     "Recycle Bin, so I won't touch it — copy anything you want from its Users folder first, then "
                     "you can format the drive in Disk Management.")
    if any(f["action"] == "cleanmgr" for f in rep["findings"]):
        text += " Windows Update leftovers are best cleared with Disk Cleanup — say \u201copen disk cleanup\u201d."
    return text


def _ask_to_recycle(rep: Dict, only: Optional[List[str]] = None):
    """Park "move those to the Recycle Bin?" as a yes/no question."""
    # Old installers are only "worth a look" — they go when you ask for them by name.
    items = [f for f in rep["findings"]
             if (f["action"] == "recycle" or only and f["category"] == "old_installer")
             and (not only or f["category"] in only)]
    return ask_to_recycle_findings(items)


def ask_to_recycle_findings(items: List[Dict]):
    """Ask before recycling these cleanup findings (ones already gone are skipped)."""
    from modules.agent.confirm import PendingAction, confirmations
    items = [f for f in items if f.get("path") and os.path.exists(f["path"])]
    if not items:
        return None
    size = sum(f["size"] for f in items)
    desc = f"move {len(items)} item{'s' if len(items) != 1 else ''} ({human(size)}) to the Recycle Bin"

    def run():
        from modules.automation.tools import get_tool_registry
        res = get_tool_registry().execute("files.recycle", _confirmed=True, paths=[f["path"] for f in items])
        return res.result["summary"] if res.success else (res.error or "That didn't work.")
    confirmations.ask(PendingAction(description=desc, run=run, tool="files.recycle"))
    return desc


def cleanup_plan(scope: str = "downloads", only: str = ""):
    """Inspect, group and propose; ask before moving anything to the Recycle Bin."""
    res = junk_report(scope)
    if res.get("started"):
        return res
    cats = [c.strip() for c in only.split(",") if c.strip()] if only else None
    desc = _ask_to_recycle(res, cats)
    return dict(res, asked=bool(desc), question=desc or "")


def recycle(paths: List[str]):
    res = ops.recycle(paths)
    n, freed = len(res["recycled"]), res["freed"]
    text = f"Moved {n} item{'s' if n != 1 else ''} to the Recycle Bin, freeing {human(freed)}." if n else \
        "Nothing was moved."
    if res["refused"]:
        r = res["refused"][0]
        text += f" I left {os.path.basename(r['path']) or r['path']} alone: {r['reason']}."
        if len(res["refused"]) > 1:
            text += f" ({len(res['refused']) - 1} more skipped.)"
    if res["failed"]:
        text += f" {len(res['failed'])} couldn't be moved — they may be open in another app."
    with _plan_lock:
        _last_plan.clear()
    return dict(res, summary=text)


def move(source: str, destination: str):
    src = resolve_folder(source) or (source if os.path.exists(source) else None)
    if not src:
        raise ToolError(f"I can't find {source}.", "NOT_FOUND")
    dest = _folder(destination)

    def work(progress, cancel):
        progress(0.05, f"moving {os.path.basename(src)}")
        r = ops.move(src, dest)
        return dict(r, summary=f"Moved {os.path.basename(src)} ({human(r['size'])}) to {dest}.")
    task = _tasks().create_callable_task(f"moving {os.path.basename(src)} to {dest}", work)
    return {"started": task.id, "summary": f"Moving {os.path.basename(src)} to {dest} — I'll tell you when it's done."}


def open_folder(name: str):
    path = _folder(name)
    ops.open_in_explorer(path)
    return {"opened": path}


def extract(archive: str = "latest", source: str = "downloads", destination: str = "",
            delete_after: bool = False):
    folder = _folder(source) if source else known_folder("downloads")
    if archive in ("", "latest", "newest", "last", "first", "recent"):
        path = archives.newest_archive(folder)
        if not path:
            raise ToolError(f"There's no .rar, .zip or .7z file in {folder}.", "NOT_FOUND")
    elif os.path.isfile(archive):
        path = archive
    else:
        path = archives.find_archive(folder, archive)
        if not path:
            raise ToolError(f"I couldn't find an archive called {archive} in {folder}.", "NOT_FOUND")
    dest = _folder(destination) if destination else os.path.dirname(path)
    if denied(dest) and not os.path.splitdrive(dest)[1].strip("\\/") == "":
        raise ToolError(f"I won't extract into {dest}.", "DENIED")
    name = os.path.basename(path)

    def work(progress, cancel):
        return archives.extract(path, dest, progress=progress, cancel=cancel)

    def done(task):
        if delete_after and task.status.value == "completed":
            from modules.agent.confirm import PendingAction, confirmations

            def run():
                from modules.automation.tools import get_tool_registry
                res = get_tool_registry().execute("files.recycle", _confirmed=True, paths=[path])
                return res.result["summary"] if res.success else (res.error or "That didn't work.")
            confirmations.ask(PendingAction(description=f"move {name} to the Recycle Bin", run=run,
                                            tool="files.recycle"))
            _say(f"Done extracting. Should I move {name} to the Recycle Bin?")
    task = _tasks().create_callable_task(f"extracting {name}", work, on_done=done)
    return {"started": task.id, "archive": path, "destination": dest,
            "summary": f"Extracting {name} to {dest} — I'll tell you when it's done."}


def _say(text: str):
    try:
        from core.runtime import runtime
        if runtime.controller:
            runtime.controller.announce(text, source="confirm")
    except Exception:
        pass


def compress(folder: str, fmt: str = "zip"):
    path = resolve_folder(folder) or (folder if os.path.exists(folder) else None)
    if not path:
        raise ToolError(f"I can't find {folder}.", "NOT_FOUND")
    if denied(path):
        raise ToolError(f"I won't compress {path}.", "DENIED")
    task = _tasks().create_callable_task(f"compressing {os.path.basename(path)}",
                                         lambda progress, cancel: archives.compress(path, fmt, progress, cancel))
    return {"started": task.id, "summary": f"Compressing {os.path.basename(path)} into a {fmt} — I'll tell you "
                                           f"when it's done."}


def find(kind: str = "games"):
    """Games, emulators or ROMs across your drives (so you know what to keep)."""
    kind = (kind or "games").lower()

    def work(progress, cancel):
        found: List[Dict] = []
        if kind == "games":
            try:
                from modules.steam.library import steam_library
                for g in steam_library.games(force=True):
                    found.append({"name": g.name, "path": g.path, "size": g.size, "source": "Steam"})
            except Exception:
                pass
        drives = fixed_drives()
        for i, root in enumerate(drives):
            if cancel.is_set():
                break
            progress(i / max(1, len(drives)), f"looking on {root}")
            for dirpath, dirs, files in _walk(root, 4, cancel):
                base = os.path.basename(dirpath).lower()
                if kind in ("emulators", "games") and any(e in base for e in EMULATOR_NAMES):
                    found.append({"name": os.path.basename(dirpath), "path": dirpath, "source": "emulator"})
                    dirs[:] = []
                    continue
                if kind == "games" and base in ("games", "xboxgames", "epic games", "gog games", "riot games") \
                        and dirpath.count(os.sep) <= 2:
                    for d in dirs:
                        found.append({"name": d, "path": os.path.join(dirpath, d), "source": base})
                    dirs[:] = []
                    continue
                if kind == "roms":
                    roms = [f for f in files if os.path.splitext(f)[1].lower() in ROM_EXTENSIONS]
                    if len(roms) >= 3:
                        found.append({"name": os.path.basename(dirpath), "path": dirpath, "count": len(roms),
                                      "source": "roms"})
        seen, unique = set(), []
        for f in found:
            key = os.path.normcase(f["path"])
            if key not in seen:
                seen.add(key)
                unique.append(f)
        names = ", ".join(f["name"] for f in unique[:10])
        more = f", and {len(unique) - 10} more" if len(unique) > 10 else ""
        summary = (f"I found {len(unique)} {kind}: {names}{more}." if unique else f"I didn't find any {kind}.")
        return {"kind": kind, "found": unique, "summary": summary}
    task = _tasks().create_callable_task(f"looking for your {kind}", work)
    return {"started": task.id, "summary": f"Looking for your {kind} — one moment."}


def _walk(root: str, max_depth: int, cancel):
    base = root.rstrip("\\").count(os.sep)
    for dirpath, dirs, files in os.walk(root):
        if cancel.is_set():
            return
        if dirpath.count(os.sep) - base >= max_depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if not d.startswith(("$", ".")) and d.lower() not in
                   ("windows", "programdata", "appdata", "system volume information", "node_modules",
                    "program files", "program files (x86)", "windowsapps", "recovery")]
        yield dirpath, dirs, files


def make_folder(name: str, parent: str = ""):
    where = _folder(parent) if parent else known_folder("desktop")
    res = ops.make_folder(where, name)
    verb = "There's already a folder called" if res["existed"] else "Made a new folder called"
    return dict(res, summary=f"{verb} {os.path.basename(res['path'])} in {where}.")


def rename(path: str, new_name: str):
    res = ops.rename(path, new_name)
    return dict(res, summary=f"Renamed {os.path.basename(res['old'])} to {os.path.basename(res['path'])}.")


def open_path(path: str):
    res = ops.open_path(path)
    return dict(res, summary=f"Opened {os.path.basename(path) or path}.")


def show_in_explorer(path: str):
    res = ops.open_in_explorer(path, select=True)
    return dict(res, summary=f"Here's {os.path.basename(path) or path} in File Explorer.")


def copy_path(path: str):
    from modules.desktop.clipboard import write_text
    write_text(path)
    return {"path": path, "summary": f"Copied {path}."}


def disk_cleanup(drive: str = "C"):
    import subprocess
    letter = (drive or "C").strip().rstrip(":\\")[:1].upper()
    subprocess.Popen(["cleanmgr", f"/d{letter}"])
    return {"opened": f"Disk Cleanup for {letter}:"}


def register_file_tools(registry):
    tools = [
        Tool("files.drive_overview", "How much space is used and free on each drive", {},
             PermissionLevel.LOW, drive_overview,
             parameters={"drive": P("string", "one drive letter, or blank for all", required=False, default="")},
             llm_exposed=True, category="files"),
        Tool("files.biggest", "What's using the most space on a drive or in a folder (scans in the background)",
             {"target": "string"}, PermissionLevel.LOW, biggest,
             parameters={"target": P("string", "drive letter like 'D' or a folder like 'downloads'",
                                     required=False, default=""),
                         "refresh": P("boolean", required=False, default=False)},
             llm_exposed=True, category="files"),
        Tool("files.junk_report", "Find things that can be cleaned up (duplicates, extracted archives, caches, "
             "old recordings) in Downloads, on a drive, or everywhere", {"scope": "string"},
             PermissionLevel.LOW, junk_report,
             parameters={"scope": P("string", "'downloads', a drive letter, or 'all'", required=False,
                                    default="downloads")}, llm_exposed=True, category="files"),
        Tool("files.cleanup_plan", "Propose a cleanup and ask before moving anything to the Recycle Bin",
             {"scope": "string"}, PermissionLevel.LOW, cleanup_plan,
             parameters={"scope": P("string", required=False, default="downloads"),
                         "only": P("string", "comma-separated categories", required=False, default="")},
             category="files"),
        Tool("files.find", "Find your games, emulators or ROMs across all drives", {"kind": "string"},
             PermissionLevel.LOW, find,
             parameters={"kind": P("string", enum=["games", "emulators", "roms"])}, llm_exposed=True,
             category="files"),
        Tool("files.open_folder", "Open a folder in File Explorer (downloads, documents, games, a drive...)",
             {"name": "string"}, PermissionLevel.LOW, open_folder, parameters={"name": P("string")},
             llm_exposed=True, category="files"),
        Tool("files.extract", "Extract an archive (.rar/.zip/.7z) with WinRAR into a folder",
             {"archive": "string"}, PermissionLevel.MEDIUM, extract,
             parameters={"archive": P("string", "'latest' or a name", required=False, default="latest"),
                         "source": P("string", "folder holding it", required=False, default="downloads"),
                         "destination": P("string", "folder to extract into", required=False, default=""),
                         "delete_after": P("boolean", "offer to recycle the archive afterwards", required=False,
                                           default=False)},
             category="files"),
        Tool("files.compress", "Compress a folder into a .zip or .rar", {"folder": "string"},
             PermissionLevel.MEDIUM, compress,
             parameters={"folder": P("string"), "fmt": P("string", required=False, default="zip",
                                                         enum=["zip", "rar"])}, category="files"),
        Tool("files.disk_cleanup", "Open Windows Disk Cleanup for a drive", {"drive": "string"},
             PermissionLevel.LOW, disk_cleanup, parameters={"drive": P("string", required=False, default="C")},
             llm_exposed=True, category="files"),
        # Always ask first (ALWAYS_CONFIRM) and never offered to the LLM.
        Tool("files.make_folder", "Make a new folder (in a named folder such as games, downloads or a drive)",
             {"name": "string"}, PermissionLevel.MEDIUM, make_folder,
             parameters={"name": P("string", "the new folder's name"),
                         "parent": P("string", "where: games, downloads, documents, D drive, or a path",
                                     required=False)},
             category="files", llm_exposed=True),
        Tool("files.open_path", "Open a file with its usual app", {"path": "string"}, PermissionLevel.MEDIUM,
             open_path, parameters={"path": P("string")}, category="files", llm_exposed=False),
        Tool("files.show_in_explorer", "Show a file or folder in File Explorer", {"path": "string"},
             PermissionLevel.LOW, show_in_explorer, parameters={"path": P("string")}, category="files",
             llm_exposed=False),
        Tool("files.copy_path", "Copy a file or folder's path to the clipboard", {"path": "string"},
             PermissionLevel.LOW, copy_path, parameters={"path": P("string")}, category="files", llm_exposed=False),
        Tool("files.rename", "Rename a file or folder", {"path": "string"}, PermissionLevel.MEDIUM, rename,
             parameters={"path": P("string"), "new_name": P("string")}, category="files", llm_exposed=False),
        Tool("files.recycle", "Move files or folders to the Recycle Bin (never deletes permanently)",
             {"paths": "array"}, PermissionLevel.HIGH, recycle,
             parameters={"paths": P("array", items={"type": "string"})}, category="files"),
        Tool("files.move", "Move a file or folder to another folder or drive", {"source": "string"},
             PermissionLevel.HIGH, move,
             parameters={"source": P("string"), "destination": P("string")}, category="files"),
    ]
    for t in tools:
        registry.register(t)

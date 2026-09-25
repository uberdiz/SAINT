"""
modules/files/ops.py

Moving files and folders, and sending them to the Recycle Bin — always
through Windows' own file operation (the same one Explorer uses), so every
action can be undone from the Recycle Bin or with Ctrl+Z in Explorer.

Nothing here deletes permanently:
* Before recycling, each item is checked against that drive's Recycle Bin:
  if the bin is set to delete immediately, or the item is bigger than the
  bin can hold, Windows would destroy it instead — SAINT refuses and says so.
* FOF_WANTNUKEWARNING stays on as a second guard: if Windows would still
  delete something outright, it asks on screen first.
* Windows, programs, drive roots, your profile folder and code repositories
  are refused outright (paths.denied).
"""

import logging
import os
import shutil
from typing import Dict, List, Optional

from modules.automation.tools import ToolError
from modules.files.paths import denied, protected

log = logging.getLogger("saint.files")

# SHFileOperation flags
FO_MOVE, FO_DELETE = 0x0001, 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMMKDIR = 0x0200
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000


def item_size(path: str) -> int:
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    from modules.files.scan import scan_tree
    return scan_tree(path, depth=0)["total"]


# ---------------------------------------------------------------------- #
# Recycle Bin capacity (per drive)
# ---------------------------------------------------------------------- #
def bin_settings(path: str) -> Dict:
    """{"nuke": bool, "max_bytes": int|None} for the drive holding ``path``."""
    drive = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    out = {"nuke": False, "max_bytes": None}
    try:
        import win32file
        import winreg
        vol = win32file.GetVolumeNameForVolumeMountPoint(drive)          # \\?\Volume{guid}\
        guid = vol[vol.index("{"):vol.index("}") + 1]
        key = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume\{guid}"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            try:
                out["nuke"] = bool(winreg.QueryValueEx(k, "NukeOnDelete")[0])
            except OSError:
                pass
            try:
                out["max_bytes"] = int(winreg.QueryValueEx(k, "MaxCapacity")[0]) * 1024 * 1024
            except OSError:
                pass
    except Exception as e:
        log.debug("files.bin_settings_unavailable %s %s", drive, e)
    if out["max_bytes"] is None:
        try:
            import psutil
            # Windows' default bin size is about 5% of the drive (at most ~50 GB on large drives).
            out["max_bytes"] = min(int(psutil.disk_usage(drive).total * 0.05), 50 * 1024 ** 3)
        except Exception:
            out["max_bytes"] = 0
    return out


def _check_recyclable(path: str, size: int, allow_protected: bool = False) -> Optional[str]:
    if not os.path.exists(path):
        return "it's already gone"
    why = denied(path)
    if why:
        return why
    if not allow_protected:
        why = protected(path)
        if why:
            return f"I won't remove it because {why}"
    b = bin_settings(path)
    if b["nuke"]:
        return (f"the Recycle Bin on {os.path.splitdrive(path)[0]} is set to delete files immediately, so it "
                f"couldn't be undone")
    if b["max_bytes"] and size > b["max_bytes"]:
        from modules.files.scan import human
        return (f"at {human(size)} it's bigger than the {os.path.splitdrive(path)[0]} Recycle Bin can hold "
                f"({human(b['max_bytes'])}), so Windows would delete it permanently")
    return None


def _shfileop(func: int, sources: List[str], dest: str, flags: int):
    from win32com.shell import shell
    res = shell.SHFileOperation((0, func, "\0".join(sources), dest or None, flags, None, None))
    code = res[0] if isinstance(res, tuple) else res
    aborted = bool(res[1]) if isinstance(res, tuple) and len(res) > 1 else False
    return code, aborted


def recycle(paths: List[str], allow_protected: bool = False) -> Dict:
    """Send ``paths`` to the Recycle Bin. Items that can't safely go there are
    skipped with the reason; nothing is ever deleted permanently."""
    ok_items, refused = [], []
    for p in paths:
        p = os.path.normpath(p)
        size = item_size(p) if os.path.exists(p) else 0
        why = _check_recyclable(p, size, allow_protected)
        if why:
            refused.append({"path": p, "reason": why})
        else:
            ok_items.append((p, size))
    recycled, failed = [], []
    if ok_items:
        flags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING
        code, aborted = _shfileop(FO_DELETE, [p for p, _ in ok_items], "", flags)
        for p, size in ok_items:
            if os.path.exists(p):
                failed.append({"path": p, "reason": "Windows couldn't move it (it may be open in another app)"
                               if not aborted else "cancelled"})
            else:
                recycled.append({"path": p, "size": size})
        if code and not recycled:
            log.info("files.recycle_failed code=%s", code)
    return {"recycled": recycled, "freed": sum(r["size"] for r in recycled), "refused": refused, "failed": failed}


def move(src: str, dest_dir: str) -> Dict:
    """Move a file or folder into ``dest_dir`` (undoable, like dragging in Explorer)."""
    src, dest_dir = os.path.normpath(src), os.path.normpath(dest_dir)
    if not os.path.exists(src):
        raise ToolError(f"I can't find {src}.", "NOT_FOUND")
    why = denied(src)
    if why:
        raise ToolError(f"I won't move that: {why}", "DENIED")
    from modules.files.paths import steam_library_roots, _under
    for lib in steam_library_roots():
        if _under(src, lib):
            raise ToolError("That's a Steam game — moving its folder breaks it. Use Steam > Settings > Storage "
                            "> Move to move it to another drive.", "USE_STEAM")
    if not os.path.isdir(dest_dir):
        raise ToolError(f"{dest_dir} doesn't exist.", "NOT_FOUND")
    target = os.path.join(dest_dir, os.path.basename(src))
    if os.path.exists(target):
        raise ToolError(f"There's already a {os.path.basename(src)} in {dest_dir}; I won't overwrite it.", "EXISTS")
    if os.path.splitdrive(src)[0].lower() == os.path.splitdrive(dest_dir)[0].lower() and \
            os.path.abspath(dest_dir).lower().startswith(os.path.abspath(src).lower() + os.sep):
        raise ToolError("I can't move a folder into itself.", "INVALID")
    size = item_size(src)
    if os.path.splitdrive(src)[0].lower() != os.path.splitdrive(dest_dir)[0].lower():
        free = shutil.disk_usage(dest_dir).free
        if size > free * 0.98:
            from modules.files.scan import human
            raise ToolError(f"{os.path.basename(src)} is {human(size)} but {dest_dir[:2]} only has "
                            f"{human(free)} free.", "NO_SPACE")
    code, aborted = _shfileop(FO_MOVE, [src], dest_dir, FOF_ALLOWUNDO | FOF_NOCONFIRMMKDIR | FOF_NOERRORUI)
    if aborted:
        raise ToolError("The move was cancelled.", "CANCELLED")
    if not os.path.exists(target):
        raise ToolError(f"Windows couldn't move {os.path.basename(src)} (code {code}); something in it may be open.",
                        "MOVE_FAILED")
    return {"moved": src, "to": target, "size": size}


# ---------------------------------------------------------------------- #
# Explorer
# ---------------------------------------------------------------------- #
def open_in_explorer(path: str, select: bool = False) -> Dict:
    import subprocess
    path = os.path.normpath(path)
    if not os.path.exists(path):
        raise ToolError(f"{path} doesn't exist.", "NOT_FOUND")
    if select or os.path.isfile(path):
        subprocess.Popen(["explorer", "/select,", path])
    else:
        os.startfile(path)
    return {"opened": path}


def explorer_selection() -> Dict:
    """The folder shown in the front-most File Explorer window and what's
    selected in it ({"folder": str|None, "selected": [paths]})."""
    import pythoncom
    pythoncom.CoInitialize()
    try:
        import win32com.client
        import win32gui
        fg = win32gui.GetForegroundWindow()
        shell = win32com.client.Dispatch("Shell.Application")
        best = None
        for w in shell.Windows():
            try:
                if not str(w.FullName).lower().endswith("explorer.exe"):
                    continue
                doc = w.Document
                folder = doc.Folder.Self.Path
                sel = [it.Path for it in doc.SelectedItems()]
                entry = {"folder": folder, "selected": sel, "hwnd": int(w.HWND)}
                if int(w.HWND) == fg:
                    return entry
                best = best or entry
            except Exception:
                continue
        return best or {"folder": None, "selected": []}
    finally:
        pythoncom.CoUninitialize()

"""
modules/files/scan.py

How full each drive is, and what's using the space.

``scan_tree`` walks a folder or whole drive once with os.scandir (fast: the
sizes come with the directory listing), totalling every folder down to
``depth`` levels. It never follows links/junctions (no double counting, no
loops) and counts OneDrive / cloud "online-only" files as 0 bytes because
they take no space. A drive scan runs in the background and is cached in
data/storage_cache.json (files.scan_cache_hours).
"""

import json
import os
import stat
import threading
import time
from typing import Callable, Dict, List, Optional

from core.config import config
from core.paths import data_path

_REPARSE = 0x400                      # FILE_ATTRIBUTE_REPARSE_POINT (junctions, symlinks, some cloud files)
_ONLINE_ONLY = 0x400000 | 0x40000 | 0x1000   # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE

_lock = threading.Lock()


def drive_overview() -> List[Dict]:
    from modules.files.paths import fixed_drives
    import psutil
    out = []
    for root in fixed_drives():
        try:
            u = psutil.disk_usage(root)
        except OSError:
            continue
        out.append({"drive": root, "letter": root[0].upper(), "total": u.total, "used": u.used, "free": u.free,
                    "percent": round(u.percent, 1), "label": _volume_label(root)})
    return out


def _volume_label(root: str) -> str:
    try:
        import win32api
        return win32api.GetVolumeInformation(root)[0] or ""
    except Exception:
        return ""


def _attrs(st) -> int:
    return getattr(st, "st_file_attributes", 0) or 0


def scan_tree(root: str, depth: int = 3, progress: Optional[Callable] = None,
              cancel: Optional[threading.Event] = None) -> Dict:
    """Total size of ``root`` and of every folder down to ``depth`` levels.

    Returns {"root", "total", "files", "folders": [{"path", "size", "depth"}] (largest first),
             "skipped": n unreadable folders, "elapsed"}."""
    root = os.path.normpath(root)
    t0 = time.time()
    expected = 0
    try:
        import psutil
        if os.path.splitdrive(root)[1].strip("\\/") == "":
            expected = psutil.disk_usage(root).used
    except Exception:
        pass
    shared = {"total": 0, "last": 0.0}
    lock = threading.Lock()

    def report(added: int):
        if not progress:
            return
        with lock:
            shared["total"] += added
            now = time.time()
            if now - shared["last"] < 0.5:
                return
            shared["last"] = now
            done = shared["total"]
        progress(min(0.99, done / expected) if expected else 0.0, f"{done / 1e9:.1f} GB counted")

    # The top level is listed here; each top-level folder is walked on its own
    # thread (os.scandir releases the GIL, so a fast SSD is read in parallel).
    sizes: Dict[str, int] = {}
    total = files = skipped = 0
    subdirs = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                a = _attrs(st)
                if a & _REPARSE or stat.S_ISLNK(st.st_mode):
                    continue
                if stat.S_ISDIR(st.st_mode):
                    subdirs.append(entry.path)
                    continue
                size = 0 if a & _ONLINE_ONLY else st.st_size
                total += size
                files += 1
    except OSError:
        skipped += 1
    sizes[root] = total
    if subdirs:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(subdirs))) as pool:
            parts = list(pool.map(lambda p: _walk(p, 1, (root,), depth, cancel, report), subdirs))
        for part_sizes, part_total, part_files, part_skipped in parts:
            for k, v in part_sizes.items():
                sizes[k] = sizes.get(k, 0) + v
            total += part_total
            files += part_files
            skipped += part_skipped
    folders = [{"path": p, "size": s, "depth": _depth(root, p)} for p, s in sizes.items() if p != root]
    folders.sort(key=lambda f: f["size"], reverse=True)
    return {"root": root, "total": total, "files": files, "folders": folders, "skipped": skipped,
            "elapsed": round(time.time() - t0, 1), "complete": not (cancel is not None and cancel.is_set())}


def _walk(start: str, start_depth: int, ancestors: tuple, depth: int, cancel, report):
    """Iterative walk of one subtree. Sizes are added to every folder in the
    chain down to ``depth`` (the root's share comes back in ``ancestors``)."""
    sizes: Dict[str, int] = {}
    total = files = skipped = 0
    stack = [(start, start_depth, ancestors)]
    pending = 0
    while stack:
        if cancel is not None and cancel.is_set():
            break
        path, d, anc = stack.pop()
        chain = anc + ((path,) if d <= depth else ())
        try:
            it = os.scandir(path)
        except OSError:
            skipped += 1
            continue
        with it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                a = _attrs(st)
                if a & _REPARSE or stat.S_ISLNK(st.st_mode):
                    continue
                if stat.S_ISDIR(st.st_mode):
                    stack.append((entry.path, d + 1, chain))
                    continue
                size = 0 if a & _ONLINE_ONLY else st.st_size
                total += size
                pending += size
                files += 1
                for c in chain:
                    sizes[c] = sizes.get(c, 0) + size
        if pending > 200e6:
            report(pending)
            pending = 0
    report(pending)
    return sizes, total, files, skipped


def _depth(root: str, path: str) -> int:
    rel = os.path.relpath(path, root)
    return 0 if rel == "." else rel.count(os.sep) + 1


def top_folders(scan: Dict, n: int = 10, max_depth: int = 2, min_share: float = 0.0) -> List[Dict]:
    """The biggest folders without listing a folder *and* most of its
    contents: a child is shown instead of its parent when it holds most of it."""
    out: List[Dict] = []
    for f in scan.get("folders", []):
        if f["depth"] > max_depth or f["size"] < min_share * max(1, scan.get("total", 1)):
            continue
        parent = next((o for o in out if f["path"].startswith(o["path"].rstrip("\\") + "\\")), None)
        if parent and f["size"] < 0.6 * parent["size"]:
            continue
        if parent:
            out.remove(parent)
        out.append(f)
        if len(out) >= n:
            break
    return sorted(out, key=lambda f: f["size"], reverse=True)[:n]


# ---------------------------------------------------------------------- #
# Cache
# ---------------------------------------------------------------------- #
def _cache_path():
    return data_path("storage_cache.json")


def cached(root: str) -> Optional[Dict]:
    hours = float(config.get("files.scan_cache_hours", 24) or 24)
    with _lock:
        try:
            with open(_cache_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
    entry = data.get(os.path.normcase(os.path.normpath(root)))
    if entry and time.time() - entry.get("at", 0) < hours * 3600:
        return entry
    return None


def remember(scan: Dict):
    """Keep the summary (top 200 folders) of a finished scan."""
    if not scan.get("complete"):
        return
    entry = dict(scan, folders=scan["folders"][:200], at=time.time())
    with _lock:
        try:
            with open(_cache_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data[os.path.normcase(os.path.normpath(scan["root"]))] = entry
        tmp = str(_cache_path()) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _cache_path())


def human(n: float) -> str:
    n = float(n or 0)
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= size:
            v = n / size
            return f"{v:.0f} {unit}" if v >= 100 else f"{v:.1f} {unit}"
    return f"{int(n)} bytes"

"""
modules/files/junk.py

What could go, and what should stay.

Each finding: {"category", "path", "size", "reason", "action"} where action is
    "recycle"   safe to move to the Recycle Bin (rebuilt automatically, or a duplicate)
    "cleanmgr"  Windows owns it — use Disk Cleanup (SAINT opens it for you)
    "review"    worth a look, but only you can decide (old recordings, old installs)

Games, emulators, ROMs and Steam libraries are never suggested
(paths.protected), and nothing under Windows / Program Files ever is
(paths.denied).
"""

import hashlib
import os
import re
import time
from typing import Callable, Dict, List, Optional

from core.config import config
from modules.files.archives import is_archive, listing
from modules.files.paths import denied, fixed_drives, old_windows_candidates, protected

_DUP = re.compile(r"^(?P<stem>.+?)(?: \((?P<n>\d+)\)| - Copy(?: \(\d+\))?)(?P<ext>\.[^.]+)$", re.I)
_INSTALLER = (".exe", ".msi", ".msix", ".appx", ".appinstaller")
CATEGORY_NAMES = {"temp": "old temp files", "shader_cache": "graphics shader caches",
                  "duplicate": "duplicate downloads", "extracted": "archives you've already extracted",
                  "old_installer": "old installers", "recordings": "old game recordings",
                  "windows_update": "Windows Update leftovers", "old_windows": "an old Windows installation",
                  "browser_cache": "browser caches", "app_cache": "app caches", "crash_dumps": "crash dumps",
                  "dev_cache": "developer caches", "big_download": "big old downloads",
                  "recycle_bin": "files already in the Recycle Bin"}

# App caches that rebuild themselves: (folder under LOCALAPPDATA / APPDATA, what it is, category).
# Browsers and apps that are open keep some files locked; those are simply skipped.
_APP_CACHES = [
    ("LOCALAPPDATA", r"Google\Chrome\User Data\*\Cache", "Chrome's cache", "browser_cache"),
    ("LOCALAPPDATA", r"Google\Chrome\User Data\*\Code Cache", "Chrome's code cache", "browser_cache"),
    ("LOCALAPPDATA", r"Microsoft\Edge\User Data\*\Cache", "Edge's cache", "browser_cache"),
    ("LOCALAPPDATA", r"Microsoft\Edge\User Data\*\Code Cache", "Edge's code cache", "browser_cache"),
    ("LOCALAPPDATA", r"BraveSoftware\Brave-Browser\User Data\*\Cache", "Brave's cache", "browser_cache"),
    ("LOCALAPPDATA", r"Opera Software\Opera Stable\Cache", "Opera's cache", "browser_cache"),
    ("LOCALAPPDATA", r"Opera Software\Opera GX Stable\Cache", "Opera GX's cache", "browser_cache"),
    ("LOCALAPPDATA", r"Mozilla\Firefox\Profiles\*\cache2", "Firefox's cache", "browser_cache"),
    ("APPDATA", r"discord\Cache", "Discord's cache", "app_cache"),
    ("APPDATA", r"discord\Code Cache", "Discord's code cache", "app_cache"),
    ("LOCALAPPDATA", r"Spotify\Data", "Spotify's streaming cache", "app_cache"),
    ("APPDATA", r"Code\Cache", "VS Code's cache", "app_cache"),
    ("APPDATA", r"Code\CachedData", "VS Code's cached data", "app_cache"),
    ("LOCALAPPDATA", r"CrashDumps", "crash dumps from apps that crashed", "crash_dumps"),
    ("LOCALAPPDATA", r"pip\cache", "pip's download cache — re-downloaded when needed", "dev_cache"),
    ("LOCALAPPDATA", r"uv\cache", "uv's package cache — re-downloaded when needed", "dev_cache"),
    ("LOCALAPPDATA", r"npm-cache", "npm's cache — re-downloaded when needed", "dev_cache"),
    ("APPDATA", r"npm-cache", "npm's cache — re-downloaded when needed", "dev_cache"),
    ("LOCALAPPDATA", r"Yarn\Cache", "Yarn's cache", "dev_cache"),
]


def recycle_bin_size() -> int:
    """Bytes already sitting in the Recycle Bin (all drives)."""
    try:
        import ctypes

        class _Info(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_longlong),
                        ("i64NumItems", ctypes.c_longlong)]
        info = _Info()
        info.cbSize = ctypes.sizeof(_Info)
        if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0:
            return int(info.i64Size)
    except Exception:
        pass
    return 0


def _size(path: str) -> int:
    from modules.files.ops import item_size
    try:
        return item_size(path)
    except OSError:
        return 0


def _age_days(path: str) -> float:
    try:
        return (time.time() - os.path.getmtime(path)) / 86400
    except OSError:
        return 0.0


def _ok(path: str) -> bool:
    return not denied(path) and not protected(path)


def _quick_hash(path: str) -> str:
    """First + last MiB — enough to tell identical downloads apart."""
    h = hashlib.sha1()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            h.update(f.read(1 << 20))
            if size > 2 << 20:
                f.seek(-(1 << 20), os.SEEK_END)
                h.update(f.read(1 << 20))
        h.update(str(size).encode())
    except OSError:
        return ""
    return h.hexdigest()


# ---------------------------------------------------------------------- #
# Downloads
# ---------------------------------------------------------------------- #
def downloads_findings(folder: str) -> List[Dict]:
    out: List[Dict] = []
    try:
        entries = [e for e in os.scandir(folder)]
    except OSError:
        return out
    files = [e for e in entries if e.is_file(follow_symlinks=False)]
    names = {e.name.lower(): e for e in entries}

    # "setup (1).exe" next to an identical "setup.exe"
    for e in files:
        m = _DUP.match(e.name)
        if not m:
            continue
        original = names.get((m.group("stem") + m.group("ext")).lower())
        if original is not None and original.is_file() and \
                _quick_hash(original.path) and _quick_hash(original.path) == _quick_hash(e.path):
            out.append({"category": "duplicate", "path": e.path, "size": e.stat().st_size,
                        "reason": f"an identical copy of {original.name}", "action": "recycle"})

    # archives whose contents are already extracted (next to them or in the games folder)
    games_dir = config.get("files.games_dir", "") or ""
    for e in files:
        if not is_archive(e.name) or any(f["path"] == e.path for f in out):
            continue
        stem = re.sub(r"\.part\d+$", "", os.path.splitext(e.name)[0], flags=re.I)
        try:
            info = listing(e.path)
        except Exception:
            continue
        for parent in [folder] + ([games_dir] if games_dir else []):
            for cand in {os.path.join(parent, stem)} | {os.path.join(parent, t) for t in info["top"][:1]}:
                if os.path.isdir(cand) and _extracted_into(info, cand, parent):
                    out.append({"category": "extracted", "path": e.path, "size": e.stat().st_size,
                                "reason": f"already extracted to {cand}", "action": "recycle"})
                    break
            else:
                continue
            break

    # installers you ran a while ago
    for e in files:
        if e.name.lower().endswith(_INSTALLER) and _age_days(e.path) > 30 and \
                not any(f["path"] == e.path for f in out):
            out.append({"category": "old_installer", "path": e.path, "size": e.stat().st_size,
                        "reason": f"an installer downloaded {int(_age_days(e.path))} days ago", "action": "review"})
    # big files you haven't touched in months
    for e in files:
        try:
            size = e.stat().st_size
        except OSError:
            continue
        if size > 500e6 and _age_days(e.path) > 60 and not any(f["path"] == e.path for f in out):
            out.append({"category": "big_download", "path": e.path, "size": size,
                        "reason": f"downloaded {int(_age_days(e.path))} days ago and not touched since",
                        "action": "review"})
    return [f for f in out if _ok(f["path"])]


def _extracted_into(info: Dict, folder: str, parent: str) -> bool:
    """Are (nearly) all of the archive's files already on disk?"""
    files = [e for e in info["entries"] if not e["dir"]]
    if not files:
        return False
    # Paths inside the archive may or may not include the top folder.
    present = 0
    for ent in files[:400]:
        rel = ent["name"]
        if os.path.exists(os.path.join(parent, rel)) or os.path.exists(os.path.join(folder, rel)):
            present += 1
    return present >= 0.9 * min(len(files), 400)


# ---------------------------------------------------------------------- #
# Caches, temp, Windows leftovers, recordings
# ---------------------------------------------------------------------- #
def cache_findings() -> List[Dict]:
    out = []
    local = os.environ.get("LOCALAPPDATA", "")
    temp = os.environ.get("TEMP", "")
    age = float(config.get("files.temp_age_days", 2) or 2)
    if temp and os.path.isdir(temp):
        try:
            for e in os.scandir(temp):
                if _age_days(e.path) > age and _ok(e.path):
                    size = _size(e.path)
                    if size > 0:
                        out.append({"category": "temp", "path": e.path, "size": size,
                                    "reason": f"temporary, untouched for {int(_age_days(e.path))} days",
                                    "action": "recycle"})
        except OSError:
            pass
    for sub, what in ((r"NVIDIA\DXCache", "NVIDIA shader cache"), (r"NVIDIA\GLCache", "NVIDIA OpenGL cache"),
                      (r"D3DSCache", "DirectX shader cache"), (r"AMD\DxCache", "AMD shader cache"),
                      (r"AMD\DxcCache", "AMD shader cache")):
        p = os.path.join(local, sub) if local else ""
        if p and os.path.isdir(p) and _ok(p):
            size = _size(p)
            if size > 50e6:
                out.append({"category": "shader_cache", "path": p, "size": size,
                            "reason": f"{what} — rebuilt automatically (games may stutter briefly the first time)",
                            "action": "recycle"})
    import glob
    for env, pattern, what, cat in _APP_CACHES:
        base = os.environ.get(env, "")
        if not base:
            continue
        for p in glob.glob(os.path.join(base, pattern)):
            if not os.path.isdir(p) or not _ok(p):
                continue
            size = _size(p)
            if size > (100e6 if cat == "dev_cache" else 30e6):
                # Dev caches cost a re-download: shown, but only cleared when you pick them.
                out.append({"category": cat, "path": p, "size": size,
                            "reason": what if cat in ("dev_cache", "crash_dumps") else f"{what} — rebuilt automatically",
                            "action": "review" if cat == "dev_cache" else "recycle"})
    bin_size = recycle_bin_size()
    if bin_size > 50e6:
        out.append({"category": "recycle_bin", "path": "shell:RecycleBinFolder", "size": bin_size,
                    "reason": "already deleted — empty the Recycle Bin yourself to get this space back",
                    "action": "info"})
    for root in fixed_drives():
        for sub in ("WUDownloadCache", r"Windows\SoftwareDistribution\Download"):
            p = os.path.join(root, sub)
            if os.path.isdir(p):
                size = _size(p)
                if size > 100e6:
                    out.append({"category": "windows_update", "path": p, "size": size,
                                "reason": "Windows Update downloads — Disk Cleanup removes these safely",
                                "action": "cleanmgr"})
    return out


def recordings_findings(roots: Optional[List[str]] = None) -> List[Dict]:
    """Clips folders (Medal, NVIDIA, Xbox Game Bar) — old recordings are listed, never removed automatically."""
    age = float(config.get("files.recordings_age_days", 60) or 60)
    out = []
    candidates = []
    for root in roots or fixed_drives():
        for sub in ("Medal", "Clips", r"Medal\Clips"):
            candidates.append(os.path.join(root, sub))
    home = os.path.expanduser("~")
    candidates += [os.path.join(home, "Videos", "Captures"), os.path.join(home, "Videos", "Medal"),
                   os.path.join(home, "Videos", "NVIDIA")]
    seen = set()
    for folder in candidates:
        if not os.path.isdir(folder) or os.path.normcase(folder) in seen:
            continue
        seen.add(os.path.normcase(folder))
        old_size, count = 0, 0
        for dirpath, _dirs, files in os.walk(folder):
            for name in files:
                if name.lower().endswith((".mp4", ".mkv", ".mov", ".webm", ".avi")):
                    fp = os.path.join(dirpath, name)
                    if _age_days(fp) > age:
                        try:
                            old_size += os.path.getsize(fp)
                            count += 1
                        except OSError:
                            pass
        if old_size > 200e6:
            out.append({"category": "recordings", "path": folder, "size": old_size,
                        "reason": f"{count} recordings older than {int(age)} days", "action": "review"})
    return out


def old_windows_findings(progress: Optional[Callable] = None, cancel=None) -> List[Dict]:
    out = []
    for cand in old_windows_candidates():
        from modules.files.scan import scan_tree
        size = 0
        for p in cand["paths"]:
            if cancel is not None and cancel.is_set():
                break
            size += scan_tree(p, depth=0, cancel=cancel)["total"]
        out.append({"category": "old_windows", "path": cand["drive"], "size": size,
                    "reason": "an old Windows installation (" + ", ".join(os.path.basename(p.rstrip("\\"))
                                                                          for p in cand["paths"]) + ")",
                    "action": "review", "paths": cand["paths"]})
    return out


def report(scope: str = "all", downloads: Optional[str] = None, progress: Optional[Callable] = None,
           cancel=None) -> Dict:
    """scope: "downloads" | a drive root like "E:\\" | "all"."""
    findings: List[Dict] = []
    if scope in ("downloads", "all") and downloads:
        if progress:
            progress(0.05, "checking Downloads")
        findings += downloads_findings(downloads)
    drive = scope if re.match(r"^[A-Z]:\\$", scope or "", re.I) else None
    if scope == "all" or drive:
        if progress:
            progress(0.3, "checking caches")
        findings += [f for f in cache_findings() if not drive or f["path"].upper().startswith(drive.upper())
                     or f["category"] == "temp" and drive.upper().startswith("C")]
        if progress:
            progress(0.5, "checking recordings")
        findings += recordings_findings([drive] if drive else None)
        if progress:
            progress(0.7, "checking for old Windows installs")
        findings += [f for f in old_windows_findings(cancel=cancel)
                     if not drive or f["path"].upper() == drive.upper()]
    findings.sort(key=lambda f: f["size"], reverse=True)
    for i, f in enumerate(findings, 1):
        f["id"] = i
    return {"scope": scope, "findings": findings,
            "recyclable": sum(f["size"] for f in findings if f["action"] == "recycle"),
            "total": sum(f["size"] for f in findings)}


def summarize(rep: Dict) -> str:
    from modules.files.scan import human
    f = rep["findings"]
    if not f:
        return "I didn't find anything that's safe to clear there."
    by_cat: Dict[str, List[Dict]] = {}
    for x in f:
        by_cat.setdefault(x["category"], []).append(x)
    parts = []
    for cat, items in sorted(by_cat.items(), key=lambda kv: -sum(i["size"] for i in kv[1])):
        size = sum(i["size"] for i in items)
        label = CATEGORY_NAMES.get(cat, cat)
        parts.append(f"{human(size)} of {label}" + (f" ({len(items)})" if len(items) > 1 else ""))
    text = "I found " + "; ".join(parts[:5]) + "."
    rec = rep["recyclable"]
    if rec:
        text += f" {human(rec)} of that can safely go to the Recycle Bin — should I move it there?"
    elif "old_installer" in by_cat:
        text += " Installers are your call — say “clean up the installers” and I'll move them to the " \
                "Recycle Bin."
    return text

"""
modules/files/archives.py

Extracting and creating archives with WinRAR (or Python's zipfile when
WinRAR isn't installed and the archive is a .zip).

    extract("C:\\...\\Downloads\\Game.rar", "D:\\Games")  -> D:\\Games\\Game\\...

* The archive goes into its own folder (``dest/<name>``) unless everything in
  it is already inside one top-level folder, so extracting never scatters
  files across your games folder.
* Free space is checked against the unpacked size first.
* Existing files are never overwritten (-o-).
* Password-protected archives are reported, not guessed at (-p-).
* Progress comes from UnRAR's own output; cancelling kills the extractor.
"""

import logging
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from typing import Callable, Dict, List, Optional

from modules.automation.tools import ToolError

log = logging.getLogger("saint.files")

ARCHIVE_EXTS = (".rar", ".zip", ".7z")
_PARTIAL = (".crdownload", ".part", ".tmp", ".download", ".opdownload", ".partial")
_NO_WINDOW = 0x08000000                       # CREATE_NO_WINDOW


def find_winrar() -> Dict[str, Optional[str]]:
    """Paths of WinRAR.exe / UnRAR.exe / Rar.exe (None when missing)."""
    dirs = []
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for key in (r"SOFTWARE\WinRAR", r"SOFTWARE\WOW6432Node\WinRAR"):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        for val in ("exe64", "exe32"):
                            try:
                                dirs.append(os.path.dirname(winreg.QueryValueEx(k, val)[0]))
                            except OSError:
                                pass
                except OSError:
                    continue
    except ImportError:
        pass
    dirs += [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "WinRAR"),
             os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "WinRAR")]
    out = {"winrar": None, "unrar": None, "rar": None}
    for d in dirs:
        for key, exe in (("winrar", "WinRAR.exe"), ("unrar", "UnRAR.exe"), ("rar", "Rar.exe")):
            p = os.path.join(d, exe)
            if out[key] is None and os.path.isfile(p):
                out[key] = p
    return out


def is_archive(path: str) -> bool:
    low = path.lower()
    if not low.endswith(ARCHIVE_EXTS):
        return False
    m = re.search(r"\.part(\d+)\.rar$", low)
    return not (m and int(m.group(1)) > 1)          # only the first part of a split archive


def newest_archive(folder: str) -> Optional[str]:
    """The most recently downloaded archive in ``folder`` (finished downloads only)."""
    best, best_t = None, 0.0
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return None
    for e in entries:
        if not e.is_file() or e.name.lower().endswith(_PARTIAL) or not is_archive(e.name):
            continue
        try:
            t = max(e.stat().st_mtime, e.stat().st_ctime)
        except OSError:
            continue
        if t > best_t:
            best, best_t = e.path, t
    return best


def find_archive(folder: str, name: str) -> Optional[str]:
    """An archive in ``folder`` whose name contains the spoken words."""
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in ("the", "a", "file", "archive")]
    if not words:
        return None
    hits = []
    try:
        for e in os.scandir(folder):
            if e.is_file() and is_archive(e.name) and all(w in re.sub(r"[^a-z0-9]+", " ", e.name.lower())
                                                          for w in words):
                hits.append((e.stat().st_mtime, e.path))
    except OSError:
        return None
    return max(hits)[1] if hits else None


# ---------------------------------------------------------------------- #
# Listing
# ---------------------------------------------------------------------- #
_LIST_ROW = re.compile(r"^\s*(\S+)\s+(\d+)\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+(.+?)\s*$")


def parse_unrar_listing(text: str) -> List[Dict]:
    """Rows of ``UnRAR l`` output: [{"name", "size", "dir"}]."""
    rows, inside = [], False
    for line in text.splitlines():
        if line.startswith("-----------"):
            if inside:
                break
            inside = True
            continue
        if inside:
            m = _LIST_ROW.match(line)
            if m:
                rows.append({"name": m.group(3), "size": int(m.group(2)), "dir": "D" in m.group(1)})
    return rows


def listing(archive: str) -> Dict:
    """{"entries": [{"name","size","dir"}], "size": unpacked bytes, "top": top-level names}
    (``entries`` is empty when the format can't be listed, e.g. .7z)."""
    low = archive.lower()
    entries: List[Dict] = []
    if low.endswith(".zip"):
        try:
            with zipfile.ZipFile(archive) as z:
                entries = [{"name": i.filename.replace("/", "\\").rstrip("\\"), "size": i.file_size,
                            "dir": i.is_dir()} for i in z.infolist()]
        except zipfile.BadZipFile:
            raise ToolError(f"{os.path.basename(archive)} isn't a valid zip file (the download may be incomplete).",
                            "BAD_ARCHIVE")
    elif low.endswith(".rar"):
        unrar = find_winrar()["unrar"]
        if unrar:
            p = subprocess.run([unrar, "l", "-idc", "-p-", archive], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", creationflags=_NO_WINDOW, timeout=60)
            entries = parse_unrar_listing(p.stdout)
            if not entries and ("encrypted" in (p.stdout + p.stderr).lower() or p.returncode == 11):
                raise ToolError(f"{os.path.basename(archive)} is password protected.", "PASSWORD")
    top = sorted({e["name"].split("\\")[0] for e in entries if e["name"]})
    return {"entries": entries, "size": sum(e["size"] for e in entries if not e["dir"]), "top": top}


def destination_for(archive: str, dest_parent: str, top: List[str], entries: List[Dict]) -> str:
    """Where the files will land: ``dest_parent`` if the archive holds a single
    top-level folder, else ``dest_parent/<archive name>``."""
    stem = re.sub(r"\.part\d+$", "", os.path.splitext(os.path.basename(archive))[0], flags=re.I)
    if len(top) == 1:
        t = top[0]
        if any(e["name"] == t and e["dir"] for e in entries) or any(e["name"].startswith(t + "\\") for e in entries):
            return dest_parent               # already wrapped in one folder
    return os.path.join(dest_parent, stem)


# ---------------------------------------------------------------------- #
# Extract
# ---------------------------------------------------------------------- #
def extract(archive: str, dest_parent: str, progress: Optional[Callable] = None,
            cancel: Optional[threading.Event] = None) -> Dict:
    archive = os.path.normpath(archive)
    if not os.path.isfile(archive):
        raise ToolError(f"I can't find {archive}.", "NOT_FOUND")
    if not os.path.isdir(dest_parent):
        raise ToolError(f"The folder {dest_parent} doesn't exist.", "NOT_FOUND")
    info = listing(archive)
    dest = destination_for(archive, dest_parent, info["top"], info["entries"])
    free = shutil.disk_usage(dest_parent).free
    if info["size"] and info["size"] > free * 0.98:
        from modules.files.scan import human
        raise ToolError(f"It unpacks to {human(info['size'])} but {dest_parent[:2]} only has {human(free)} free.",
                        "NO_SPACE")
    os.makedirs(dest, exist_ok=True)
    low = archive.lower()
    tools = find_winrar()
    t0 = time.time()
    if low.endswith(".rar") and tools["unrar"]:
        _unrar(tools["unrar"], archive, dest, info, progress, cancel)
        how = "WinRAR"
    elif low.endswith(".zip") and not tools["winrar"]:
        _unzip(archive, dest, info, progress, cancel)
        how = "Windows"
    elif tools["winrar"]:
        _winrar_gui(tools["winrar"], archive, dest, cancel)
        how = "WinRAR"
    elif low.endswith(".zip"):
        _unzip(archive, dest, info, progress, cancel)
        how = "Windows"
    else:
        raise ToolError(f"I need WinRAR to open {os.path.splitext(archive)[1]} files, and it isn't installed.",
                        "NO_WINRAR")
    if cancel is not None and cancel.is_set():
        return {"archive": archive, "dest": dest, "cancelled": True,
                "summary": f"Stopped extracting {os.path.basename(archive)}; what finished is in {dest}."}
    from modules.files.scan import human
    name = os.path.basename(archive)
    # The folder the files are in: the archive's own top folder when it had one.
    folder = dest
    top = list(info["top"])
    if os.path.normpath(dest) == os.path.normpath(dest_parent) and len(top) == 1 and \
            os.path.isdir(os.path.join(dest_parent, top[0])):
        folder = os.path.join(dest_parent, top[0])
    return {"archive": archive, "dest": dest, "folder": folder, "size": info["size"],
            "files": sum(1 for e in info["entries"] if not e["dir"]),
            "seconds": round(time.time() - t0, 1), "via": how,
            "summary": f"Extracted {name} to {dest}" + (f" ({human(info['size'])})." if info["size"] else ".")}


def _unrar(unrar: str, archive: str, dest: str, info: Dict, progress, cancel):
    sizes = {e["name"].lower(): e["size"] for e in info["entries"] if not e["dir"]}
    total = max(1, info["size"])
    proc = subprocess.Popen([unrar, "x", "-y", "-o-", "-p-", "-idc", archive, dest.rstrip("\\") + "\\"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)
    done, current, current_size, buf, tail = 0, "", 0, "", ""
    while True:
        if cancel is not None and cancel.is_set():
            proc.kill()
            proc.wait()
            return
        chunk = proc.stdout.read(256)
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace")
        tail = (tail + text)[-2000:]
        buf += text
        for m in re.finditer(r"Extracting\s+(.+?)\s{2,}", buf):
            if current:
                done += current_size
            name = m.group(1).strip()
            rel = name[len(dest):].lstrip("\\/") if name.lower().startswith(dest.lower()) else name
            current, current_size = rel, sizes.get(rel.lower().replace("/", "\\"), 0)
        pcts = re.findall(r"(\d{1,3})%", buf)
        if progress:
            frac = (done + current_size * (int(pcts[-1]) / 100 if pcts else 0)) / total
            progress(min(0.99, frac), f"extracting {os.path.basename(current) or '…'}")
        buf = buf[-200:]
    code = proc.wait()
    if code == 11 or "incorrect password" in tail.lower() or "encrypted" in tail.lower():
        raise ToolError(f"{os.path.basename(archive)} is password protected.", "PASSWORD")
    if code in (3,):
        raise ToolError(f"{os.path.basename(archive)} is damaged (checksum errors) — try downloading it again.",
                        "BAD_ARCHIVE")
    if code not in (0, 1, 10):
        raise ToolError(f"WinRAR stopped with error {code}: {tail.strip().splitlines()[-1] if tail.strip() else ''}",
                        "EXTRACT_FAILED")


def _unzip(archive: str, dest: str, info: Dict, progress, cancel):
    total = max(1, info["size"])
    done = 0
    root = os.path.realpath(dest)
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            if cancel is not None and cancel.is_set():
                return
            target = os.path.realpath(os.path.join(dest, member.filename))
            if not (target == root or target.startswith(root + os.sep)):
                raise ToolError("That zip tries to write outside its folder, so I didn't extract it.", "UNSAFE")
            if os.path.exists(target) and not member.is_dir():
                continue                                   # never overwrite
            try:
                z.extract(member, dest)
            except RuntimeError as e:                       # encrypted member
                raise ToolError(f"{os.path.basename(archive)} is password protected.", "PASSWORD") from e
            done += member.file_size
            if progress:
                progress(min(0.99, done / total), f"extracting {os.path.basename(member.filename)}")


def _winrar_gui(winrar: str, archive: str, dest: str, cancel):
    proc = subprocess.Popen([winrar, "x", "-ibck", "-y", "-o-", archive, dest.rstrip("\\") + "\\"])
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            proc.kill()
            return
        time.sleep(0.3)
    if proc.returncode not in (0, 1, 10):
        raise ToolError(f"WinRAR couldn't extract {os.path.basename(archive)} (error {proc.returncode}).",
                        "EXTRACT_FAILED")


# ---------------------------------------------------------------------- #
# Compress
# ---------------------------------------------------------------------- #
def compress(folder: str, fmt: str = "zip", progress: Optional[Callable] = None,
             cancel: Optional[threading.Event] = None) -> Dict:
    folder = os.path.normpath(folder)
    if not os.path.exists(folder):
        raise ToolError(f"I can't find {folder}.", "NOT_FOUND")
    base = folder.rstrip("\\/")
    fmt = "rar" if fmt == "rar" else "zip"
    target = base + "." + fmt
    n = 2
    while os.path.exists(target):
        target = f"{base} ({n}).{fmt}"
        n += 1
    rar = find_winrar()["rar"]
    if fmt == "rar" and not rar:
        raise ToolError("Making .rar files needs WinRAR, which isn't installed — I can make a zip instead.", "NO_WINRAR")
    if fmt == "rar":
        proc = subprocess.Popen([rar, "a", "-r", "-ep1", "-idc", "-ma5", target, base],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)
        buf = ""
        while True:
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.wait()
                return {"cancelled": True, "summary": "Stopped compressing."}
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            buf = (buf + chunk.decode("utf-8", errors="replace"))[-300:]
            pcts = re.findall(r"(\d{1,3})%", buf)
            if progress and pcts:
                progress(min(0.99, int(pcts[-1]) / 100), "compressing")
        if proc.wait() not in (0, 1):
            raise ToolError(f"WinRAR couldn't compress {os.path.basename(base)}.", "COMPRESS_FAILED")
    else:
        if progress:
            progress(0.1, "compressing")
        shutil.make_archive(target[:-4], "zip", root_dir=os.path.dirname(base), base_dir=os.path.basename(base))
    from modules.files.scan import human
    size = os.path.getsize(target) if os.path.exists(target) else 0
    return {"archive": target, "size": size,
            "summary": f"Compressed {os.path.basename(base)} into {os.path.basename(target)} ({human(size)})."}

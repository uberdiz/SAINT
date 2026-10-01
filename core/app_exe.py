"""
core/app_exe.py

SAINT as a real Windows app: ``.venv\\SAINT\\SAINT.exe``.

Run from source, SAINT used to show up in Task Manager as two "Python"
processes (the venv's launcher and the interpreter it starts). SAINT.exe is a
copy of the venv's base ``pythonw.exe`` with SAINT's own name, description
and icon stamped into its resources, so Task Manager, the taskbar and
Startup apps all say "SAINT" — one process, no console window.

How it finds everything without being "installed":

* It sits one folder below the venv, so Python reads ``.venv\\pyvenv.cfg``
  and uses the venv's packages (the interpreter DLLs are copied next to it).
* Started with no arguments (a double-click), ``saint_exe.pth`` in the venv's
  site-packages runs app.py. Any other interpreter in the venv ignores it.

``ensure()`` builds or refreshes it (startup does this in the background);
``python -m core.app_exe`` does it by hand. Nothing here needs admin rights.
"""

import ctypes
import logging
import os
import shutil
import struct
import sys
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger("saint.app_exe")

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
EXE_DIR = VENV / "SAINT"
EXE = EXE_DIR / "SAINT.exe"
VERSION = (2, 0, 0, 0)
BOOT_PTH = "saint_exe.pth"
BOOT_MODULE = "saint_exe_boot"

RT_ICON, RT_GROUP_ICON, RT_VERSION = 3, 14, 16


def _base_home() -> Optional[Path]:
    """The real Python install the venv was made from (pyvenv.cfg ``home``)."""
    try:
        for line in (VENV / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip().lower() == "home" and v.strip():
                return Path(v.strip())
    except OSError:
        pass
    return None


def _runtime_files(home: Path) -> List[Path]:
    """The interpreter's own DLLs: the copied exe loads them from its folder."""
    keep = []
    for f in home.iterdir():
        n = f.name.lower()
        if f.is_file() and n.endswith(".dll") and (n.startswith("python") or n.startswith("vcruntime")):
            keep.append(f)
    return keep


# ---------------------------------------------------------------------- #
# Resources: VS_VERSIONINFO and the icon
# ---------------------------------------------------------------------- #
def _pad(b: bytes) -> bytes:
    return b + b"\0" * (-len(b) % 4)


def _node(key: str, value: bytes = b"", vtype: int = 1, vlen: int = 0, children=()) -> bytes:
    out = _pad(struct.pack("<HHH", 0, vlen, vtype) + (key + "\0").encode("utf-16-le"))
    out += value
    for c in children:
        out = _pad(out) + c
    return struct.pack("<H", len(out)) + out[2:]


def _string(key: str, val: str) -> bytes:
    return _node(key, (val + "\0").encode("utf-16-le"), vtype=1, vlen=len(val) + 1)


def version_info(name: str = "SAINT", description: str = "SAINT", version=VERSION) -> bytes:
    """A VS_VERSIONINFO block. Task Manager shows FileDescription as the app's name."""
    ms, ls = (version[0] << 16) | version[1], (version[2] << 16) | version[3]
    fixed = struct.pack("<13I", 0xFEEF04BD, 0x00010000, ms, ls, ms, ls, 0x3F, 0, 0x40004, 1, 0, 0, 0)
    dotted = ".".join(map(str, version))
    strings = [_string(k, v) for k, v in (
        ("CompanyName", "SAINT"), ("FileDescription", description), ("FileVersion", dotted),
        ("InternalName", name), ("OriginalFilename", f"{name}.exe"), ("ProductName", name),
        ("ProductVersion", dotted), ("LegalCopyright", ""))]
    table = _node("040904B0", vtype=1, children=strings)
    sfi = _node("StringFileInfo", vtype=1, children=[table])
    var = _node("Translation", struct.pack("<HH", 0x0409, 0x04B0), vtype=0, vlen=4)
    vfi = _node("VarFileInfo", vtype=1, children=[var])
    return _node("VS_VERSION_INFO", fixed, vtype=0, vlen=len(fixed), children=[sfi, vfi])


def icon_resources(ico: bytes) -> Tuple[bytes, List[bytes]]:
    """(RT_GROUP_ICON data, [RT_ICON data, ...]) from a .ico file's bytes."""
    _reserved, kind, count = struct.unpack_from("<HHH", ico, 0)
    if kind != 1 or not count:
        raise ValueError("not an .ico file")
    group, images = struct.pack("<HHH", 0, 1, count), []
    for i in range(count):
        w, h, colors, res, planes, bits, size, offset = struct.unpack_from("<BBBBHHII", ico, 6 + 16 * i)
        images.append(ico[offset:offset + size])
        group += struct.pack("<BBBBHHIH", w, h, colors, res, planes, bits, size, i + 1)
    return group, images


def _stamp(exe: Path, ico: Optional[Path]):
    """Replace the copied interpreter's version info and icons with SAINT's."""
    import win32api
    import win32con
    h = win32api.LoadLibraryEx(str(exe), 0, win32con.LOAD_LIBRARY_AS_DATAFILE)
    old = []
    try:
        for typ in (RT_ICON, RT_GROUP_ICON, RT_VERSION):
            try:
                names = win32api.EnumResourceNames(h, typ)
            except win32api.error:
                continue
            for name in names:
                for lang in win32api.EnumResourceLanguages(h, typ, name):
                    old.append((typ, name, lang))
    finally:
        win32api.FreeLibrary(h)
    upd = win32api.BeginUpdateResource(str(exe), False)
    try:
        for typ, name, lang in old:                       # delete Python's
            win32api.UpdateResource(upd, typ, name, None, lang)
        win32api.UpdateResource(upd, RT_VERSION, 1, version_info(), 1033)
        if ico is not None and ico.exists():
            group, images = icon_resources(ico.read_bytes())
            for i, img in enumerate(images, 1):
                win32api.UpdateResource(upd, RT_ICON, i, img, 1033)
            win32api.UpdateResource(upd, RT_GROUP_ICON, 1, group, 1033)
    except Exception:
        win32api.EndUpdateResource(upd, True)            # discard
        raise
    win32api.EndUpdateResource(upd, False)


# ---------------------------------------------------------------------- #
def _site_packages() -> Path:
    return VENV / "Lib" / "site-packages"


_BOOT = '''"""Written by core/app_exe.py: SAINT.exe started with no script runs SAINT."""
import os
import sys


def run():
    exe = os.path.basename(sys.executable or "").lower()
    if exe != "saint.exe":
        return
    # The venv's base interpreter isn't called SAINT.exe (multiprocessing and
    # venv creation would look for it).
    base = os.path.join(os.path.dirname(getattr(sys, "_base_executable", "") or ""), "pythonw.exe")
    if not os.path.isfile(getattr(sys, "_base_executable", "")) and os.path.isfile(base):
        sys._base_executable = base
    if len(sys.argv) > 1 or (sys.argv and sys.argv[0]):
        return
    app = os.environ.get("SAINT_EXE_APP") or {app!r}
    if not os.path.isfile(app):
        return
    import runpy
    sys.argv = [app]
    sys.path.insert(0, os.path.dirname(app))
    os.chdir(os.path.dirname(app))
    try:
        runpy.run_path(app, run_name="__main__")
    finally:
        os._exit(0)          # never fall through to the interactive prompt


run()
'''


def _write_boot():
    sp = _site_packages()
    (sp / f"{BOOT_MODULE}.py").write_text(_BOOT.replace("{app!r}", repr(str(ROOT / "app.py"))), encoding="utf-8")
    (sp / BOOT_PTH).write_text(f"import {BOOT_MODULE}\n", encoding="utf-8")


def is_current() -> bool:
    """SAINT.exe exists and matches the interpreter it was copied from."""
    home = _base_home()
    if home is None or not EXE.exists():
        return False
    src = home / "pythonw.exe"
    marker = EXE_DIR / "source.txt"
    try:
        stamp = f"{src}|{src.stat().st_size}|{int(src.stat().st_mtime)}"
        return marker.read_text(encoding="utf-8") == stamp and (_site_packages() / BOOT_PTH).exists()
    except OSError:
        return False


def build(ico: Optional[Path] = None) -> Path:
    """(Re)build .venv\\SAINT\\SAINT.exe. Raises on failure."""
    if os.name != "nt":
        raise RuntimeError("SAINT.exe is Windows only.")
    home = _base_home()
    if home is None or not (home / "pythonw.exe").exists():
        raise RuntimeError("Couldn't find the Python install behind .venv (pyvenv.cfg 'home').")
    EXE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = EXE_DIR / "SAINT.new.exe"
    shutil.copy2(home / "pythonw.exe", tmp)
    if ico is None:
        try:
            from core.autostart import icon_path
            p = icon_path()
            ico = Path(p) if p else None
        except Exception:
            ico = None
    _stamp(tmp, ico)
    for f in _runtime_files(home):
        dst = EXE_DIR / f.name
        if not dst.exists() or dst.stat().st_size != f.stat().st_size:
            shutil.copy2(f, dst)
    try:
        os.replace(tmp, EXE)
    except PermissionError:                                # SAINT.exe is running: next start
        raise RuntimeError("SAINT.exe is in use — it will be refreshed next time SAINT starts from source.")
    _write_boot()
    src = home / "pythonw.exe"
    (EXE_DIR / "source.txt").write_text(f"{src}|{src.stat().st_size}|{int(src.stat().st_mtime)}",
                                        encoding="utf-8")
    log.info("app_exe.built %s", EXE)
    return EXE


def ensure() -> Optional[Path]:
    """Build SAINT.exe if it's missing or stale, and point the shortcuts at it."""
    from core import autostart
    if os.name != "nt" or getattr(sys, "frozen", False) or not VENV.exists():
        autostart.sync()
        return None
    try:
        if not is_current():
            build()
    except Exception as e:
        log.info("app_exe.unavailable %s", e)
    try:
        autostart.refresh_shortcuts()
    except Exception as e:
        log.info("app_exe.shortcuts_failed %s", e)
    return EXE if EXE.exists() else None


def running_as_exe() -> bool:
    return os.path.basename(sys.executable or "").lower() == "saint.exe"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    path = build()
    from core import autostart
    autostart.refresh_shortcuts(start_menu=True)
    print(f"Built {path}")

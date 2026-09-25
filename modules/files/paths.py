"""
modules/files/paths.py

Where things are, and what SAINT must never touch.

* ``resolve_folder("my downloads")`` → the real Downloads folder (Windows'
  known-folder API, so a moved Downloads folder still resolves); "my games
  folder" → files.games_dir; "the D drive" → D:\\; user names from files.known.
* ``denied(path)`` → why SAINT may never move / recycle it (Windows, Program
  Files, drive roots, your profile folder itself, SAINT, source repos), or None.
* ``protected(path)`` → why SAINT won't *suggest* removing it (Steam
  libraries, your games folder, emulators and ROMs), or None. You can still
  ask for a protected folder to be moved.
"""

import os
import re
import string
from pathlib import Path
from typing import Dict, List, Optional

from core.config import config

_KNOWN = {
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
}
_SYNONYMS = {"download": "downloads", "my downloads": "downloads", "docs": "documents", "document": "documents",
             "my documents": "documents", "photos": "pictures", "pics": "pictures", "video": "videos",
             "movies": "videos", "games": "games", "game": "games", "my games": "games"}

EMULATOR_NAMES = ("retroarch", "dolphin", "pcsx2", "rpcs3", "yuzu", "ryujinx", "cemu", "duckstation", "ppsspp",
                  "citra", "lime3ds", "xenia", "desmume", "melonds", "mgba", "snes9x", "project64", "epsxe",
                  "vita3k", "suyu", "sudachi", "shadps4", "xemu", "mame", "emulationstation", "launchbox",
                  "playnite", "emudeck")
ROM_EXTENSIONS = frozenset({".iso", ".chd", ".rvz", ".wbfs", ".gcm", ".nsp", ".xci", ".3ds", ".cia", ".gba", ".gbc",
                            ".gb", ".sfc", ".smc", ".nes", ".z64", ".n64", ".v64", ".nds", ".cso", ".pbp", ".cue",
                            ".gdi", ".wad", ".rom", ".md", ".gen", ".sms", ".pce", ".32x", ".xex"})


def known_folder(name: str) -> Optional[str]:
    key = _SYNONYMS.get(name.strip().lower(), name.strip().lower())
    if key == "games":
        p = config.get("files.games_dir", "") or ""
        return os.path.normpath(p) if p else None
    guid = _KNOWN.get(key)
    if not guid:
        return None
    try:
        from win32com.shell import shell
        import pywintypes
        return os.path.normpath(shell.SHGetKnownFolderPath(pywintypes.IID(guid), 0, None))
    except Exception:
        fallback = Path.home() / key.capitalize()
        return str(fallback) if fallback.exists() else None


def fixed_drives() -> List[str]:
    """['C:\\', 'D:\\', 'E:\\'] — local disks only (no network / optical)."""
    try:
        import psutil
        out = []
        for part in psutil.disk_partitions(all=False):
            if "fixed" in (part.opts or "").lower() or ("cdrom" not in (part.opts or "").lower()
                                                         and part.fstype):
                out.append(part.mountpoint)
        return sorted(set(out))
    except Exception:
        return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]


def resolve_folder(spoken: str) -> Optional[str]:
    """A spoken folder ("my downloads", "games folder", "the E drive",
    "D:\\Games", "my emulators folder") → an existing path, or None."""
    s = (spoken or "").strip().strip("\"'").rstrip(".")
    if not s:
        return None
    if re.match(r"^[a-z]:[\\/]", s, re.I) or s.startswith("\\\\"):
        return os.path.normpath(s) if os.path.exists(s) else None
    low = re.sub(r"^(?:the|my|your)\s+", "", s.lower())
    low = re.sub(r"\s+(?:folder|directory|dir)$", "", low).strip()
    m = re.match(r"^([a-z])(?::)?(?:\s+drive)?$", low) or re.match(r"^drive\s+([a-z])$", low)
    if m and (len(low) == 1 or "drive" in low or low.endswith(":")):
        root = f"{m.group(1).upper()}:\\"
        return root if os.path.exists(root) else None
    known = {k.lower(): v for k, v in (config.get("files.known", {}) or {}).items()}
    if low in known and os.path.exists(known[low]):
        return os.path.normpath(known[low])
    p = known_folder(low)
    if p and os.path.exists(p):
        return p
    return None


# ---------------------------------------------------------------------- #
# Never touch / never suggest
# ---------------------------------------------------------------------- #
def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def _under(path: str, root: str) -> bool:
    path, root = _norm(path), _norm(root)
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def _live_windows() -> str:
    return os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"


def _deny_roots() -> List[str]:
    env = os.environ
    roots = [_live_windows(), env.get("ProgramFiles", r"C:\Program Files"),
             env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), env.get("ProgramData", r"C:\ProgramData"),
             os.path.join(env.get("LOCALAPPDATA", ""), "Microsoft", "Windows") if env.get("LOCALAPPDATA") else "",
             str(Path(__file__).resolve().parents[2])]              # SAINT itself
    return [r for r in roots if r]


_DENY_NAMES = re.compile(r"^(\$recycle\.bin|system volume information|pagefile\.sys|hiberfil\.sys|swapfile\.sys|"
                         r"dumpstack\.log(\.tmp)?|bootmgr|boot|recovery|\$sysreset|\$winreagent)$", re.I)


def denied(path: str) -> Optional[str]:
    """Why SAINT must never move or recycle ``path`` (None = allowed)."""
    if not path:
        return "No path given."
    p = os.path.abspath(path)
    drive, rest = os.path.splitdrive(p)
    if not rest.strip("\\/"):
        return f"{drive}\\ is a whole drive."
    home = str(Path.home())
    if _norm(p) == _norm(home):
        return "That's your whole user folder."
    for root in _deny_roots():
        if _under(p, root):
            return f"{p} is part of Windows, installed programs or SAINT itself."
    parts = Path(p).parts
    if any(_DENY_NAMES.match(x) for x in parts[1:]):
        return f"{p} is a Windows system file or folder."
    if any(x.lower() == ".git" for x in parts) or os.path.isdir(os.path.join(p, ".git")):
        return f"{p} is a code repository."
    for known in _KNOWN:
        kp = known_folder(known)
        if kp and _norm(p) == _norm(kp):
            return f"That's your whole {known.capitalize()} folder."
    return None


def steam_library_roots() -> List[str]:
    try:
        from modules.steam.library import libraries
        return [lib["path"] for lib in libraries()]
    except Exception:
        return []


def is_emulator_dir(path: str) -> bool:
    name = os.path.basename(os.path.normpath(path)).lower()
    return any(e in name for e in EMULATOR_NAMES) or name in ("emulators", "emulation", "roms", "bios")


def protected(path: str) -> Optional[str]:
    """Why SAINT won't *suggest* removing ``path`` (games and emulators)."""
    p = os.path.abspath(path)
    for lib in steam_library_roots():
        if _under(p, lib):
            return "it's part of a Steam library"
    games = config.get("files.games_dir", "") or ""
    if games and _under(p, games):
        return "it's in your games folder"
    parts = Path(p).parts
    if any(is_emulator_dir(x) for x in parts[1:]):
        return "it's part of an emulator setup"
    if os.path.splitext(p)[1].lower() in ROM_EXTENSIONS:
        return "it looks like a game ROM or disc image"
    if re.search(r"\\(xboxgames|epic games|gog galaxy\\games|gog games|riot games|battle\.net|ea games|"
                 r"ubisoft game launcher)(\\|$)", p, re.I):
        return "it's a game install"
    return None


def old_windows_candidates() -> List[Dict]:
    """Drives that hold an *old* Windows installation (not the one running):
    a Windows\\System32 plus Users folder, or Windows.old / $Windows.~BT."""
    live = os.path.splitdrive(_live_windows())[0].upper()
    out = []
    for root in fixed_drives():
        drive = os.path.splitdrive(root)[0].upper()
        found = []
        if drive != live and os.path.isdir(os.path.join(root, "Windows", "System32")) \
                and os.path.isdir(os.path.join(root, "Users")):
            found += [os.path.join(root, "Windows"), os.path.join(root, "Users")]
            if os.path.isdir(os.path.join(root, "Program Files")):
                found.append(os.path.join(root, "Program Files"))
        for leftover in ("Windows.old", "$Windows.~BT", "$Windows.~WS"):
            lp = os.path.join(root, leftover)
            if os.path.isdir(lp):
                found.append(lp)
        if found:
            out.append({"drive": root, "paths": found, "running_windows": drive == live})
    return out

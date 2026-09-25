"""
modules/steam/library.py

What's installed through Steam, read straight from Steam's own files:

    <Steam>/steamapps/libraryfolders.vdf     every library Steam knows about
    <library>/steamapps/appmanifest_*.acf    one per installed game (name, size, folder)

Libraries in steam.extra_libraries (default D:\\SteamLibrary) are read too.
When one of those isn't registered with Steam, its games are listed but
flagged: Steam can't launch them until the folder is added under
Steam > Settings > Storage.
"""

import difflib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from core.config import config
from modules.steam import vdf

log = logging.getLogger("saint.steam")

# Not games: runtimes and tools Steam installs alongside them.
_NOT_GAMES = re.compile(r"redistributable|steamworks common|proton|steam linux runtime|directx|vc\+\+|"
                        r"\bsdk\b|dedicated server|steamvr", re.I)


@dataclass
class Game:
    appid: str
    name: str
    installdir: str
    size: int                 # bytes on disk
    library: str              # library root, e.g. D:\SteamLibrary
    registered: bool          # Steam knows about this library
    last_played: int = 0

    @property
    def path(self) -> str:
        return os.path.join(self.library, "steamapps", "common", self.installdir)

    def to_dict(self) -> dict:
        return {"appid": self.appid, "name": self.name, "size": self.size, "library": self.library,
                "registered": self.registered, "path": self.path, "last_played": self.last_played}


def steam_path() -> Optional[str]:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            p = winreg.QueryValueEx(k, "SteamPath")[0]
            if p and os.path.isdir(p):
                return os.path.normpath(p)
    except OSError:
        pass
    for guess in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        if os.path.isdir(guess):
            return guess
    return None


def _norm_path(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def libraries(root: Optional[str] = None, extra: Optional[List[str]] = None) -> List[Dict]:
    """[{path, registered}] — Steam's libraries plus steam.extra_libraries."""
    root = root if root is not None else steam_path()
    out: List[Dict] = []
    seen = set()
    if root:
        for p in [root] + _registered_paths(root):
            key = _norm_path(p)
            if key not in seen and os.path.isdir(p):
                seen.add(key)
                out.append({"path": os.path.normpath(p), "registered": True})
    for p in (extra if extra is not None else config.get("steam.extra_libraries", []) or []):
        key = _norm_path(p)
        if key not in seen and os.path.isdir(os.path.join(p, "steamapps")):
            seen.add(key)
            out.append({"path": os.path.normpath(p), "registered": False})
    return out


def _registered_paths(root: str) -> List[str]:
    path = os.path.join(root, "steamapps", "libraryfolders.vdf")
    try:
        data = vdf.load(path)
    except OSError:
        return []
    folders = vdf.get_ci(data, "libraryfolders", {}) or {}
    out = []
    for _key, val in folders.items():
        if isinstance(val, dict):
            p = vdf.get_ci(val, "path")
        else:
            p = val if isinstance(val, str) and os.path.isabs(val) else None
        if p:
            out.append(p)
    return out


def read_manifest(path: str, library: str, registered: bool) -> Optional[Game]:
    try:
        data = vdf.get_ci(vdf.load(path), "AppState", {})
    except OSError:
        return None
    name = re.sub(r"\s*[™®©]\s*", " ", vdf.get_ci(data, "name", "") or "").strip()   # read aloud cleanly
    appid = str(vdf.get_ci(data, "appid", "") or "")
    if not name or not appid or _NOT_GAMES.search(name) or appid == "228980":
        return None
    try:
        size = int(vdf.get_ci(data, "SizeOnDisk", 0) or 0)
    except ValueError:
        size = 0
    try:
        played = int(vdf.get_ci(data, "LastPlayed", 0) or 0)
    except ValueError:
        played = 0
    return Game(appid, name, vdf.get_ci(data, "installdir", "") or name, size, library, registered, played)


class SteamLibrary:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = (0.0, None, [])            # (time, signature, games)

    def games(self, force: bool = False) -> List[Game]:
        libs = libraries()
        sig = tuple((lib["path"], _mtime(os.path.join(lib["path"], "steamapps"))) for lib in libs)
        with self._lock:
            at, old_sig, cached = self._cache
            if not force and old_sig == sig and time.time() - at < 300:
                return list(cached)
        games: List[Game] = []
        seen = set()
        for lib in libs:
            apps = os.path.join(lib["path"], "steamapps")
            try:
                names = [n for n in os.listdir(apps) if n.startswith("appmanifest_") and n.endswith(".acf")]
            except OSError:
                continue
            for n in names:
                g = read_manifest(os.path.join(apps, n), lib["path"], lib["registered"])
                if g and g.appid not in seen:
                    seen.add(g.appid)
                    games.append(g)
        games.sort(key=lambda g: g.name.lower())
        with self._lock:
            self._cache = (time.time(), sig, games)
        return list(games)

    def match(self, name: str, games: Optional[List[Game]] = None) -> Optional[Game]:
        """The installed game a spoken name means ("counter strike 2", "cs2",
        "terraria"), or None."""
        games = games if games is not None else self.games()
        q = _simplify(name)
        if not q:
            return None
        by = {_simplify(g.name): g for g in games}
        if q in by:
            return by[q]
        alias = _ALIASES.get(q)
        if alias and alias in by:
            return by[alias]
        starts = [g for k, g in by.items() if k.startswith(q + " ") or k.startswith(q)]
        if len(starts) == 1 and len(q) >= 4:
            return starts[0]
        acronym = {"".join(w[0] for w in k.split() if w): g for k, g in by.items() if len(k.split()) >= 2}
        if q.replace(" ", "") in acronym:
            return acronym[q.replace(" ", "")]
        close = difflib.get_close_matches(q, list(by), n=1, cutoff=0.85)
        return by[close[0]] if close else None


_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
_ALIASES = {"cs2": "counter strike 2", "cs": "counter strike 2", "csgo": "counter strike 2",
            "gmod": "garry s mod", "l4d2": "left 4 dead 2", "rdr2": "red dead redemption 2",
            "tf2": "team fortress 2", "wallpaper": "wallpaper engine"}


def _simplify(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[™®©:]", "", s)
    s = re.sub(r"[^\w\s]", " ", s)
    words = [_ROMAN.get(w, w) for w in s.split()]
    return " ".join(w for w in words if w not in ("the",))


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


steam_library = SteamLibrary()

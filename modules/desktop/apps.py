"""
modules/desktop/apps.py

Application resolution for "open X" commands.

SAINT never passes a user- or LLM-supplied string to a shell. An app name is
resolved against a catalogue of things that are actually installed:

  1. user aliases              config desktop.apps  {"notes": "C:/.../Obsidian.exe"}
  2. built-in well-known apps  (Windows tools, URI schemes such as ms-settings:)
  3. Start Menu shortcuts      (.lnk files for all users + current user)
  4. Store / UWP apps          (Get-StartApps -> shell:AppsFolder\\<AppID>)
  5. App Paths registry        (chrome.exe, msedge.exe, ...)

and launched with os.startfile / an argument list (no shell=True).
"""

import difflib
import json
import logging
import os
import re
import struct
import subprocess
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

log = logging.getLogger("saint.desktop")


@dataclass
class AppEntry:
    name: str             # display name
    kind: str             # "path" | "uri" | "appsfolder" | "exe"
    target: str
    source: str           # where it was found
    process_hint: str = ""  # executable name used to find its windows


# name -> (kind, target, process hint)
_BUILTIN: Dict[str, tuple] = {
    "notepad": ("exe", "notepad.exe", "notepad"),
    "calculator": ("exe", "calc.exe", "calculator"),
    "calc": ("exe", "calc.exe", "calculator"),
    "file explorer": ("exe", "explorer.exe", "explorer"),
    "explorer": ("exe", "explorer.exe", "explorer"),
    "files": ("exe", "explorer.exe", "explorer"),
    "task manager": ("exe", "taskmgr.exe", "taskmgr"),
    "paint": ("exe", "mspaint.exe", "mspaint"),
    "command prompt": ("exe", "cmd.exe", "cmd"),
    "settings": ("uri", "ms-settings:", "systemsettings"),
    "windows settings": ("uri", "ms-settings:", "systemsettings"),
    "sound settings": ("uri", "ms-settings:sound", "systemsettings"),
    "display settings": ("uri", "ms-settings:display", "systemsettings"),
    "bluetooth settings": ("uri", "ms-settings:bluetooth", "systemsettings"),
}

# Common spoken names -> Start Menu / executable names
_SYNONYMS = {
    "chrome": ["google chrome", "chrome"],
    "google chrome": ["google chrome"],
    "edge": ["microsoft edge"],
    "firefox": ["firefox", "mozilla firefox"],
    "vs code": ["visual studio code"],
    "vscode": ["visual studio code"],
    "code": ["visual studio code"],
    "visual studio": ["visual studio 2022", "visual studio"],
    "word": ["word"],
    "excel": ["excel"],
    "powerpoint": ["powerpoint"],
    "outlook": ["outlook"],
    "teams": ["microsoft teams", "teams"],
    "discord": ["discord"],
    "spotify": ["spotify"],
    "steam": ["steam"],
    "obs": ["obs studio"],
    "terminal": ["terminal", "windows terminal"],
}

_START_MENU_DIRS = [
    os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs"),
    os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
]

# Apps pinned to the taskbar or with a desktop shortcut but no Start-menu entry
# (Bloxstrap installs only a taskbar pin). Scanned one level deep.
_EXTRA_SHORTCUT_DIRS = [
    os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar"),
    os.path.join(os.path.expanduser("~"), "Desktop"),
    os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop"),
]

_IGNORED_SHORTCUTS = re.compile(r"\b(uninstall|readme|help|documentation|release notes|website|license)\b", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


class _ShortcutReader:
    """Reads where a .lnk points by parsing the file ([MS-SHLLINK] LinkInfo) — no COM.
    A cached WScript.Shell object went stale and crashed the whole process with an access
    violation (the release build's tests, 2026-10-06)."""

    def target(self, path: str) -> str:
        if not path.lower().endswith(".lnk"):
            return ""
        try:
            with open(path, "rb") as f:
                data = f.read(1 << 20)
            return lnk_target(data)
        except (OSError, ValueError, IndexError, struct.error):
            return ""


def _idlist_path(ids: bytes) -> str:
    """A file-system path from a shell item ID list: a drive item ("C:\\") then folder/file
    items (long name from their 0xBEEF0004 extension block, else the 8.3 name)."""
    parts, i = [], 0
    while i + 2 <= len(ids):
        size = struct.unpack_from("<H", ids, i)[0]
        if size < 3:
            break
        item, kind = ids[i:i + size], ids[i + 2] & 0x70
        if kind == 0x20:                                  # volume: "C:\"
            m = re.match(rb"([A-Za-z]:\\)", item[3:])
            if m:
                parts = [m.group(1).decode()]
        elif kind == 0x30 and parts:                      # file or folder entry
            ext = item.find(b"\x04\x00\xef\xbe")
            names = re.findall(rb"((?:[^\x00]\x00){1,255})\x00\x00", item[ext:]) if ext > 0 else []
            if names:
                parts.append(names[0].decode("utf-16-le", "replace"))
            else:
                short = item[14:].split(b"\0", 1)[0]
                parts.append(short.decode("latin-1"))
        i += size
    if not parts or not parts[0].endswith("\\"):
        return ""
    return parts[0] + "\\".join(parts[1:])


def lnk_target(data: bytes) -> str:
    """The local path a shell link points at, or "" (advertised / URL / network-only links)."""
    if len(data) < 0x4C or struct.unpack_from("<I", data, 0)[0] != 0x4C:
        return ""
    flags = struct.unpack_from("<I", data, 0x14)[0]
    pos = 0x4C
    idlist_path = ""
    if flags & 0x1:                                       # HasLinkTargetIDList
        size = struct.unpack_from("<H", data, pos)[0]
        idlist_path = _idlist_path(data[pos + 2:pos + 2 + size])
        pos += 2 + size
    if not flags & 0x2:                                   # HasLinkInfo (absent when the target never existed)
        return idlist_path
    info = pos
    header = struct.unpack_from("<I", data, info + 4)[0]
    info_flags = struct.unpack_from("<I", data, info + 8)[0]
    if not info_flags & 0x1:                              # VolumeIDAndLocalBasePath
        return idlist_path
    base_off, suffix_off = struct.unpack_from("<II", data, info + 0x10)

    def ansi(off):
        end = data.index(b"\0", off)
        return data[off:end].decode("mbcs" if os.name == "nt" else "latin-1", "replace")

    def wide(off):
        end = off
        while data[end:end + 2] != b"\0\0":
            end += 2
        return data[off:end].decode("utf-16-le", "replace")
    if header >= 0x24:                                    # Unicode paths present
        base_u, suffix_u = struct.unpack_from("<II", data, info + 0x1C)
        base, suffix = wide(info + base_u), (wide(info + suffix_u) if suffix_u else "")
    else:
        base, suffix = ansi(info + base_off), (ansi(info + suffix_off) if suffix_off else "")
    if suffix and not base.endswith("\\"):
        base += "\\"
    return base + suffix


_shortcuts = _ShortcutReader()


def broken_shortcut(path: str) -> bool:
    """A shortcut to a program that's gone ("Antigravity.lnk" left behind when the app
    reinstalled as "Antigravity IDE"). Windows answers launching one with ERROR_CANCELLED,
    which SAINT used to read out as "the operation was canceled by the user".
    Installer-advertised shortcuts (no plain target) and URLs count as fine."""
    target = _shortcuts.target(path)
    return bool(re.match(r"^[a-z]:\\", target, re.I)) and not os.path.exists(target)


class AppCatalog:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries: Optional[Dict[str, AppEntry]] = None
        self._loading = False

    # ------------------------------------------------------------------ #
    def warm(self):
        """Build the catalogue in the background (Get-StartApps takes ~1s)."""
        threading.Thread(target=self.entries, daemon=True, name="app-catalog").start()

    def entries(self) -> Dict[str, AppEntry]:
        with self._lock:
            if self._entries is not None:
                return self._entries
            entries: Dict[str, AppEntry] = {}
            self._scan_start_menu(entries)
            self._scan_start_apps(entries)
            self._entries = entries
            log.info("desktop.apps.catalog entries=%d", len(entries))
            return entries

    def refresh(self):
        with self._lock:
            self._entries = None
        return self.entries()

    def _scan_start_menu(self, entries: Dict[str, AppEntry]):
        for base in _START_MENU_DIRS:
            if not base or not os.path.isdir(base):
                continue
            for root, _dirs, files in os.walk(base):
                for f in files:
                    if not f.lower().endswith((".lnk", ".url")):
                        continue
                    name = os.path.splitext(f)[0]
                    if _IGNORED_SHORTCUTS.search(name):
                        continue
                    path = os.path.join(root, f)
                    if broken_shortcut(path):
                        log.info("desktop.apps.broken_shortcut %s", path)
                        continue
                    key = _norm(name)
                    entries.setdefault(key, AppEntry(name, "path", path, "start_menu",
                                                     process_hint=key.split(" ")[0]))
        for base in _EXTRA_SHORTCUT_DIRS:
            try:
                files = os.listdir(base) if base and os.path.isdir(base) else []
            except OSError:
                files = []
            for f in files:
                if not f.lower().endswith((".lnk", ".url")):
                    continue
                name = os.path.splitext(f)[0]
                if _IGNORED_SHORTCUTS.search(name) or broken_shortcut(os.path.join(base, f)):
                    continue
                key = _norm(name)
                entries.setdefault(key, AppEntry(name, "path", os.path.join(base, f), "shortcut",
                                                 process_hint=key.split(" ")[0]))

    def _scan_start_apps(self, entries: Dict[str, AppEntry]):
        if os.name != "nt":
            return
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            data = json.loads(out) if out.strip() else []
            if isinstance(data, dict):
                data = [data]
            for item in data:
                name, appid = item.get("Name") or "", item.get("AppID") or ""
                if not name or not appid or _IGNORED_SHORTCUTS.search(name):
                    continue
                key = _norm(name)
                if key not in entries:
                    entries[key] = AppEntry(name, "appsfolder", appid, "start_apps",
                                            process_hint=key.split(" ")[0])
        except Exception as e:
            log.warning("desktop.apps.start_apps_failed %s", e)

    # ------------------------------------------------------------------ #
    def resolve(self, name: str) -> Optional[AppEntry]:
        from core.config import config

        # A real path ("C:\Users\me\Downloads\Bloxstrap.exe", quoted or not):
        # start exactly that file, never something that sounds like it.
        raw = (name or "").strip().strip("\"'“”")
        if re.match(r"^[a-z]:[\\/]", raw, re.I) and os.path.exists(raw):
            stem = os.path.splitext(os.path.basename(raw))[0]
            return AppEntry(stem, "path", raw, "path", process_hint=stem.lower())

        query = _norm(name)
        query = re.sub(r"^(the|my|an?)\s+(?=\S)", "", query)        # "open a rocket leak"
        query = re.sub(r"\s+(app|application|program)$", "", query)
        if not query:
            return None

        custom = {(_norm(k)): v for k, v in (config.get("desktop.apps", {}) or {}).items()}
        if query in custom:
            target = str(custom[query]).strip().strip("\"'")     # '"C:\...\x.exe"' copied from Explorer
            # "steam://open/games" and "ms-settings:display" are URIs; "C:\..." (a
            # one-letter scheme) is a path.
            kind = "uri" if re.match(r"^[a-z][\w+.-]+:", target, re.I) else "path"
            # Prefer the target's filename stem as the process hint — the display
            # alias ("AIDE") often has no relation to the actual process
            # (e.g. aide.exe / VSCodium.exe). Falls back to the alias.
            stem = ""
            if kind == "path":
                stem = os.path.splitext(os.path.basename(target.strip('"')))[0].lower()
            else:
                stem = target.split(":", 1)[0].lower()     # steam://... -> the Steam process
            return AppEntry(name, kind, target, "custom",
                            process_hint=stem or query.split(" ")[0])

        if query in _BUILTIN:
            kind, target, hint = _BUILTIN[query]
            return AppEntry(query.title(), kind, target, "builtin", process_hint=hint)

        entries = self.entries()
        candidates = [query] + _SYNONYMS.get(query, [])
        for cand in candidates:
            if cand in entries:
                return entries[cand]
        # Prefix / contains matches ("chrome" -> "google chrome")
        for cand in candidates:
            hits = [k for k in entries if k.startswith(cand + " ") or k.endswith(" " + cand) or k == cand]
            if hits:
                return entries[min(hits, key=len)]
        # Misheard names, by sound and spelling together: "clad" / "clawed" -> Claude, "rocket leak" ->
        # Rocket League (modules/desktop/vocabulary.py). Two that fit equally ("cloud": Claude or
        # iCloud) are asked about instead of guessed.
        from modules.desktop.vocabulary import vocabulary
        meant = vocabulary.resolve(query, list(entries))
        if meant:
            return entries[meant]
        if vocabulary.ambiguous(query, list(entries)):
            return None
        close = difflib.get_close_matches(query, list(entries), n=1, cutoff=0.82)
        if not close and " " in query:
            # "block strap" -> Bloxstrap: speech-to-text split one word in two.
            squashed = {k.replace(" ", ""): k for k in entries}
            hit = difflib.get_close_matches(query.replace(" ", ""), list(squashed), n=1, cutoff=0.82)
            close = [squashed[hit[0]]] if hit else []
        if close:
            return entries[close[0]]

        exe = self._app_paths(query)
        if exe:
            return AppEntry(query.title(), "path", exe, "app_paths", process_hint=query.split(" ")[0])
        # "Windows Disk Cleanup", "Microsoft Paint": the Start menu calls them without the maker.
        m = re.match(r"^(?:windows|microsoft|ms)\s+(.+)$", query)
        if m:
            return self.resolve(m.group(1))
        return None

    @staticmethod
    def _app_paths(query: str) -> Optional[str]:
        if os.name != "nt":
            return None
        try:
            import winreg
        except ImportError:
            return None
        names = {query.replace(" ", "") + ".exe"}
        for syn in _SYNONYMS.get(query, []):
            names.add(syn.split(" ")[-1] + ".exe")
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for exe in names:
                try:
                    with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                        value, _ = winreg.QueryValueEx(k, None)
                        if value and os.path.exists(value.strip('"')):
                            return value.strip('"')
                except OSError:
                    continue
        return None

    def suggestions(self, name: str, n: int = 3) -> List[str]:
        """Installed apps the user may have meant, the ones that *sound* like it first."""
        from modules.desktop.vocabulary import vocabulary
        entries = self.entries()
        query = re.sub(r"^(the|my|an?)\s+(?=\S)", "", _norm(name))
        sounds = [m.name for m in vocabulary.rank(query, list(entries), limit=n)]
        close = difflib.get_close_matches(query, list(entries), n=n, cutoff=0.5)
        return [entries[c].name for c in list(dict.fromkeys(sounds + close))[:n]]


# Executable names for apps whose process isn't their display name.
_KNOWN_PROCESSES = {
    "chrome": {"chrome"}, "google chrome": {"chrome"}, "edge": {"msedge"}, "microsoft edge": {"msedge"},
    "firefox": {"firefox"}, "opera": {"opera"}, "opera gx": {"opera"}, "opera browser": {"opera"},
    "brave": {"brave"}, "vivaldi": {"vivaldi"}, "spotify": {"spotify"}, "discord": {"discord"},
    "steam": {"steam", "steamwebhelper"}, "vs code": {"code"}, "vscode": {"code"},
    "visual studio code": {"code"}, "code": {"code"}, "file explorer": {"explorer"}, "explorer": {"explorer"},
    "files": {"explorer"}, "word": {"winword"}, "excel": {"excel"}, "powerpoint": {"powerpnt"},
    "outlook": {"outlook", "olk"}, "teams": {"ms-teams", "teams"}, "terminal": {"windowsterminal"},
    "windows terminal": {"windowsterminal"}, "notepad": {"notepad"}, "calculator": {"calculatorapp", "calculator"},
    "settings": {"systemsettings"}, "task manager": {"taskmgr"}, "obs": {"obs64", "obs"},
    "roblox studio": {"robloxstudiobeta"}, "roblox": {"robloxplayerbeta"}, "claude": {"claude"},
}


def process_names_for(name: str, entry: Optional[AppEntry] = None) -> set:
    """Likely executable stems (no .exe) for an app, used to find its windows."""
    q = _norm(re.sub(r"^(the|my)\s+", "", _norm(name or "")))
    procs = set(_KNOWN_PROCESSES.get(q, set()))
    if entry is not None:
        procs |= _KNOWN_PROCESSES.get(_norm(entry.name), set())
        # Any path-shaped target — including custom entries pointing at a
        # launcher .exe — contributes its basename stem. Previously we only
        # accepted targets ending exactly in ".exe", which missed .lnk / .bat.
        tgt = (entry.target or "").strip('"')
        stem = os.path.splitext(os.path.basename(tgt))[0].lower()
        if stem and entry.kind in ("path", "exe"):
            procs.add(stem)
        if entry.process_hint:
            procs.add(entry.process_hint.lower())
    if not procs and q:
        procs.add(q.replace(" ", ""))
    return {p for p in procs if p}


def launch(entry: AppEntry):
    """Launch a resolved app without a shell."""
    if entry.kind == "appsfolder":
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{entry.target}"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    elif entry.kind == "exe":
        subprocess.Popen([entry.target])
    else:  # path or uri
        os.startfile(entry.target)  # type: ignore[attr-defined]


app_catalog = AppCatalog()

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

_IGNORED_SHORTCUTS = re.compile(r"\b(uninstall|readme|help|documentation|release notes|website|license)\b", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


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
                    key = _norm(name)
                    entries.setdefault(key, AppEntry(name, "path", os.path.join(root, f), "start_menu",
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

        query = _norm(name)
        query = re.sub(r"^(the|my)\s+", "", query)
        query = re.sub(r"\s+(app|application|program)$", "", query)
        if not query:
            return None

        custom = {(_norm(k)): v for k, v in (config.get("desktop.apps", {}) or {}).items()}
        if query in custom:
            target = str(custom[query])
            kind = "uri" if re.match(r"^[a-z][\w+.-]*:(?![\\/])", target, re.I) else "path"
            return AppEntry(name, kind, target, "custom", process_hint=query.split(" ")[0])

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
        close = difflib.get_close_matches(query, list(entries), n=1, cutoff=0.82)
        if close:
            return entries[close[0]]

        exe = self._app_paths(query)
        if exe:
            return AppEntry(query.title(), "path", exe, "app_paths", process_hint=query.split(" ")[0])
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
        entries = self.entries()
        close = difflib.get_close_matches(_norm(name), list(entries), n=n, cutoff=0.5)
        return [entries[c].name for c in close]


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

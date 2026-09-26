"""
modules/steam/tools.py

Steam tools: list installed games, launch one, open the library, search the
store, uninstall (always asks), and game sizes for the storage view.

Everything goes through Steam's own steam:// links, so Steam does the work
(and shows its own confirmation for uninstalling).
"""

import logging
import os
import threading
import time
import urllib.parse

from modules.automation.tools import P, PermissionLevel, Tool, ToolError
from modules.steam.library import steam_library, steam_path


def _available():
    return (True, "") if steam_path() else (False, "Steam isn't installed on this PC.")


def _open(uri: str):
    try:
        os.startfile(uri)
    except OSError as e:
        raise ToolError(f"Windows couldn't open Steam: {e.strerror or e}", "LAUNCH_FAILED")


log = logging.getLogger("saint.steam")


def _steam_running() -> bool:
    try:
        import psutil
        return any((p.info.get("name") or "").lower() == "steam.exe" for p in psutil.process_iter(["name"]))
    except Exception:
        return True          # can't tell: behave as before


def _steam_window_up() -> bool:
    try:
        from modules.desktop.controller import desktop
        return any(w.process.lower().startswith(("steamwebhelper", "steam")) and w.title and not w.minimized
                   for w in desktop.list_windows())
    except Exception:
        return False


def _open_in_steam(uri: str) -> bool:
    """Open a steam:// page. When Steam isn't running, launching it with the
    link starts Steam but drops the page ("I had to open Steam myself"), so the
    link is sent again once Steam's window is up. Returns True if Steam was
    starting."""
    starting = not _steam_running()
    _open(uri)
    if starting:
        def again():
            deadline = time.time() + 45
            while time.time() < deadline and not _steam_window_up():
                time.sleep(1.0)
            time.sleep(2.5)                    # let the client finish loading its pages
            try:
                _open(uri)
                log.info("steam.reopened %s", uri.split("?")[0])
            except Exception:
                pass
        threading.Thread(target=again, daemon=True, name="steam-nav").start()
    return starting


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _find(name: str):
    g = steam_library.match(name)
    if g is None:
        raise ToolError(f"I couldn't find a game called {name} in your Steam libraries.", "NOT_FOUND")
    return g


def list_games(sort: str = "name"):
    games = steam_library.games(force=True)
    if sort == "size":
        games.sort(key=lambda g: g.size, reverse=True)
    elif sort == "recent":
        games.sort(key=lambda g: g.last_played, reverse=True)
    return {"count": len(games), "total_size": sum(g.size for g in games),
            "games": [dict(g.to_dict(), size_text=_gb(g.size)) for g in games],
            "unregistered": sorted({g.library for g in games if not g.registered})}


def launch(name: str):
    g = _find(name)
    if not g.registered:
        raise ToolError(f"{g.name} is in {g.library}, which Steam doesn't know about yet, so it can't start it. "
                        f"Add that folder in Steam > Settings > Storage and it'll launch.", "UNREGISTERED_LIBRARY")
    _open(f"steam://rungameid/{g.appid}")
    return {"launched": g.name, "appid": g.appid}


def open_library():
    starting = _open_in_steam("steam://nav/games")
    return {"opened": "library", "starting": starting}


def store_search(query: str):
    url = "https://store.steampowered.com/search/?term=" + urllib.parse.quote_plus(query)
    starting = _open_in_steam("steam://openurl/" + url)
    return {"searched": query, "starting": starting}


def uninstall(name: str):
    g = _find(name)
    _open(f"steam://uninstall/{g.appid}")
    return {"uninstalling": g.name, "size": g.size, "size_text": _gb(g.size)}


def game_info(name: str):
    g = _find(name)
    return dict(g.to_dict(), size_text=_gb(g.size))


def register_steam_tools(registry):
    tools = [
        Tool("steam.list_games", "List the games installed through Steam, with their sizes",
             {}, PermissionLevel.LOW, list_games,
             parameters={"sort": P("string", "name / size / recent", required=False, default="name",
                                   enum=["name", "size", "recent"])},
             llm_exposed=True, category="steam"),
        Tool("steam.launch", "Start an installed Steam game by name", {"name": "string"},
             PermissionLevel.MEDIUM, launch, parameters={"name": P("string", "game name")},
             llm_exposed=True, category="steam"),
        Tool("steam.open_library", "Open the Steam library", {}, PermissionLevel.LOW, open_library,
             parameters={}, llm_exposed=True, category="steam"),
        Tool("steam.store_search", "Search the Steam store", {"query": "string"}, PermissionLevel.LOW,
             store_search, parameters={"query": P("string")}, llm_exposed=True, category="steam"),
        Tool("steam.game_info", "Size and location of an installed Steam game", {"name": "string"},
             PermissionLevel.LOW, game_info, parameters={"name": P("string")}, llm_exposed=True, category="steam"),
        # Always asks first (core/permissions.ALWAYS_CONFIRM); Steam asks again itself.
        Tool("steam.uninstall", "Uninstall a Steam game (Steam asks to confirm too)", {"name": "string"},
             PermissionLevel.HIGH, uninstall, parameters={"name": P("string")}, category="steam"),
    ]
    for t in tools:
        t.availability = _available
        registry.register(t)

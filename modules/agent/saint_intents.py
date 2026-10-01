"""
modules/agent/saint_intents.py

Voice commands about SAINT's own window:

    "open the dashboard" / "go to history" / "show me SAINT's settings"
    "turn off the mini player" / "show the mini player"
    "hide the halo" / "halo always on" / "open the overlay" / "turn off action notices"
    "dark mode" / "switch to light theme"
    "minimize yourself" / "show yourself"

"the mini player" on its own means SAINT's now-playing widget. It means
YouTube's miniplayer only when YouTube is named, or YouTube is being watched
and the user didn't say "SAINT's" / "your" (modules/desktop/youtube.py).
Bare "open settings" / "open music" keep meaning Windows Settings / the app;
SAINT's page needs "go to", "SAINT", "your" or "page/tab".
"""

import re
from typing import Optional

from modules.agent.router import Intent, Reply, _clean, run_tool

_GAME_MODE = re.compile(r"\bgam(?:e|ing) mode\b")


def _game_mode(t: str) -> Intent:
    """"game mode on/off", "is game mode on?" (core/game_mode.py)."""
    def run() -> Reply:
        from core.game_mode import game_mode
        if re.match(r"^(?:is|are you in|what'?s)\b", t) or t.endswith(("status", "on or off")):
            return Reply(f"Game Mode is on for {game_mode.game}." if game_mode.active else "Game Mode is off.")
        value = _value(t)
        on = (not game_mode.active) if value == "toggle" else value == "on"
        game_mode.set_manual(True if on else False)
        if on:
            return Reply("Game Mode on. The Halo and pop-ups are off; I'm still listening.")
        return Reply("Game Mode off.")
    return Intent("ui.game_mode", run, "ui")

_WHEN_WINDOWS = (r"(?:(?:automatically\s+)?(?:when|with|at|on)\s+(?:windows\s+)?(?:starts?|start ?up|boots?|boot ?up|"
                 r"log ?in|login|sign ?in|windows)|automatically|at startup|on startup)")
_AUTOSTART = re.compile(
    rf"^(?:(?:please|can you|could you)\s+)?(?P<off>don'?t |do not |stop )?(?:start|launch|open|run|boot)(?:ing)?\s+"
    rf"(?:saint\s+|yourself\s+|up\s+)?{_WHEN_WINDOWS}$|"
    rf"^(?:(?:please|can you|could you)\s+)?(?:add|put)\s+(?:saint|yourself|you|this app)\s+(?:to|in|into)\s+(?:the\s+|my\s+)?"
    rf"(?:windows\s+)?startup(?:\s+apps|\s+programs|\s+folder)?$|"
    rf"^(?:(?:please|can you|could you)\s+)?(?P<off2>remove|take)\s+(?:saint|yourself|you)\s+(?:out of|from|off)\s+(?:the\s+|my\s+)?"
    rf"(?:windows\s+)?startup(?:\s+apps|\s+programs|\s+folder)?$")
_OWN = r"(?:saint'?s?|your|the saint)"
_OFF = r"\b(?:off|hide|close|disable|remove|get rid of|stop showing|turn off|switch off|dismiss)\b"
_ON = r"\b(?:on|show|open|enable|bring up|bring back|turn on|switch on|put up|display)\b"
_TOGGLE = r"\btoggle\b"


def _value(t: str) -> str:
    if re.search(_TOGGLE, t):
        return "toggle"
    if re.search(_OFF, t):
        return "off"
    if re.search(_ON, t):
        return "on"
    return "toggle"


def _set(feature: str, value: str, spoken: str) -> Intent:
    def run():
        return run_tool("ui.set", f"change {spoken}", lambda r: _said(feature, value), feature=feature, value=value)
    return Intent(f"ui.{feature}", run, "ui")


def _said(feature: str, value: str) -> str:
    if feature == "lyrics":
        return {"on": "Lyrics on — they're in the mini player.", "off": "Lyrics off."}.get(value, "Toggled the lyrics.")
    names = {"mini_player": "the mini player", "halo": "the halo", "overlay": "the overlay",
             "action_notices": "action notices", "theme": "the theme"}
    name = names.get(feature, feature)
    if feature == "theme":
        return {"dark": "Dark mode.", "light": "Light mode.", "system": "Following the system theme."}.get(
            value, "Switched the theme.")
    if feature == "halo" and value in ("always", "minimized"):
        return "Halo always on." if value == "always" else "The halo shows while I'm minimized."
    if value == "toggle":
        return f"Toggled {name}."
    return f"{name[0].upper()}{name[1:]} {'on' if value == 'on' else 'off'}."


_PAGE_WORDS = (r"dashboard|home(?:\s+page)?|history|stats|statistics|automations?|scenes|memor(?:y|ies)|activity|"
               r"console|logs|music|system|storage|devices|settings|preferences")
_PAGE = re.compile(
    rf"^(?P<verb>open|go to|show(?: me)?|take me to|switch to|navigate to|bring up|pull up|jump to|back to|"
    rf"go back to)\s+(?:the\s+|my\s+|your\s+|{_OWN}\s+)?(?P<page>{_PAGE_WORDS})"
    rf"(?P<suffix>\s+(?:page|tab|screen|section|panel))?(?P<own>\s+(?:in|on|of)\s+saint)?$")
# Pages whose names mean something else on their own ("open settings" = Windows Settings).
_AMBIGUOUS = {"settings", "preferences", "music", "system", "memory", "memories", "activity", "home", "storage",
              "logs", "console", "devices"}


def parse_saint_ui(text: str) -> Optional[Intent]:
    t = _clean(text).lower().strip(" .!?")
    if not t or len(t.split()) > 12:
        return None
    own = bool(re.search(rf"\b{_OWN}\b|\byourself\b", t))

    # ---- start with Windows (core/autostart.py) ------------------------------------------------
    m = _AUTOSTART.match(t)
    if m:
        on = not (m.group("off") or m.group("off2"))

        def run_autostart():
            from core import autostart
            ok, message = autostart.set_enabled(on)
            return Reply(message, ok=ok)
        return Intent("ui.autostart", run_autostart, "ui")

    # ---- lyrics (in the mini player) ---------------------------------------------------------
    # "turn on the lyrics for Spotify", "show lyrics", "hide the lyrics" — not "look up the lyrics to X".
    if re.search(r"\blyrics?\b", t) and not re.search(
            r"\b(?:search|google|look up|find|what are|what're|what is|what's|write|meaning|mean|translate|"
            r"read|sing|to the song|of the song|for the song)\b", t) and len(t.split()) <= 9:
        return _set("lyrics", _value(t), "the lyrics")

    # ---- mini player -----------------------------------------------------------------------
    if re.search(r"\bmini ?player\b|\bnow playing widget\b|\bmusic widget\b|\bspotify widget\b", t):
        if re.search(r"\b(youtube|video|clip)\b", t):
            return None
        if not own:
            try:
                from modules.desktop import youtube
                if youtube.is_watching():
                    return None            # "mini player" while watching YouTube means the video
            except Exception:
                pass
        return _set("mini_player", _value(t), "the mini player")

    # ---- game mode (before "halo": "game mode, hide the halo" is about the game) --------------
    if _GAME_MODE.search(t) and not re.search(r"\b(?:scene|workspace)\b", t):
        return _game_mode(t)

    # ---- halo / overlay / action notices -------------------------------------------------------
    if re.search(r"\bhalo\b", t):
        if re.search(r"\balways\b", t):
            return _set("halo", "always", "the halo")
        if re.search(r"\b(?:when|while) (?:you'?re |you are )?minimi[sz]ed\b|\bminimi[sz]ed\b", t):
            return _set("halo", "minimized", "the halo")
        v = _value(t)
        return _set("halo", {"on": "always"}.get(v, v), "the halo")
    if re.search(r"\boverlay\b", t) and not re.search(r"\b(game|steam|discord|nvidia|xbox)\b", t):
        return _set("overlay", _value(t), "the overlay")
    if re.search(r"\b(?:action )?notices\b|\baction (?:notifications?|pills?)\b", t):
        return _set("action_notices", _value(t), "action notices")

    # ---- theme --------------------------------------------------------------------------------
    m = re.search(r"^(?:switch to |go |turn on |use |change to |set (?:the )?theme to |enable )?(dark|light)"
                  r"(?: mode| theme)?$|^(?:switch|change|flip|toggle) (?:the )?(?:theme|mode)$", t)
    if m:
        return _set("theme", m.group(1) or "toggle", "the theme")

    # ---- SAINT's window --------------------------------------------------------------------
    if re.search(r"^(?:minimi[sz]e|hide)\s+(?:yourself|saint|the saint window|your window)$", t):
        action = "minimize" if t.startswith("minimi") else "hide"
        return Intent("ui.window", lambda: run_tool("ui.window", f"{action} my window",
                                                     lambda r: "Minimized." if action == "minimize" else "Hidden.",
                                                     action=action), "ui")
    if re.search(r"^(?:show|open|bring up|pull up|restore)\s+(?:yourself|saint|the saint window|your window)$", t):
        return Intent("ui.window", lambda: run_tool("ui.window", "show my window", lambda r: "Here I am.",
                                                     action="show"), "ui")

    # ---- pages ------------------------------------------------------------------------------
    m = _PAGE.match(t)
    if m:
        page = m.group("page")
        qualified = own or m.group("suffix") or m.group("own") or m.group("verb") in (
            "go to", "take me to", "navigate to", "go back to", "back to", "jump to")
        if page.split()[0] in _AMBIGUOUS and not qualified:
            return None
        from modules.ui_control.tools import page_for
        key = re.sub(r"\s+page$", "", page)
        key = {"stats": "history", "statistics": "history", "scenes": "automations", "memories": "memory",
               "automation": "automations"}.get(key, key)
        target = page_for(key)

        def run_nav():
            label = "the dashboard" if page == "dashboard" else f"the {target} page"
            return run_tool("ui.navigate", f"open {label}", lambda r: f"Opened {label}.", page=key)
        return Intent("ui.navigate", run_nav, "ui")
    return None

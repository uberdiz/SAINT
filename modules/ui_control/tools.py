"""
modules/ui_control/tools.py

Voice control of SAINT's own window: "open the dashboard", "go to history",
"turn off the mini player", "hide the halo", "dark mode", "minimize yourself".

The tools only send a UI_COMMAND (core/ui_link.py); MainWindow performs it on
the GUI thread and reports back.
"""

from modules.automation.tools import P, PermissionLevel, Tool, ToolError

PAGES = ("Home", "Music", "Automations", "History", "Memory", "Activity", "Storage", "System", "Devices", "Settings")
_PAGE_ALIASES = {"dashboard": "Home", "home": "Home", "main": "Home", "music": "Music", "spotify": "Music",
                 "automations": "Automations", "automation": "Automations", "scenes": "Automations",
                 "reminders": "Automations", "history": "History", "stats": "History", "memory": "Memory",
                 "memories": "Memory", "activity": "Activity", "console": "Activity", "logs": "Activity",
                 "system": "System", "devices": "Devices", "device": "Devices", "link": "Devices", "phone": "Devices",
                 "collaborators": "Devices", "storage": "Storage", "junk": "Storage", "disk space": "Storage", "drives": "Storage", "settings": "Settings", "preferences": "Settings"}
FEATURES = ("mini_player", "lyrics", "halo", "overlay", "action_notices", "theme")


def page_for(name: str) -> str:
    key = (name or "").strip().lower()
    if key in _PAGE_ALIASES:
        return _PAGE_ALIASES[key]
    for p in PAGES:
        if p.lower() == key:
            return p
    raise ToolError(f"SAINT doesn't have a {name} page.", "NOT_FOUND")


def _available():
    from core.ui_link import ui_link
    return (True, "") if ui_link.attached else (False, "SAINT's window isn't running.")


def _send(cmd: str, **args) -> dict:
    from core.ui_link import ui_link
    ok, message = ui_link.send(cmd, **args)
    if not ok:
        raise ToolError(message or "SAINT's window didn't respond.", "UI_UNAVAILABLE")
    return {"done": cmd, **args, "message": message}


def navigate(page):
    return _send("navigate", page=page_for(page))


def set_feature(feature, value):
    feature = (feature or "").strip().lower().replace(" ", "_")
    if feature not in FEATURES:
        raise ToolError(f"I can't change {feature}.", "INVALID")
    value = str(value).strip().lower()
    if feature == "halo" and value not in ("off", "minimized", "always", "on", "toggle"):
        raise ToolError("The halo can be off, minimized or always.", "INVALID")
    if feature == "theme" and value not in ("dark", "light", "system", "toggle"):
        raise ToolError("The theme can be dark, light or system.", "INVALID")
    if feature in ("mini_player", "lyrics", "action_notices", "overlay") and value not in ("on", "off", "toggle"):
        raise ToolError("Say on, off or toggle.", "INVALID")
    return _send("set", feature=feature, value=value)


def window(action):
    action = (action or "").strip().lower()
    if action not in ("show", "minimize", "hide"):
        raise ToolError("I can show, minimize or hide my window.", "INVALID")
    return _send("window", action=action)


def register_ui_tools(registry):
    tools = [
        Tool("ui.navigate", "Open a page in SAINT's own window (dashboard/home, music, automations, history, "
             "memory, activity, system, settings)",
             {"page": "string"}, PermissionLevel.LOW, navigate,
             parameters={"page": P("string", "page name; 'dashboard' means Home")},
             llm_exposed=True, category="ui"),
        Tool("ui.set", "Turn one of SAINT's own features on or off: the mini player, song lyrics (shown in the "
             "mini player), the halo, the overlay, "
             "action notices, or the theme",
             {"feature": "string", "value": "string"}, PermissionLevel.LOW, set_feature,
             parameters={"feature": P("string", "which feature", enum=list(FEATURES)),
                         "value": P("string", "on/off/toggle; halo: off/minimized/always; theme: dark/light")},
             llm_exposed=True, category="ui"),
        Tool("ui.window", "Show, minimize or hide SAINT's own window",
             {"action": "string"}, PermissionLevel.LOW, window,
             parameters={"action": P("string", enum=["show", "minimize", "hide"])},
             llm_exposed=True, category="ui"),
    ]
    for t in tools:
        t.availability = _available
        registry.register(t)

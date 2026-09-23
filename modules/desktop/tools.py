"""Desktop-control tools registered in SAINT's shared tool registry."""

from core.config import config
from modules.automation.tools import Tool, PermissionLevel, P, ToolRegistry


def _available():
    if not config.get("modules.desktop", True):
        return False, "Desktop control is turned off in the Module Manager."
    if not config.get("desktop.enabled", True):
        return False, "Desktop control is turned off in Settings."
    return True, ""


def close_permission() -> PermissionLevel:
    return PermissionLevel.HIGH if config.get("desktop.confirm_close_apps", True) else PermissionLevel.MEDIUM


def register_desktop_tools(registry: ToolRegistry):
    from modules.desktop.controller import desktop
    from modules.desktop import uia

    def open_app(name):
        return desktop.open_app(name)

    def close_app(name):
        return desktop.close(name)

    def focus_window(name):
        return desktop.focus(name).to_dict()

    def list_windows():
        return {"windows": [w.to_dict() for w in desktop.list_windows()],
                "monitors": [{"index": m.index, "width": m.width, "height": m.height, "primary": m.primary}
                             for m in desktop.monitors()]}

    def move_window(window, monitor):
        return desktop.move_to_monitor(window, monitor).to_dict()

    def arrange_window(window, action):
        return desktop.arrange(window, action).to_dict()

    def resize_window(window, width, height):
        return desktop.resize(window, width, height).to_dict()

    def type_text(text, target=None, press_enter=False):
        return desktop.type_text(text, target=target, press_enter=press_enter)

    def press_keys(keys):
        return desktop.press_keys(keys)

    def mouse_move(x, y, duration=0.2):
        return desktop.mouse_move(x, y, duration)

    def mouse_click(x=None, y=None, button="left", clicks=1):
        return desktop.mouse_click(x, y, button, clicks)

    def scroll(amount):
        return desktop.scroll(amount)

    def click_element(name):
        from modules.desktop.controller import _require
        _require("allow_mouse", "Mouse control")
        return uia.click_element(name)

    def list_ui_elements(limit=60):
        return uia.list_elements(limit=limit)

    win_param = P("string", "window title or app name; 'this' for the active window")
    tools = [
        Tool("desktop.open_app", "Open an installed application by name (e.g. Chrome, Discord, Spotify)",
             {"name": "string"}, PermissionLevel.MEDIUM, open_app,
             parameters={"name": P("string", "application name")}, llm_exposed=True, category="desktop"),
        Tool("desktop.close_app", "Close an application window (asks for confirmation)",
             {"name": "string"}, close_permission(), close_app,
             parameters={"name": win_param}, llm_exposed=True, category="desktop"),
        Tool("desktop.focus_window", "Switch to / bring an application window to the front",
             {"name": "string"}, PermissionLevel.LOW, focus_window,
             parameters={"name": win_param}, llm_exposed=True, category="desktop"),
        Tool("desktop.list_windows", "List open windows and monitors", {}, PermissionLevel.LOW, list_windows,
             parameters={}, llm_exposed=True, category="desktop"),
        Tool("desktop.move_window", "Move a window to another monitor",
             {"window": "string", "monitor": "string"}, PermissionLevel.LOW, move_window,
             parameters={"window": win_param,
                         "monitor": P("string", "monitor number (1, 2, ...) or next/left/right/primary")},
             llm_exposed=True, category="desktop"),
        Tool("desktop.arrange_window", "Maximize, minimize, restore, center or snap a window left/right",
             {"window": "string", "action": "string"}, PermissionLevel.LOW, arrange_window,
             parameters={"window": win_param,
                         "action": P("string", "what to do", enum=["maximize", "minimize", "restore",
                                                                   "snap_left", "snap_right", "center"])},
             llm_exposed=True, category="desktop"),
        Tool("desktop.resize_window", "Resize a window (pixels)",
             {"window": "string", "width": "int", "height": "int"}, PermissionLevel.LOW, resize_window,
             parameters={"window": win_param, "width": P("integer", minimum=200, maximum=10000),
                         "height": P("integer", minimum=150, maximum=10000)},
             category="desktop"),
        Tool("desktop.type_text", "Type text into the active window, optionally into a named field first",
             {"text": "string", "target": "string (optional)"}, PermissionLevel.MEDIUM, type_text,
             parameters={"text": P("string", "text to type"),
                         "target": P("string", "input to focus first, e.g. 'search box'", required=False),
                         "press_enter": P("boolean", "press Enter afterwards", required=False, default=False)},
             llm_exposed=True, category="desktop"),
        Tool("desktop.press_keys", "Press a key or shortcut such as ctrl+t or enter",
             {"keys": "string"}, PermissionLevel.MEDIUM, press_keys,
             parameters={"keys": P("string", "e.g. ctrl+shift+t, enter, volumeup")},
             llm_exposed=True, category="desktop"),
        Tool("desktop.click_element", "Click a visible button/link/item in the active window by its label",
             {"name": "string"}, PermissionLevel.MEDIUM, click_element,
             parameters={"name": P("string", "label of the element")}, llm_exposed=True, category="desktop"),
        Tool("desktop.list_ui_elements", "Describe the interactive elements of the active window",
             {}, PermissionLevel.LOW, list_ui_elements,
             parameters={"limit": P("integer", required=False, default=60, minimum=1, maximum=200)},
             llm_exposed=True, category="desktop"),
        Tool("desktop.mouse_move", "Move the mouse to screen coordinates", {"x": "int", "y": "int"},
             PermissionLevel.LOW, mouse_move,
             parameters={"x": P("integer"), "y": P("integer"),
                         "duration": P("number", required=False, default=0.2, minimum=0, maximum=2)},
             category="desktop"),
        Tool("desktop.mouse_click", "Click the mouse (at coordinates or where it is)",
             {"x": "int (optional)", "y": "int (optional)"}, PermissionLevel.MEDIUM, mouse_click,
             parameters={"x": P("integer", required=False), "y": P("integer", required=False),
                         "button": P("string", required=False, default="left", enum=["left", "right", "middle"]),
                         "clicks": P("integer", required=False, default=1, minimum=1, maximum=3)},
             category="desktop"),
        Tool("desktop.scroll", "Scroll the mouse wheel (positive = up)", {"amount": "int"},
             PermissionLevel.LOW, scroll, parameters={"amount": P("integer", minimum=-50, maximum=50)},
             category="desktop"),
    ]
    for t in tools:
        t.availability = _available
        registry.register(t)

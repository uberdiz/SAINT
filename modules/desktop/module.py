"""Desktop control module: apps, windows, keyboard, mouse, UI elements."""

from core.events import event_bus, EventType
from modules.base import BaseModule


class DesktopModule(BaseModule):
    name = "Desktop"
    description = "Controlled desktop automation: open/close/switch apps, move windows, type, click UI elements."

    def __init__(self):
        super().__init__()
        self.subtasks = {
            "Open / Close Apps": True,
            "Window Switching": True,
            "Move / Snap Windows": True,
            "Keyboard Input": True,
            "Mouse Control": True,
            "UI Element Interaction": True,
            "Safety Validation": True,
            "Multi-step Plans": True,
        }

    def enable(self):
        super().enable()
        from modules.automation.tools import get_tool_registry
        from modules.desktop.apps import app_catalog
        get_tool_registry()          # registers desktop tools
        app_catalog.warm()           # build the installed-app catalogue in the background
        if not getattr(self, "_connected", False):
            event_bus.subscribe(self._on_event)
            self._connected = True

    def _on_event(self, ev):
        if ev.type == EventType.SETTINGS_CHANGED:
            from modules.automation.tools import get_tool_registry
            from modules.desktop.tools import close_permission
            tool = get_tool_registry().get("desktop.close_app")
            if tool:
                tool.permission = close_permission()

"""
modules/automation/module.py

Automation module with tool registry for Windows desktop control.
"""

from modules.base import BaseModule
from modules.automation.tools import (
    ToolRegistry,
    Tool,
    ToolResult,
    PermissionLevel,
    get_tool_registry,
)
from core.events import event_bus, EventType
from core.config import config


class AutomationModule(BaseModule):
    name = "Automation"
    description = "Desktop automation: keyboard, mouse, window control, file operations, shell commands."

    def __init__(self):
        super().__init__()
        self.subtasks = {
            "Tool Registry": True,
            "Mouse Control": True,
            "Keyboard Control": True,
            "Window Management": True,
            "Screenshot Capture": True,
            "File Operations": True,
            "Shell Commands": True,
            "Permission System": True,
        }
        self._registry: ToolRegistry = None

    def enable(self):
        super().enable()
        self._registry = get_tool_registry()
        event_bus.emit_event(EventType.MODULE_ENABLED, {"module": self.name})

    def disable(self):
        super().disable()
        event_bus.emit_event(EventType.MODULE_DISABLED, {"module": self.name})

    # ------------------------------------------------------------------ #
    # Tool Registry Access
    # ------------------------------------------------------------------ #

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    def list_tools(self) -> list:
        """List all available tools."""
        return [{"name": t.name, "description": t.description, "permission": t.permission.value, "enabled": t.enabled}
                for t in self._registry.list_tools()]

    def get_tool(self, name: str) -> dict:
        """Get tool details."""
        tool = self._registry.get(name)
        if not tool:
            return {}
        return {
            "name": tool.name,
            "description": tool.description,
            "arguments": tool.arguments,
            "permission": tool.permission.value,
            "enabled": tool.enabled,
        }

    def execute_tool(self, name: str, **kwargs) -> dict:
        """Execute a tool by name."""
        result = self._registry.execute(name, **kwargs)
        return {"success": result.success, "result": result.result, "error": result.error}

    def set_tool_enabled(self, name: str, enabled: bool) -> bool:
        """Enable or disable a tool."""
        tool = self._registry.get(name)
        if tool:
            tool.enabled = enabled
            return True
        return False

    def get_permission_mode(self) -> str:
        """Get current permission mode from config."""
        return config.get("automation.permission_mode", "confirm")

    def set_permission_mode(self, mode: str) -> bool:
        """Set permission mode (safe, confirm, autonomous)."""
        if mode in ("safe", "confirm", "autonomous"):
            config.set("automation.permission_mode", mode, persist=True)
            return True
        return False

    def check_permission(self, tool_name: str) -> bool:
        """Check if a tool can be executed based on permission mode."""
        mode = self.get_permission_mode()
        tool = self._registry.get(tool_name)
        if not tool:
            return False
        
        if mode == "safe":
            return tool.permission == PermissionLevel.LOW
        elif mode == "confirm":
            # In confirm mode, all tools require confirmation (handled by caller)
            return True
        elif mode == "autonomous":
            # In autonomous mode, all tools allowed except HIGH requires confirmation
            return tool.permission != PermissionLevel.HIGH
        return False
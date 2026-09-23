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
    description = "Tool registry, reminders and scheduled automations, background tasks."

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
            "Reminders & Schedules": True,
            "Persistent Automations": True,
        }
        self._registry: ToolRegistry = None

    def enable(self):
        super().enable()
        self._registry = get_tool_registry()
        self._register_automation_tools()

    def disable(self):
        super().disable()

    # ------------------------------------------------------------------ #
    # Reminder / automation tools (scheduler lives in scheduler.py)
    # ------------------------------------------------------------------ #
    def _register_automation_tools(self):
        from modules.automation.tools import P, ToolError
        from modules.automation.scheduler import scheduler
        from modules.automation.timeparse import parse_schedule, describe

        def available():
            if not config.get("modules.automation", True) or not config.get("automation.enabled", True):
                return False, "Automations are turned off in Settings."
            return True, ""

        def _schedule(when):
            sched, _ = parse_schedule(when)
            if sched is None:
                raise ToolError(f"I couldn't understand the time '{when}'. Try 'at 5 PM', 'in 30 minutes' "
                                "or 'every weekday at 8'.", "BAD_TIME")
            return sched

        def create_reminder(message, when):
            try:
                a = scheduler.create("reminder", message.strip(), _schedule(when))
            except ValueError as e:
                raise ToolError(str(e), "BAD_TIME")
            return a.to_dict()

        def schedule_command(command, when, condition=None):
            cond = {"type": condition} if condition else None
            try:
                a = scheduler.create("command", command.strip(), _schedule(when), condition=cond)
            except ValueError as e:
                raise ToolError(str(e), "BAD_TIME")
            return a.to_dict()

        def list_automations():
            return {"automations": [a.to_dict() for a in scheduler.list(active_only=True)]}

        def cancel(id=None, query=None):
            if id:
                a = scheduler.cancel(id)
                if not a:
                    raise ToolError(f"There's no automation {id}.", "NOT_FOUND")
                return {"cancelled": [a.to_dict()]}
            hits = scheduler.find(query or "")
            if not hits:
                raise ToolError("I couldn't find a matching reminder.", "NOT_FOUND")
            if len(hits) > 1 and query:
                raise ToolError("That matches several: " + "; ".join(
                    f"{h.title} ({h.describe()})" for h in hits[:4]) + ". Which one?", "AMBIGUOUS")
            if len(hits) > 1:
                raise ToolError(f"You have {len(hits)} active reminders; tell me which one to cancel.", "AMBIGUOUS")
            return {"cancelled": [scheduler.cancel(hits[0].id).to_dict()]}

        def pause(id, paused=True):
            a = scheduler.set_paused(id, paused)
            if not a:
                raise ToolError(f"There's no active automation {id}.", "NOT_FOUND")
            return a.to_dict()

        conds = ["spotify_playing", "spotify_not_playing"]
        tools = [
            Tool("automation.create_reminder", "Create a reminder that SAINT announces at a time",
                 {"message": "string", "when": "string"}, PermissionLevel.LOW, create_reminder,
                 parameters={"message": P("string", "what to remind the user about"),
                             "when": P("string", "natural time, e.g. 'at 5 PM', 'in 30 minutes', 'every weekday at 8'")},
                 llm_exposed=True, category="automation"),
            Tool("automation.schedule_command", "Schedule a spoken-style command to run later or repeatedly "
                 "(e.g. 'play my focus playlist' every weekday at 9)",
                 {"command": "string", "when": "string"}, PermissionLevel.MEDIUM, schedule_command,
                 parameters={"command": P("string", "the command SAINT should run"),
                             "when": P("string", "natural time expression"),
                             "condition": P("string", "only run if", required=False, enum=conds)},
                 llm_exposed=True, category="automation"),
            Tool("automation.list", "List active reminders and automations", {}, PermissionLevel.LOW,
                 list_automations, parameters={}, llm_exposed=True, category="automation"),
            Tool("automation.cancel", "Cancel a reminder/automation by id or description",
                 {"id": "string (optional)", "query": "string (optional)"}, PermissionLevel.LOW, cancel,
                 parameters={"id": P("string", required=False), "query": P("string", required=False)},
                 llm_exposed=True, category="automation"),
            Tool("automation.pause", "Pause or resume an automation", {"id": "string"}, PermissionLevel.LOW, pause,
                 parameters={"id": P("string"), "paused": P("boolean", required=False, default=True)},
                 category="automation"),
        ]
        for t in tools:
            t.availability = available
            self._registry.register(t)

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

    def execute_tool(self, name: str, confirmed: bool = False, **kwargs) -> dict:
        """Execute a tool by name (``confirmed`` = the user already approved it)."""
        result = self._registry.execute(name, _confirmed=confirmed, **kwargs)
        return {"success": result.success, "result": result.result, "error": result.error,
                "error_code": result.error_code}

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
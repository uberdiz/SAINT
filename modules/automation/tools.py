"""
modules/automation/tools.py

SAINT's single tool registry.

Every action SAINT can take — Spotify playback, memory, reminders, desktop
control, file access — is a ``Tool`` registered here. A tool declares:

    name          dotted identifier, e.g. "spotify.play_track"
    description   what it does (shown to the LLM and in Settings)
    parameters    typed schema used for validation and for LLM function calling
    permission    LOW / MEDIUM / HIGH risk level
    execute_fn    the implementation
    llm_exposed   whether the LLM may call it (shell/file tools never are)
    cancellable   accepts a ``cancel_event`` keyword

``ToolRegistry.execute`` validates parameters, applies the permission policy
(allow / confirm / deny), emits TOOL_* events for the UI and logs, and turns
exceptions into a structured ``ToolResult`` — callers never see a raw
traceback, and SAINT never claims an action happened unless the tool
returned success.
"""

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from modules.automation.tasks import task_manager

log = logging.getLogger("saint.tools")


class PermissionLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolError(Exception):
    """Raised by tool implementations with a clean, user-facing message."""

    def __init__(self, message: str, code: str = "TOOL_ERROR"):
        super().__init__(message)
        self.code = code


def P(type_: str, description: str = "", required: bool = True, enum=None,
      minimum=None, maximum=None, default=None, items: str = "string") -> Dict[str, Any]:
    """Shorthand for a parameter spec."""
    spec: Dict[str, Any] = {"type": type_, "description": description, "required": required}
    if enum is not None:
        spec["enum"] = list(enum)
    if minimum is not None:
        spec["minimum"] = minimum
    if maximum is not None:
        spec["maximum"] = maximum
    if default is not None:
        spec["default"] = default
    if type_ == "array":
        spec["items"] = items
    return spec


@dataclass
class Tool:
    name: str
    description: str
    arguments: Dict[str, Any]              # legacy human-readable argument notes
    permission: PermissionLevel
    execute_fn: Callable
    enabled: bool = True
    parameters: Optional[Dict[str, Dict[str, Any]]] = None
    llm_exposed: bool = False
    category: str = ""
    cancellable: bool = False
    # Optional () -> (available: bool, reason: str), e.g. "Spotify not connected"
    availability: Optional[Callable[[], Tuple[bool, str]]] = None

    def to_llm_schema(self) -> Dict[str, Any]:
        props, required = {}, []
        for pname, spec in (self.parameters or {}).items():
            t = spec.get("type", "string")
            prop: Dict[str, Any] = {"type": t, "description": spec.get("description", "")}
            if "enum" in spec:
                prop["enum"] = spec["enum"]
            if t == "array":
                prop["items"] = {"type": spec.get("items", "string")}
            props[pname] = prop
            if spec.get("required", True):
                required.append(pname)
        return {
            "type": "function",
            "function": {
                "name": llm_name(self.name),
                "description": self.description,
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        }


def llm_name(tool_name: str) -> str:
    """LLM function names cannot contain dots."""
    return tool_name.replace(".", "__")


@dataclass
class ToolResult:
    success: bool
    result: Any = None
    error: Optional[str] = None
    error_code: Optional[str] = None  # e.g. NO_ACTIVE_DEVICE, INVALID_PARAMS, CONFIRM_REQUIRED
    tool: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"success": self.success, "result": self.result, "error": self.error,
                "error_code": self.error_code, "tool": self.tool}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


def _coerce(name: str, value: Any, spec: Dict[str, Any]) -> Any:
    t = spec.get("type", "string")
    if t == "string":
        if isinstance(value, (dict, list)):
            raise ValueError(f"'{name}' must be text")
        value = str(value)
    elif t == "integer":
        if isinstance(value, bool):
            raise ValueError(f"'{name}' must be a whole number")
        try:
            value = int(float(value))
        except (TypeError, ValueError):
            raise ValueError(f"'{name}' must be a whole number")
    elif t == "number":
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"'{name}' must be a number")
    elif t == "boolean":
        if isinstance(value, str):
            low = value.strip().lower()
            if low in _TRUE:
                value = True
            elif low in _FALSE:
                value = False
            else:
                raise ValueError(f"'{name}' must be true or false")
        else:
            value = bool(value)
    elif t == "array":
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"'{name}' must be a list")
        value = list(value)
    elif t == "object":
        if not isinstance(value, dict):
            raise ValueError(f"'{name}' must be an object")
    if "enum" in spec and value not in spec["enum"]:
        raise ValueError(f"'{name}' must be one of: {', '.join(map(str, spec['enum']))}")
    if t in ("integer", "number"):
        if "minimum" in spec and value < spec["minimum"]:
            raise ValueError(f"'{name}' must be at least {spec['minimum']}")
        if "maximum" in spec and value > spec["maximum"]:
            raise ValueError(f"'{name}' must be at most {spec['maximum']}")
    return value


def validate_args(tool: Tool, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and coerce kwargs against the tool schema. Raises ValueError."""
    if tool.parameters is None:
        return kwargs
    unknown = set(kwargs) - set(tool.parameters)
    if unknown:
        raise ValueError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
    out: Dict[str, Any] = {}
    for pname, spec in tool.parameters.items():
        if pname in kwargs and kwargs[pname] is not None and kwargs[pname] != "":
            out[pname] = _coerce(pname, kwargs[pname], spec)
        elif "default" in spec:
            out[pname] = spec["default"]
        elif spec.get("required", True):
            raise ValueError(f"missing required parameter '{pname}'")
    return out


def _summarize_args(args: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in args.items():
        s = v if isinstance(v, (int, float, bool)) else str(v)
        if isinstance(s, str) and len(s) > 80:
            s = s[:77] + "..."
        out[k] = s
    return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class ToolRegistry:
    """Registry and executor for every SAINT tool."""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}
        self._lock = threading.Lock()

    def register(self, tool: Tool):
        with self._lock:
            self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._tools.pop(name, None) is not None

    def get(self, name: str) -> Optional[Tool]:
        with self._lock:
            return self._tools.get(name)

    def list_tools(self) -> List[Tool]:
        with self._lock:
            return list(self._tools.values())

    def llm_tools(self) -> List[Tool]:
        return [t for t in self.list_tools() if t.llm_exposed and t.enabled
                and (t.availability is None or t.availability()[0])]

    def by_llm_name(self, name: str) -> Optional[Tool]:
        for t in self.list_tools():
            if llm_name(t.name) == name or t.name == name:
                return t
        return None

    def execute(self, tool_name: str, /, _confirmed: bool = False,
                _cancel_event: Optional[threading.Event] = None, **kwargs) -> ToolResult:
        # tool_name is positional-only so tools may have a parameter called "name".
        from core.permissions import permission_manager
        from core.events import event_bus, EventType

        name = tool_name
        tool = self.get(name)
        if not tool:
            return ToolResult(False, error=f"I don't have a tool called '{name}'.",
                              error_code="UNKNOWN_TOOL", tool=name)
        if not tool.enabled:
            return ToolResult(False, error=f"Tool '{name}' is disabled.",
                              error_code="DISABLED", tool=name)
        if tool.availability is not None:
            try:
                ok, reason = tool.availability()
            except Exception as e:  # pragma: no cover - defensive
                ok, reason = False, str(e)
            if not ok:
                return ToolResult(False, error=reason, error_code="UNAVAILABLE", tool=name)
        try:
            args = validate_args(tool, kwargs)
        except ValueError as e:
            log.info("tool.invalid_params tool=%s error=%s args=%s", name, e, _summarize_args(kwargs))
            event_bus.emit_event(EventType.TOOL_FAILED, {"tool": name, "error": f"invalid parameters: {e}"})
            return ToolResult(False, error=f"Invalid parameters for {name}: {e}",
                              error_code="INVALID_PARAMS", tool=name, args=kwargs)

        policy = permission_manager.policy_for_tool(name, tool.permission.value)
        event_bus.emit_event(EventType.TOOL_REQUESTED, {
            "tool": name, "permission": tool.permission.value, "policy": policy,
            "args": _summarize_args(args)})
        if policy == permission_manager.DENY:
            event_bus.emit_event(EventType.TOOL_PERMISSION_DENIED, {"tool": name, "reason": "policy"})
            return ToolResult(False, error=f"I'm not allowed to use {name} (blocked in Settings > Permissions).",
                              error_code="DENIED", tool=name, args=args)
        if policy == permission_manager.CONFIRM and not _confirmed:
            event_bus.emit_event(EventType.TOOL_PERMISSION_REQUIRED, {"tool": name,
                                                                      "args": _summarize_args(args)})
            return ToolResult(False, error=f"Confirmation required for {name}.",
                              error_code="CONFIRM_REQUIRED", tool=name, args=args)

        event_bus.emit_event(EventType.TOOL_PERMISSION_GRANTED, {"tool": name})
        started = time.perf_counter()
        event_bus.emit_event(EventType.TOOL_STARTED, {"tool": name, "args": _summarize_args(args)})
        log.info("tool.start %s %s", name, _summarize_args(args))
        call_args = dict(args)
        if tool.cancellable and _cancel_event is not None:
            call_args["cancel_event"] = _cancel_event
        try:
            result = tool.execute_fn(**call_args)
            ms = round((time.perf_counter() - started) * 1000, 1)
            event_bus.emit_event(EventType.TOOL_COMPLETED, {"tool": name, "duration_ms": ms})
            log.info("tool.ok %s (%.0f ms)", name, ms)
            return ToolResult(True, result=result, tool=name, args=args, duration_ms=ms)
        except ToolError as e:
            ms = round((time.perf_counter() - started) * 1000, 1)
            event_bus.emit_event(EventType.TOOL_FAILED, {"tool": name, "error": str(e), "duration_ms": ms})
            log.info("tool.failed %s code=%s %s", name, e.code, e)
            return ToolResult(False, error=str(e), error_code=e.code, tool=name, args=args, duration_ms=ms)
        except Exception as e:
            ms = round((time.perf_counter() - started) * 1000, 1)
            code = getattr(e, "code", None) or "ERROR"
            user_msg = getattr(e, "user_message", None)
            event_bus.emit_event(EventType.TOOL_FAILED, {"tool": name, "error": str(e), "code": code,
                                                         "duration_ms": ms})
            if user_msg:
                # Expected, typed failure (e.g. Spotify NO_ACTIVE_DEVICE) — no traceback.
                log.info("tool.failed %s code=%s %s", name, code, e)
            else:
                log.exception("tool.error %s", name)
                user_msg = f"Something went wrong running {name}."
            return ToolResult(False, error=user_msg, error_code=code, tool=name, args=args, duration_ms=ms)


_tool_registry: Optional[ToolRegistry] = None
_registry_lock = threading.Lock()


def get_tool_registry() -> ToolRegistry:
    global _tool_registry
    with _registry_lock:
        if _tool_registry is None:
            _tool_registry = ToolRegistry()
            _register_default_tools(_tool_registry)
    return _tool_registry


def _register_default_tools(registry: ToolRegistry):
    """Built-in tools: files, shell (never LLM-exposed), background tasks,
    plus desktop control (modules/desktop)."""

    def read_file(path: str) -> dict:
        path_obj = Path(path)
        if not path_obj.exists():
            raise ToolError(f"File not found: {path}", "NOT_FOUND")
        if path_obj.stat().st_size > 1024 * 1024:
            raise ToolError("File too large (>1MB)", "TOO_LARGE")
        content = path_obj.read_text(encoding='utf-8', errors='replace')
        return {"path": path, "content": content, "size": len(content)}

    def write_file(path: str, content: str, append: bool = False) -> dict:
        path_obj = Path(path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        with open(path_obj, "a" if append else "w", encoding="utf-8") as f:
            f.write(content)
        return {"path": path, "bytes": len(content.encode('utf-8')), "appended": append}

    def list_directory(path: str) -> dict:
        path_obj = Path(path)
        if not path_obj.exists():
            raise ToolError(f"Directory not found: {path}", "NOT_FOUND")
        if not path_obj.is_dir():
            raise ToolError(f"Not a directory: {path}", "INVALID")
        items = []
        for item in path_obj.iterdir():
            try:
                st = item.stat()
            except OSError:
                continue
            items.append({"name": item.name, "is_dir": item.is_dir(),
                          "size": st.st_size if item.is_file() else None, "modified": st.st_mtime})
        return {"path": path, "items": items}

    def search_files(pattern: str, root: str = ".") -> dict:
        root_path = Path(root)
        if not root_path.exists():
            raise ToolError(f"Root path not found: {root}", "NOT_FOUND")
        matches = []
        for m in root_path.rglob(pattern):
            matches.append(str(m))
            if len(matches) >= 100:
                break
        return {"pattern": pattern, "root": root, "matches": matches}

    def run_command(command: str, timeout: int = 30, cwd: Optional[str] = None) -> dict:
        """Run a shell command. HIGH risk: never exposed to the LLM and
        requires confirmation under the default policy."""
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True,
                                    timeout=timeout, cwd=cwd)
            return {"command": command, "exit_code": result.returncode,
                    "stdout": result.stdout, "stderr": result.stderr}
        except subprocess.TimeoutExpired:
            raise ToolError(f"Command timed out after {timeout}s", "TIMEOUT")

    def start_background_task(description: str, command: str, cwd: Optional[str] = None,
                              timeout: int = 300) -> dict:
        task = task_manager.create_command_task(description, command, cwd=cwd, timeout=timeout)
        return {"task_id": task.id, "status": task.status.value, "description": description}

    def background_task_status(task_id: Optional[str] = None) -> dict:
        return {"tasks": task_manager.status(task_id)}

    def cancel_background_task(task_id: str) -> dict:
        return {"task_id": task_id, "cancelled": task_manager.cancel(task_id)}

    tools = [
        Tool("read_file", "Read a text file (max 1MB)", {"path": "string"}, PermissionLevel.LOW, read_file,
             parameters={"path": P("string", "file path")}, category="files"),
        Tool("write_file", "Write to a text file", {"path": "string", "content": "string", "append": "bool (optional)"},
             PermissionLevel.MEDIUM, write_file,
             parameters={"path": P("string", "file path"), "content": P("string", "text to write"),
                         "append": P("boolean", "append instead of overwrite", required=False, default=False)},
             category="files"),
        Tool("list_directory", "List directory contents", {"path": "string"}, PermissionLevel.LOW, list_directory,
             parameters={"path": P("string", "directory path")}, category="files"),
        Tool("search_files", "Search for files matching a pattern", {"pattern": "string", "root": "string (optional)"},
             PermissionLevel.LOW, search_files,
             parameters={"pattern": P("string", "glob pattern, e.g. *.txt"),
                         "root": P("string", "directory to search", required=False, default=".")},
             category="files"),
        Tool("run_command", "Run a shell command (never available to the LLM)",
             {"command": "string", "timeout": "int (optional)", "cwd": "string (optional)"},
             PermissionLevel.HIGH, run_command,
             parameters={"command": P("string"), "timeout": P("integer", required=False, default=30, minimum=1, maximum=600),
                         "cwd": P("string", required=False)},
             category="system"),
        Tool("start_background_task", "Start a cancellable background command",
             {"description": "string", "command": "string"}, PermissionLevel.HIGH, start_background_task,
             parameters={"description": P("string"), "command": P("string"), "cwd": P("string", required=False),
                         "timeout": P("integer", required=False, default=300, minimum=1, maximum=3600)},
             category="system"),
        Tool("background_task_status", "Get the status of background tasks", {"task_id": "string (optional)"},
             PermissionLevel.LOW, background_task_status,
             parameters={"task_id": P("string", required=False)}, category="system"),
        Tool("cancel_background_task", "Cancel a running background task", {"task_id": "string"},
             PermissionLevel.MEDIUM, cancel_background_task,
             parameters={"task_id": P("string")}, category="system"),
    ]
    for tool in tools:
        registry.register(tool)

    # Desktop control lives in its own package but shares this registry.
    try:
        from modules.desktop.tools import register_desktop_tools
        register_desktop_tools(registry)
    except Exception:  # pragma: no cover - desktop deps missing
        log.exception("desktop.tools.register_failed")

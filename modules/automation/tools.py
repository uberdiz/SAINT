"""
modules/automation/tools.py

Tool registry and automation tools for Windows desktop control.
"""

import os
import subprocess
import time
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from pathlib import Path

try:
    import pyautogui
    import pygetwindow as gw
    HAS_AUTOMATION = True
except ImportError:
    pyautogui = None
    gw = None
    HAS_AUTOMATION = False


class PermissionLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolError(Exception):
    pass


@dataclass
class Tool:
    name: str
    description: str
    arguments: Dict[str, Any]  # JSON schema-like description
    permission: PermissionLevel
    execute_fn: Callable
    enabled: bool = True


@dataclass
class ToolResult:
    success: bool
    result: Any = None
    error: Optional[str] = None
    error_code: Optional[str] = None  # machine-readable code, e.g. NO_ACTIVE_DEVICE


class ToolRegistry:
    """Registry for automation tools."""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}
        self._lock = threading.Lock()

    def register(self, tool: Tool):
        with self._lock:
            self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        with self._lock:
            if name in self._tools:
                del self._tools[name]
                return True
            return False

    def get(self, name: str) -> Optional[Tool]:
        with self._lock:
            return self._tools.get(name)

    def list_tools(self) -> List[Tool]:
        with self._lock:
            return list(self._tools.values())

    def execute(self, name: str, **kwargs) -> ToolResult:
        tool = self.get(name)
        if not tool:
            return ToolResult(success=False, error=f"Tool '{name}' not found")
        if not tool.enabled:
            return ToolResult(success=False, error=f"Tool '{name}' is disabled")

        # Import lazily to avoid a module-import cycle.
        from core.permissions import permission_manager
        from core.events import event_bus, EventType
        policy = permission_manager.policy_for_tool(name, tool.permission.value)
        event_bus.emit_event(EventType.TOOL_REQUESTED, {
            "tool": name, "permission": tool.permission.value, "policy": policy
        })
        if policy == permission_manager.DENY:
            event_bus.emit_event(EventType.TOOL_PERMISSION_DENIED, {"tool": name, "reason": "policy"})
            return ToolResult(success=False, error=f"Permission denied for tool '{name}'")
        if policy == permission_manager.CONFIRM:
            event_bus.emit_event(EventType.TOOL_PERMISSION_REQUIRED, {"tool": name})
            return ToolResult(success=False, error=f"Confirmation required for tool '{name}'")

        event_bus.emit_event(EventType.TOOL_PERMISSION_GRANTED, {"tool": name})
        started = time.perf_counter()
        event_bus.emit_event(EventType.TOOL_STARTED, {"tool": name})
        try:
            result = tool.execute_fn(**kwargs)
            event_bus.emit_event(EventType.TOOL_COMPLETED, {
                "tool": name, "duration_ms": round((time.perf_counter() - started) * 1000, 1)
            })
            return ToolResult(success=True, result=result)
        except Exception as e:
            event_bus.emit_event(EventType.TOOL_FAILED, {
                "tool": name, "error": str(e),
                "duration_ms": round((time.perf_counter() - started) * 1000, 1)
            })
            return ToolResult(success=False, error=str(e))


# Global registry
_tool_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    global _tool_registry
    if _tool_registry is None:
        _tool_registry = ToolRegistry()
        _register_default_tools(_tool_registry)
    return _tool_registry


def _register_default_tools(registry: ToolRegistry):
    """Register built-in automation tools."""

    # --- Mouse Tools ---
    def mouse_move(x: int, y: int, duration: float = 0.5) -> dict:
        if not HAS_AUTOMATION:
            raise ToolError("pyautogui not installed")
        pyautogui.moveTo(x, y, duration=duration)
        return {"x": x, "y": y}

    def mouse_click(x: Optional[int] = None, y: Optional[int] = None, button: str = "left", clicks: int = 1) -> dict:
        if not HAS_AUTOMATION:
            raise ToolError("pyautogui not installed")
        if x is not None and y is not None:
            pyautogui.click(x, y, button=button, clicks=clicks)
        else:
            pyautogui.click(button=button, clicks=clicks)
        return {"x": x, "y": y, "button": button, "clicks": clicks}

    def mouse_scroll(clicks: int, x: Optional[int] = None, y: Optional[int] = None) -> dict:
        if not HAS_AUTOMATION:
            raise ToolError("pyautogui not installed")
        if x is not None and y is not None:
            pyautogui.scroll(clicks, x=x, y=y)
        else:
            pyautogui.scroll(clicks)
        return {"clicks": clicks, "x": x, "y": y}

    # --- Keyboard Tools ---
    def type_text(text: str, interval: float = 0.01) -> dict:
        if not HAS_AUTOMATION:
            raise ToolError("pyautogui not installed")
        pyautogui.write(text, interval=interval)
        return {"text": text, "length": len(text)}

    def press_key(key: str) -> dict:
        if not HAS_AUTOMATION:
            raise ToolError("pyautogui not installed")
        pyautogui.press(key)
        return {"key": key}

    def hotkey(*keys: str) -> dict:
        if not HAS_AUTOMATION:
            raise ToolError("pyautogui not installed")
        pyautogui.hotkey(*keys)
        return {"keys": list(keys)}

    # --- Screenshot Tools ---
    def take_screenshot(region: Optional[tuple] = None, save_path: Optional[str] = None) -> dict:
        if not HAS_AUTOMATION:
            raise ToolError("pyautogui not installed")
        if region:
            screenshot = pyautogui.screenshot(region=region)
        else:
            screenshot = pyautogui.screenshot()
        if save_path:
            screenshot.save(save_path)
        return {"saved": save_path is not None, "path": save_path, "size": screenshot.size}

    # --- Application Tools ---
    def open_application(app_name: str) -> dict:
        """Open an application by name or path."""
        try:
            if os.name == 'nt':
                # Windows
                if app_name.endswith('.exe') or '\\' in app_name or '/' in app_name:
                    subprocess.Popen(app_name, shell=True)
                else:
                    # Try start command for known apps
                    subprocess.Popen(f"start {app_name}", shell=True)
            else:
                subprocess.Popen(app_name, shell=True)
            return {"app": app_name, "status": "started"}
        except Exception as e:
            raise ToolError(f"Failed to open {app_name}: {e}")

    def close_application(window_title: str) -> dict:
        """Close a window by title."""
        if not HAS_AUTOMATION:
            raise ToolError("pygetwindow not installed")
        windows = gw.getWindowsWithTitle(window_title)
        if not windows:
            raise ToolError(f"No window found with title: {window_title}")
        for w in windows:
            w.close()
        return {"closed": len(windows)}

    def focus_window(window_title: str) -> dict:
        """Focus a window by title."""
        if not HAS_AUTOMATION:
            raise ToolError("pygetwindow not installed")
        windows = gw.getWindowsWithTitle(window_title)
        if not windows:
            raise ToolError(f"No window found with title: {window_title}")
        windows[0].activate()
        return {"focused": window_title}

    def list_windows() -> dict:
        """List all open windows."""
        if not HAS_AUTOMATION:
            raise ToolError("pygetwindow not installed")
        windows = gw.getAllWindows()
        return {"windows": [{"title": w.title, "left": w.left, "top": w.top, "width": w.width, "height": w.height} for w in windows if w.title]}

    # --- File System Tools ---
    def read_file(path: str) -> dict:
        """Read a text file."""
        path_obj = Path(path)
        if not path_obj.exists():
            raise ToolError(f"File not found: {path}")
        if path_obj.stat().st_size > 1024 * 1024:  # 1MB limit
            raise ToolError("File too large (>1MB)")
        content = path_obj.read_text(encoding='utf-8', errors='replace')
        return {"path": path, "content": content, "size": len(content)}

    def write_file(path: str, content: str, append: bool = False) -> dict:
        """Write to a text file."""
        path_obj = Path(path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        mode = 'a' if append else 'w'
        path_obj.write_text(content, encoding='utf-8')
        return {"path": path, "bytes": len(content.encode('utf-8')), "appended": append}

    def list_directory(path: str) -> dict:
        """List directory contents."""
        path_obj = Path(path)
        if not path_obj.exists():
            raise ToolError(f"Directory not found: {path}")
        if not path_obj.is_dir():
            raise ToolError(f"Not a directory: {path}")
        items = []
        for item in path_obj.iterdir():
            items.append({
                "name": item.name,
                "is_dir": item.is_dir(),
                "size": item.stat().st_size if item.is_file() else None,
                "modified": item.stat().st_mtime,
            })
        return {"path": path, "items": items}

    def search_files(pattern: str, root: str = ".") -> dict:
        """Search for files matching a pattern."""
        root_path = Path(root)
        if not root_path.exists():
            raise ToolError(f"Root path not found: {root}")
        matches = list(root_path.rglob(pattern))
        return {"pattern": pattern, "root": root, "matches": [str(m) for m in matches[:100]]}

    # --- System Tools ---
    def run_command(command: str, timeout: int = 30, cwd: Optional[str] = None) -> dict:
        """Run a shell command."""
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
            return {
                "command": command,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        except subprocess.TimeoutExpired:
            raise ToolError(f"Command timed out after {timeout}s")
        except Exception as e:
            raise ToolError(f"Command failed: {e}")

    # Register all tools
    tools = [
        Tool(
            name="mouse_move",
            description="Move mouse to coordinates",
            arguments={"x": "int", "y": "int", "duration": "float (optional)"},
            permission=PermissionLevel.LOW,
            execute_fn=mouse_move,
        ),
        Tool(
            name="mouse_click",
            description="Click mouse at position",
            arguments={"x": "int (optional)", "y": "int (optional)", "button": "str (optional, default: left)", "clicks": "int (optional, default: 1)"},
            permission=PermissionLevel.MEDIUM,
            execute_fn=mouse_click,
        ),
        Tool(
            name="mouse_scroll",
            description="Scroll mouse wheel",
            arguments={"clicks": "int", "x": "int (optional)", "y": "int (optional)"},
            permission=PermissionLevel.LOW,
            execute_fn=mouse_scroll,
        ),
        Tool(
            name="type_text",
            description="Type text at current cursor position",
            arguments={"text": "string", "interval": "float (optional, default: 0.01)"},
            permission=PermissionLevel.MEDIUM,
            execute_fn=type_text,
        ),
        Tool(
            name="press_key",
            description="Press a single key",
            arguments={"key": "string"},
            permission=PermissionLevel.MEDIUM,
            execute_fn=press_key,
        ),
        Tool(
            name="hotkey",
            description="Press a key combination (e.g., ctrl+c)",
            arguments={"keys": "string..."},
            permission=PermissionLevel.MEDIUM,
            execute_fn=hotkey,
        ),
        Tool(
            name="take_screenshot",
            description="Take a screenshot",
            arguments={"region": "tuple (optional: x, y, width, height)", "save_path": "string (optional)"},
            permission=PermissionLevel.LOW,
            execute_fn=take_screenshot,
        ),
        Tool(
            name="open_application",
            description="Launch an application",
            arguments={"app_name": "string (name or path)"},
            permission=PermissionLevel.MEDIUM,
            execute_fn=open_application,
        ),
        Tool(
            name="close_application",
            description="Close a window by title",
            arguments={"window_title": "string"},
            permission=PermissionLevel.MEDIUM,
            execute_fn=close_application,
        ),
        Tool(
            name="focus_window",
            description="Focus a window by title",
            arguments={"window_title": "string"},
            permission=PermissionLevel.LOW,
            execute_fn=focus_window,
        ),
        Tool(
            name="list_windows",
            description="List all open windows",
            arguments={},
            permission=PermissionLevel.LOW,
            execute_fn=list_windows,
        ),
        Tool(
            name="read_file",
            description="Read a text file (max 1MB)",
            arguments={"path": "string"},
            permission=PermissionLevel.LOW,
            execute_fn=read_file,
        ),
        Tool(
            name="write_file",
            description="Write to a text file",
            arguments={"path": "string", "content": "string", "append": "bool (optional)"},
            permission=PermissionLevel.MEDIUM,
            execute_fn=write_file,
        ),
        Tool(
            name="list_directory",
            description="List directory contents",
            arguments={"path": "string"},
            permission=PermissionLevel.LOW,
            execute_fn=list_directory,
        ),
        Tool(
            name="search_files",
            description="Search for files matching a pattern",
            arguments={"pattern": "string", "root": "string (optional, default: .)"},
            permission=PermissionLevel.LOW,
            execute_fn=search_files,
        ),
        Tool(
            name="run_command",
            description="Run a shell command",
            arguments={"command": "string", "timeout": "int (optional, default: 30)", "cwd": "string (optional)"},
            permission=PermissionLevel.HIGH,
            execute_fn=run_command,
        ),
    ]

    for tool in tools:
        registry.register(tool)
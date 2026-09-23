"""
tests/test_automation.py

Tests for the Automation module.
"""

import sys
import os
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from modules.automation.module import AutomationModule
from modules.automation.tools import PermissionLevel, ToolRegistry, get_tool_registry


@pytest.fixture
def automation_module():
    """Create a fresh automation module for each test."""
    import modules.automation.tools as tools_module
    tools_module._tool_registry = None
    
    m = AutomationModule()
    m.enable()
    yield m
    m.disable()
    tools_module._tool_registry = None


def test_tool_registry(automation_module):
    """Test tool registry functionality."""
    m = automation_module
    
    tools = m.list_tools()
    assert len(tools) > 0
    
    # Check for expected tools
    tool_names = [t["name"] for t in tools]
    expected = ["desktop.mouse_move", "desktop.mouse_click", "desktop.type_text", "desktop.press_keys",
                "desktop.open_app", "desktop.close_app", "desktop.focus_window", "desktop.list_windows",
                "desktop.move_window", "read_file", "write_file", "list_directory", "search_files",
                "run_command"]
    for exp in expected:
        assert exp in tool_names


def test_tool_details(automation_module):
    """Test getting tool details."""
    m = automation_module
    
    details = m.get_tool("desktop.mouse_move")
    assert details["name"] == "desktop.mouse_move"
    assert "description" in details
    assert "arguments" in details
    assert details["permission"] == "low"
    
    # Non-existent tool
    assert m.get_tool("nonexistent") == {}


def test_execute_tool(automation_module):
    """Test executing tools."""
    m = automation_module
    
    # Test list_directory
    result = m.execute_tool("list_directory", path=".")
    assert result["success"] is True
    assert "result" in result
    assert "items" in result["result"]
    
    # Test read/write file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        temp_path = f.name
    
    try:
        # Write
        result = m.execute_tool("write_file", path=temp_path, content="Hello, World!")
        assert result["success"] is True
        
        # Read
        result = m.execute_tool("read_file", path=temp_path)
        assert result["success"] is True
        assert "Hello, World!" in result["result"]["content"]
    finally:
        os.unlink(temp_path)


def test_permission_modes(automation_module):
    """Test permission modes."""
    m = automation_module
    
    # Default is confirm
    assert m.get_permission_mode() == "confirm"
    
    # Change mode
    m.set_permission_mode("safe")
    assert m.get_permission_mode() == "safe"
    
    # In safe mode, only LOW permission tools allowed
    assert m.check_permission("desktop.mouse_move") is True  # LOW
    assert m.check_permission("desktop.type_text") is False  # MEDIUM
    assert m.check_permission("run_command") is False  # HIGH
    
    m.set_permission_mode("autonomous")
    assert m.get_permission_mode() == "autonomous"
    # In autonomous, all except HIGH allowed
    assert m.check_permission("desktop.mouse_move") is True
    assert m.check_permission("desktop.type_text") is True
    assert m.check_permission("run_command") is False  # HIGH still needs confirm
    
    m.set_permission_mode("confirm")
    assert m.get_permission_mode() == "confirm"
    # In confirm mode, all allowed (confirmation handled by caller)
    assert m.check_permission("desktop.mouse_move") is True
    assert m.check_permission("desktop.type_text") is True
    assert m.check_permission("run_command") is True


def test_tool_enable_disable(automation_module):
    """Test enabling/disabling tools."""
    m = automation_module
    
    # Disable a tool
    assert m.set_tool_enabled("desktop.mouse_move", False) is True
    result = m.execute_tool("desktop.mouse_move", x=100, y=100)
    assert result["success"] is False
    assert "disabled" in result["error"].lower()
    
    # Re-enable
    assert m.set_tool_enabled("desktop.mouse_move", True) is True
    result = m.execute_tool("desktop.mouse_move", x=100, y=100)
    # Desktop control is switched off in the test config, so this must fail
    # safely (without moving the real mouse) — but not as "disabled".
    error_msg = result.get("error", "") or ""
    assert "disabled" not in error_msg.lower()


def test_file_operations(automation_module):
    """Test file system tools."""
    m = automation_module
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # List directory
        result = m.execute_tool("list_directory", path=tmpdir)
        assert result["success"] is True
        
        # Create a file
        test_file = os.path.join(tmpdir, "test.txt")
        result = m.execute_tool("write_file", path=test_file, content="Test content")
        assert result["success"] is True
        
        # Read it back
        result = m.execute_tool("read_file", path=test_file)
        assert result["success"] is True
        assert result["result"]["content"] == "Test content"
        
        # Search
        result = m.execute_tool("search_files", pattern="*.txt", root=tmpdir)
        assert result["success"] is True
        assert len(result["result"]["matches"]) == 1


def test_shell_command(automation_module):
    """Test shell command execution."""
    m = automation_module
    
    # Simple command
    result = m.execute_tool("run_command", confirmed=True, command="echo hello")
    assert result["success"] is True
    assert "hello" in result["result"]["stdout"]
    assert result["result"]["exit_code"] == 0
    
    # Command with error
    result = m.execute_tool("run_command", confirmed=True, command="cmd /c exit 1")
    assert result["success"] is True
    assert result["result"]["exit_code"] == 1
    
    # Timeout - use ping to simulate delay on Windows
    result = m.execute_tool("run_command", confirmed=True, command="ping -n 5 127.0.0.1", timeout=1)
    assert result["success"] is False
    assert "timed out" in result["error"].lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
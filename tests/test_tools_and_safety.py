"""Tool framework: validation, permissions/confirmation, LLM exposure, desktop safety."""

import pytest

from core.config import config
from modules.automation.tools import Tool, ToolRegistry, PermissionLevel, P, ToolError, get_tool_registry
from modules.agent.confirm import confirmations, classify_reply
from modules.agent import router


def _reg():
    reg = ToolRegistry()
    calls = []
    reg.register(Tool("t.low", "low", {}, PermissionLevel.LOW, lambda n: calls.append(n) or {"n": n},
                      parameters={"n": P("integer", minimum=1, maximum=5)}))
    reg.register(Tool("t.high", "high", {}, PermissionLevel.HIGH, lambda: calls.append("high") or {"ok": 1},
                      parameters={}))
    reg.register(Tool("t.err", "err", {}, PermissionLevel.LOW,
                      lambda: (_ for _ in ()).throw(ToolError("clean message", "X")), parameters={}))
    return reg, calls


def test_validation():
    reg, calls = _reg()
    assert reg.execute("t.low", n="3").result == {"n": 3}          # coerced
    r = reg.execute("t.low", n=9)
    assert not r.success and r.error_code == "INVALID_PARAMS"
    r = reg.execute("t.low", n=2, rm="-rf")
    assert not r.success and "unknown parameter" in r.error
    r = reg.execute("t.low")
    assert not r.success and "missing" in r.error
    assert calls == [3]


def test_high_risk_needs_confirmation():
    reg, calls = _reg()
    r = reg.execute("t.high")
    assert not r.success and r.error_code == "CONFIRM_REQUIRED" and calls == []
    assert reg.execute("t.high", _confirmed=True).success and calls == ["high"]


def test_tool_error_is_clean():
    reg, _ = _reg()
    r = reg.execute("t.err")
    assert r.error == "clean message" and r.error_code == "X"


def test_shell_never_exposed_to_llm():
    names = {t.name for t in get_tool_registry().list_tools() if t.llm_exposed}
    assert "run_command" not in names and "start_background_task" not in names
    assert "write_file" not in names


def test_confirmation_flow():
    assert classify_reply("yes please") is True and classify_reply("no") is False
    assert classify_reply("play some jazz") is None
    ran = []
    reply = router.run_tool.__globals__  # noqa: F841 (import check)
    from modules.agent.confirm import PendingAction
    confirmations.ask(PendingAction("do the thing", lambda: ran.append(1) or "Done."))
    assert confirmations.resolve("yes") == "Done." and ran == [1]
    confirmations.ask(PendingAction("do the thing", lambda: ran.append(2) or "Done."))
    assert confirmations.resolve("no") == "Okay, I won't." and ran == [1]
    confirmations.ask(PendingAction("do the thing", lambda: ran.append(3) or "Done."))
    assert confirmations.resolve("what time is it") is None and confirmations.pending is None


def test_desktop_disabled_blocks_everything():
    config.set("desktop.enabled", False, persist=False)
    r = get_tool_registry().execute("desktop.type_text", text="hello")
    assert not r.success and "turned off" in r.error.lower()


def test_desktop_key_validation(monkeypatch):
    from modules.desktop.controller import DesktopController
    config.set("desktop.enabled", True, persist=False)
    try:
        d = DesktopController()
        with pytest.raises(ToolError):
            d.press_keys("alt+f4")
        with pytest.raises(ToolError):
            d.press_keys("ctrl+notakey")
        config.set("desktop.max_type_length", 5, persist=False)
        with pytest.raises(ToolError):
            d.type_text("way too long")
        with pytest.raises(ToolError):
            d.mouse_move(10 ** 6, 10 ** 6)
    finally:
        config.set("desktop.enabled", False, persist=False)
        config.set("desktop.max_type_length", 500, persist=False)


def test_app_resolution_never_uses_shell():
    from modules.desktop.apps import AppCatalog
    cat = AppCatalog()
    cat._entries = {}
    assert cat.resolve("notepad").target == "notepad.exe"
    assert cat.resolve("rm -rf / && calc") is None

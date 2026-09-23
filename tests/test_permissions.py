from core.config import config
from core.permissions import PermissionManager


def test_low_risk_defaults_to_allow():
    manager = PermissionManager()
    assert manager.policy_for_tool("spotify.play", "low") == manager.ALLOW


def test_medium_risk_allowed_in_confirm_mode():
    config.set("automation.permission_mode", "confirm", persist=False)
    manager = PermissionManager()
    assert manager.policy_for_tool("spotify.queue", "medium") == manager.ALLOW


def test_high_risk_requires_confirmation():
    config.set("automation.permission_mode", "confirm", persist=False)
    manager = PermissionManager()
    assert manager.policy_for_tool("run_command", "high") == manager.CONFIRM


def test_safe_mode_denies_medium_and_high():
    config.set("automation.permission_mode", "safe", persist=False)
    try:
        manager = PermissionManager()
        assert manager.policy_for_tool("desktop.type_text", "medium") == manager.DENY
        assert manager.policy_for_tool("spotify.current", "low") == manager.ALLOW
    finally:
        config.set("automation.permission_mode", "confirm", persist=False)


def test_override_wins():
    manager = PermissionManager()
    config.set("permissions.overrides", {"desktop": "deny"}, persist=False)
    try:
        assert manager.policy_for_tool("desktop.open_app", "low") == manager.DENY
    finally:
        config.set("permissions.overrides", {}, persist=False)

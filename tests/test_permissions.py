from core.permissions import PermissionManager

def test_low_risk_defaults_to_allow():
    manager = PermissionManager()
    assert manager.policy_for_tool("spotify.play", "low") == manager.ALLOW

def test_medium_risk_defaults_to_confirm():
    manager = PermissionManager()
    assert manager.policy_for_tool("spotify.queue", "medium") == manager.CONFIRM

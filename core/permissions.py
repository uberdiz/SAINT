"""Centralized tool permission policy for SAINT 0.2."""

from core.config import config
from modules.automation.tools import PermissionLevel


class PermissionManager:
    """Stores per-tool and per-module access overrides.
    Values are allow, confirm, or deny.
    """

    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"

    def _overrides(self):
        return config.get("permissions.overrides", {}) or {}

    def get_policy(self, tool_name: str, default: str = CONFIRM) -> str:
        overrides = self._overrides()
        if tool_name in overrides:
            return overrides[tool_name]
        module = tool_name.split(".", 1)[0]
        if module in overrides:
            return overrides[module]
        return default

    def set_policy(self, key: str, policy: str):
        if policy not in {self.ALLOW, self.CONFIRM, self.DENY}:
            raise ValueError(f"Invalid permission policy: {policy}")
        overrides = dict(self._overrides())
        overrides[key] = policy
        config.set("permissions.overrides", overrides, persist=True)

    def reset_policy(self, key: str):
        overrides = dict(self._overrides())
        overrides.pop(key, None)
        config.set("permissions.overrides", overrides, persist=True)

    def list_overrides(self):
        return dict(self._overrides())


permission_manager = PermissionManager()

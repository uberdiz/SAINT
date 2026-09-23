"""Centralized tool permission policy for SAINT.

Every tool has a risk level (low / medium / high). The effective policy for a
tool is, in order of precedence:

1. an explicit per-tool override       permissions.overrides["spotify.play"]
2. a per-module override               permissions.overrides["desktop"]
3. the global automation mode          automation.permission_mode
       safe        low → allow, medium/high → deny
       confirm     low/medium → allow, high → confirm            (default)
       autonomous  everything allowed, except high → confirm when
                   automation.confirm_dangerous is on

"confirm" means SAINT asks the user ("Should I close Discord?") and only runs
the tool after an explicit yes.
"""

from core.config import config


class PermissionManager:
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"

    def _overrides(self):
        return config.get("permissions.overrides", {}) or {}

    def default_policy(self, permission_level: str) -> str:
        mode = config.get("automation.permission_mode", "confirm")
        level = (permission_level or "medium").lower()
        if mode == "safe":
            return self.ALLOW if level == "low" else self.DENY
        if mode == "autonomous":
            if level == "high" and config.get("automation.confirm_dangerous", True):
                return self.CONFIRM
            return self.ALLOW
        # "confirm" (default)
        return self.CONFIRM if level == "high" else self.ALLOW

    def policy_for_tool(self, tool_name: str, permission_level: str = "medium") -> str:
        overrides = self._overrides()
        if tool_name in overrides:
            return overrides[tool_name]
        module = tool_name.split(".", 1)[0]
        if module in overrides:
            return overrides[module]
        return self.default_policy(permission_level)

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

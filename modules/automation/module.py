"""
modules/automation/module.py

Planned for v0.4. Disabled by default in v0.1.
"""

from modules.base import BaseModule


class AutomationModule(BaseModule):
    name = "Automation"
    description = "Desktop automation: keyboard, mouse, and window control (planned v0.4)."

    def __init__(self):
        super().__init__()
        self.subtasks = {
            "Keyboard Control": False,
            "Mouse Control": False,
            "Window Management": False,
            "Macro Recording": False,
        }

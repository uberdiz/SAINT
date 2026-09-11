"""
modules/base.py

Every SAINT module (AI, Voice, Automation, Vision, Memory...) inherits
from BaseModule. Modules never talk to each other directly -- only to
the Core (event bus, config, logger, state).
"""

from core.events import event_bus, EventType


class BaseModule:
    name = "Base"
    description = ""

    def __init__(self):
        self.enabled = False
        self.loaded = False
        # subtasks: dict of {label: bool} used to compute completion %
        self.subtasks = {}

    def load(self):
        self.loaded = True
        event_bus.emit_event(EventType.MODULE_LOADED, {"module": self.name})

    def unload(self):
        self.loaded = False
        event_bus.emit_event(EventType.MODULE_UNLOADED, {"module": self.name})

    def enable(self):
        self.enabled = True
        if not self.loaded:
            self.load()
        event_bus.emit_event(EventType.MODULE_ENABLED, {"module": self.name})

    def disable(self):
        self.enabled = False
        event_bus.emit_event(EventType.MODULE_DISABLED, {"module": self.name})

    def completion_percentage(self):
        if not self.subtasks:
            return 0
        done = sum(1 for v in self.subtasks.values() if v)
        return int(round(done / len(self.subtasks) * 100))

    def status_symbol(self):
        return "\u2713" if self.enabled else "OFF"

"""
core/module_manager.py

Holds every installed module instance and provides enable/disable and
status querying. In v0.1 only AI is functional; the rest exist as real
(but inert) objects so the Module Manager UI has genuine data to show
rather than placeholders.
"""

from core.config import config
from modules.ai.module import AIModule
from modules.voice.module import VoiceModule
from modules.automation.module import AutomationModule
from modules.vision.module import VisionModule
from modules.memory.module import MemoryModule


class ModuleManager:
    def __init__(self):
        self.modules = {
            "ai": AIModule(),
            "voice": VoiceModule(),
            "automation": AutomationModule(),
            "vision": VisionModule(),
            "memory": MemoryModule(),
        }
        self._apply_initial_state()

    def _apply_initial_state(self):
        for key, module in self.modules.items():
            should_enable = config.get(f"modules.{key}", False)
            # Only AI is actually functional in v0.1; others can be
            # "enabled" in config but will not do anything yet.
            if should_enable:
                module.enable()

    def get(self, key):
        return self.modules.get(key)

    def all_modules(self):
        return list(self.modules.values())

    def loaded_count(self):
        return sum(1 for m in self.modules.values() if m.loaded)

    def total_count(self):
        return len(self.modules)

    def set_enabled(self, key, enabled: bool):
        module = self.modules.get(key)
        if not module:
            return
        if enabled:
            module.enable()
        else:
            module.disable()
        config.set(f"modules.{key}", enabled)


# Singleton
module_manager = ModuleManager()

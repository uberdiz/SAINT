"""Central module registry for SAINT."""
from core.config import config
from modules.ai.module import AIModule
from modules.voice.module import VoiceModule
from modules.automation.module import AutomationModule
from modules.vision.module import VisionModule
from modules.memory.module import MemoryModule
from modules.spotify.module import SpotifyModule


class ModuleManager:
    def __init__(self):
        self.modules = {
            "ai": AIModule(),
            "voice": VoiceModule(),
            "automation": AutomationModule(),
            "vision": VisionModule(),
            "memory": MemoryModule(),
            "spotify": SpotifyModule(),
        }
        self._apply_initial_state()

    def _apply_initial_state(self):
        for key, module in self.modules.items():
            if config.get(f"modules.{key}", False):
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

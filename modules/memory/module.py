"""
modules/memory/module.py

Planned for v0.3. Disabled by default in v0.1.
"""

from modules.base import BaseModule


class MemoryModule(BaseModule):
    name = "Memory"
    description = "Long-term memory, conversation history, and memory search (planned v0.3)."

    def __init__(self):
        super().__init__()
        self.subtasks = {
            "Long-Term Storage": False,
            "Conversation History": False,
            "Memory Search": False,
        }

"""The Watch module: watchers + the rolling 'what changed?' log."""

import logging

from modules.base import BaseModule

log = logging.getLogger("saint.watch")


class WatchModule(BaseModule):
    name = "Watch"
    description = "Tells you when something finishes or changes, and what changed while you were away."

    def __init__(self):
        super().__init__()
        self.subtasks = {"Window / download watchers": True, "What changed?": True, "Follow a screen": True}

    def enable(self):
        super().enable()
        from modules.watch.snapshots import snapshot_log
        snapshot_log.start()

    def disable(self):
        super().disable()
        from modules.watch.snapshots import snapshot_log
        from modules.watch.watchers import watch_manager
        snapshot_log.stop()
        watch_manager.clear()
        watch_manager.stop()

"""
modules/notifications/module.py

When turned on (Settings > Modules, or "read my notifications out loud"),
checks for new Windows notifications every notifications.poll_sec seconds
and announces the important ones. Everything recent is kept in memory so
"read my notifications" can list what arrived.
"""

import logging
import threading

from modules.base import BaseModule

log = logging.getLogger("saint.notifications")


class NotificationsModule(BaseModule):
    name = "Notifications"
    description = "Reads Windows notifications aloud — only the important ones, if you like."

    def __init__(self):
        super().__init__()
        self.subtasks = {"Read notifications": True, "Importance filter": True}
        self._thread = None
        self._stop = threading.Event()

    def enable(self):
        super().enable()
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="notifications")
        self._thread.start()

    def disable(self):
        super().disable()
        self._stop.set()

    def _run(self):
        from core.config import config
        from modules.notifications import reader
        from modules.notifications.tools import inbox
        last = reader.latest_order()          # only what arrives from now on
        while not self._stop.wait(max(2.0, float(config.get("notifications.poll_sec", 5) or 5))):
            try:
                new = reader.read(last)
            except Exception:
                log.exception("notifications.read_failed")
                continue
            for n in new:
                last = max(last, n["order"])
                inbox.add(n)
                if reader.is_important(n):
                    self._announce(n)

    @staticmethod
    def _announce(n):
        from modules.notifications.reader import spoken
        log.info("notifications.announce app=%s", n["app"])
        try:
            from core.runtime import runtime
            if runtime.controller:
                runtime.controller.announce(spoken(n), source="notification")
        except Exception:
            pass

"""
core/state.py

Tracks live runtime state for the app: uptime, total events seen,
error counts, and system resource usage (CPU / RAM). The Dashboard
and Health Monitor both read from this.
"""

import os
import time

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

from core.events import event_bus


class AppState:
    def __init__(self):
        self.start_time = time.time()
        self.event_count = 0
        self.error_count = 0
        self.module_crash_count = 0
        self._process = psutil.Process(os.getpid()) if _HAS_PSUTIL else None

        event_bus.event_occurred.connect(self._on_event)

    def _on_event(self, ev):
        self.event_count += 1
        if ev.type in ("error", "ai.error"):
            self.error_count += 1
        if ev.type == "module.crash":
            self.module_crash_count += 1

    def uptime_seconds(self):
        return time.time() - self.start_time

    def uptime_formatted(self):
        secs = int(self.uptime_seconds())
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def cpu_percent(self):
        """Return process CPU usage as a percentage of total system capacity (0-100%)."""
        if not _HAS_PSUTIL:
            return 0.0
        # cpu_percent() returns percentage of ONE core by default
        # Divide by logical CPU count to get percentage of total system capacity
        # This gives us 0-100% range regardless of core count
        raw_percent = self._process.cpu_percent(interval=None)
        cpu_count = psutil.cpu_count(logical=True) or 1
        return min(100.0, raw_percent / cpu_count)

    def ram_mb(self):
        if not _HAS_PSUTIL:
            return 0.0
        return self._process.memory_info().rss / (1024 * 1024)


# Singleton
app_state = AppState()

"""
modules/link/logs.py

Every device's log in one place, on this PC: data/logs/devices/<device>-<id>.log.

While your own devices are connected, this PC asks each one for the log lines it
hasn't collected yet (``log.get`` — the iPhone app and other PCs keep their
recent lines numbered in memory; see core/logger.LogRing and the app's AppLog).
Only own devices are asked, and only own devices are answered. A cursor per
device (and the device's run id, "boot") means nothing is fetched twice, and a
device that restarted is read again from its start.

Turn it off with ``link.collect_logs``.
"""

import json
import logging
import os
import re
import threading
from typing import Dict, Optional

log = logging.getLogger("saint.link.logs")

MAX_BYTES = 4_000_000


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name or "device").strip("-.")[:40] or "device"


class LogCollector:
    def __init__(self, directory: Optional[str] = None):
        if directory is None:
            from core.paths import data_path
            directory = str(data_path("logs", "devices"))
        self.directory = directory
        self._lock = threading.Lock()
        self._state: Optional[Dict[str, dict]] = None

    # ------------------------------------------------------------------ #
    @property
    def _state_path(self) -> str:
        return os.path.join(self.directory, "cursors.json")

    def _load(self) -> Dict[str, dict]:
        if self._state is None:
            try:
                with open(self._state_path, encoding="utf-8") as f:
                    self._state = json.load(f)
            except (OSError, ValueError):
                self._state = {}
        return self._state

    def _save(self):
        os.makedirs(self.directory, exist_ok=True)
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state or {}, f)
        os.replace(tmp, self._state_path)

    def cursor(self, peer_id: str) -> int:
        with self._lock:
            return int(self._load().get(peer_id, {}).get("cursor", 0))

    def request_args(self, peer_id: str) -> dict:
        with self._lock:
            st = self._load().get(peer_id, {})
        return {"after": int(st.get("cursor", 0)), "boot": st.get("boot", "")}

    def path_for(self, peer_id: str, name: str) -> str:
        return os.path.join(self.directory, f"{_safe(name)}-{peer_id[:6]}.log")

    def store(self, peer_id: str, name: str, data: dict) -> int:
        """Append what a device sent; returns how many lines were written."""
        lines = [str(l).rstrip("\r\n") for l in (data.get("lines") or []) if isinstance(l, (str, int, float))]
        with self._lock:
            os.makedirs(self.directory, exist_ok=True)
            path = self.path_for(peer_id, name)
            if lines:
                try:
                    if os.path.getsize(path) > MAX_BYTES:
                        os.replace(path, path + ".1")
                except OSError:
                    pass
                with open(path, "a", encoding="utf-8") as f:
                    if data.get("dropped"):
                        f.write(f"… {int(data['dropped'])} older line(s) were no longer kept on {name}\n")
                    f.write("\n".join(lines) + "\n")
            st = self._load()
            st[peer_id] = {"cursor": int(data.get("cursor") or 0), "boot": str(data.get("boot") or ""),
                           "name": name, "file": os.path.basename(path)}
            self._save()
        return len(lines)

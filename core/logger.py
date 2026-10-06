"""
core/logger.py

Central logging for SAINT.

* Every subsystem logs through the standard ``logging`` module under the
  ``saint.*`` namespace (``saint.voice``, ``saint.wake``, ``saint.agent``,
  ``saint.spotify`` ...). Those records, plus every event on the Event Bus,
  go to a rotating file (data/logs/saint.log) and to the terminal.
* High-frequency events (audio levels, streamed tokens, per-chunk TTS timing,
  wake-word scores) are never written per-occurrence, so the log stays
  readable.
* The level can be changed at runtime from Settings (``apply_level``).
"""

import collections
import logging
import os
import threading
import uuid
from logging.handlers import RotatingFileHandler

from core.events import event_bus
from core.paths import data_path

LOG_FILE = str(data_path("logs", "saint.log"))
LOG_DIR = os.path.dirname(LOG_FILE)

_LEVELS = {
    "Verbose": logging.DEBUG,
    "Normal": logging.INFO,
    "Errors Only": logging.ERROR,
}

# Event types that fire many times per second / per token. They drive the UI
# but would drown the log file.
NOISY_EVENTS = frozenset({
    "voice.audio.level",
    "voice.wake.score",
    "ai.stream.token",
    "tts.speak.chunk",
    "chat.message.update",
    "voice.stt.debug",
    "tts.inference.start",
    "tts.inference.end",
    "tts.audio.ready",
    "tts.playback.start",
    "tts.playback.end",
    "latency.tts.inference",
    "latency.tts.playback",
    "tts.generation.start",
    "tts.generation.end",
    "spotify.playback.changed",
    "system.stats",
    "agent.task",                  # every task state change: the trail goes to logs/tasks.log instead
})

_ERROR_EVENTS = frozenset({"error", "ai.error", "module.crash", "tool.failed",
                           "automation.failed", "wake.error", "tts.error"})
_WARNING_EVENTS = frozenset({"warning", "tool.permission.denied", "voice.stt.error"})

class LogRing(logging.Handler):
    """The last few thousand log lines in memory, numbered, so a paired PC can collect this
    device's log over SAINT Link (``log.get``) without reading files: "lines after N"."""

    def __init__(self, capacity: int = 4000):
        super().__init__()
        self._lines = collections.deque(maxlen=capacity)
        self._seq = 0
        self._ring_lock = threading.Lock()
        self.boot = uuid.uuid4().hex[:12]        # changes each run: the reader starts again from 0
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                                            "%Y-%m-%d %H:%M:%S"))

    def emit(self, record: logging.LogRecord):
        try:
            line = self.format(record)
        except Exception:
            return
        with self._ring_lock:
            self._seq += 1
            self._lines.append((self._seq, line))

    def since(self, after: int = 0, boot: str = "", limit: int = 2000) -> dict:
        """Lines numbered above ``after`` (all of them when ``boot`` is another run's)."""
        with self._ring_lock:
            if boot and boot != self.boot or after > self._seq:
                after = 0
            first = self._lines[0][0] if self._lines else self._seq + 1
            out = [line for n, line in self._lines if n > after][:limit]
            cursor = min(self._seq, max(after, first - 1) + len(out))
            dropped = max(0, first - 1 - after)
        return {"lines": out, "cursor": cursor, "dropped": dropped, "boot": self.boot}


log_ring = LogRing()

_logger = logging.getLogger("SAINT")      # event-bus mirror
_saint = logging.getLogger("saint")       # subsystem loggers (saint.voice, ...)
_initialized = False
_handlers = []


class _DropKnownNoise(logging.Filter):
    """Third-party warnings that repeat on every sentence and mean nothing to
    the user (Kokoro's phonemizer word-count check)."""
    _NOISE = ("words count mismatch",)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(n in msg for n in self._NOISE)


def _quiet_third_party():
    noise = _DropKnownNoise()
    logging.getLogger("phonemizer").addFilter(noise)


def _level_for(name: str, debug: bool = False) -> int:
    level = _LEVELS.get(name, logging.INFO)
    return logging.DEBUG if debug else level


def init_logger(level="Normal", debug=False):
    global _initialized
    if _initialized:
        apply_level(level, debug)
        return _logger

    os.makedirs(LOG_DIR, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    _handlers[:] = [file_handler, stream, log_ring]

    for lg in (_logger, _saint):
        lg.propagate = False
        for h in _handlers:
            lg.addHandler(h)

    # Third-party / root warnings (e.g. library deprecations) still reach the
    # file, but only at WARNING and above.
    root = logging.getLogger()
    root.addHandler(file_handler)
    root.addHandler(log_ring)
    if root.level == logging.NOTSET or root.level > logging.WARNING:
        root.setLevel(logging.WARNING)

    # The agent's activity trail ("15:03:24 task=ab12 ACTION Open VS Code"): its own file as well,
    # so a task can be followed start to finish (modules/agent/autonomy/manager.py).
    trail = RotatingFileHandler(os.path.join(LOG_DIR, "tasks.log"), maxBytes=1_000_000, backupCount=3,
                                encoding="utf-8")
    trail.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger("saint.agent.trail").addHandler(trail)

    _quiet_third_party()
    apply_level(level, debug)
    event_bus.subscribe(_on_event)
    _initialized = True
    return _logger


def apply_level(level="Normal", debug=False):
    """Change the log level at runtime (called when Settings are saved)."""
    lvl = _level_for(level, debug)
    _logger.setLevel(lvl)
    _saint.setLevel(lvl)


_last_media = [None]


def _on_event(ev):
    t = ev.type
    if t in NOISY_EVENTS:
        return
    if t == "media.changed":
        # Windows reports progress every few seconds; log a change of song / play state only.
        p = ev.payload or {}
        key = (p.get("app_id"), p.get("title"), p.get("artist"), p.get("is_playing"))
        if key == _last_media[0]:
            return
        _last_media[0] = key
    msg = f"{t} {ev.payload}" if ev.payload else t
    if t in _ERROR_EVENTS:
        _logger.error(msg)
    elif t in _WARNING_EVENTS:
        _logger.warning(msg)
    else:
        _logger.info(msg)


def get_logger(name: str = ""):
    """Return a subsystem logger: get_logger("voice") -> saint.voice."""
    if not _initialized:
        init_logger()
    return logging.getLogger(f"saint.{name}") if name else _logger


def read_recent_lines(n=200):
    """Used by the Console UI on startup to backfill history."""
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return lines[-n:]

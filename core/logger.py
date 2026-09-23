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

import logging
import os
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
})

_ERROR_EVENTS = frozenset({"error", "ai.error", "module.crash", "tool.failed",
                           "automation.failed", "wake.error", "tts.error"})
_WARNING_EVENTS = frozenset({"warning", "tool.permission.denied", "voice.stt.error"})

_logger = logging.getLogger("SAINT")      # event-bus mirror
_saint = logging.getLogger("saint")       # subsystem loggers (saint.voice, ...)
_initialized = False
_handlers = []


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
    _handlers[:] = [file_handler, stream]

    for lg in (_logger, _saint):
        lg.propagate = False
        for h in _handlers:
            lg.addHandler(h)

    # Third-party / root warnings (e.g. library deprecations) still reach the
    # file, but only at WARNING and above.
    root = logging.getLogger()
    root.addHandler(file_handler)
    if root.level == logging.NOTSET or root.level > logging.WARNING:
        root.setLevel(logging.WARNING)

    apply_level(level, debug)
    event_bus.subscribe(_on_event)
    _initialized = True
    return _logger


def apply_level(level="Normal", debug=False):
    """Change the log level at runtime (called when Settings are saved)."""
    lvl = _level_for(level, debug)
    _logger.setLevel(lvl)
    _saint.setLevel(lvl)


def _on_event(ev):
    t = ev.type
    if t in NOISY_EVENTS:
        return
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

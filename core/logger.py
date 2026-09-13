"""
core/logger.py

Every event on the Event Bus is written to a rotating log file, in
addition to normal Python logging calls. Nothing is hidden -- the
Console UI reads from the same stream live.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

from core.events import event_bus

LOG_DIR = "data/logs"
LOG_FILE = os.path.join(LOG_DIR, "saint.log")

_logger = logging.getLogger("SAINT")
_logger.propagate = False  # prevent duplicate emission to root logger
_initialized = False


def init_logger(level="Verbose"):
    global _initialized
    if _initialized:
        return _logger

    os.makedirs(LOG_DIR, exist_ok=True)

    level_map = {
        "Verbose": logging.DEBUG,
        "Normal": logging.INFO,
        "Errors Only": logging.ERROR,
    }
    _logger.setLevel(level_map.get(level, logging.DEBUG))

    handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)

    if not _logger.handlers:
        _logger.addHandler(handler)
        # Echo to stdout so `python app.py` shows activity in the terminal
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        _logger.addHandler(stream)

    def _on_event(ev):
        msg = f"{ev.type} {ev.payload}" if ev.payload else ev.type
        if ev.type in ("error", "ai.error", "module.crash"):
            _logger.error(msg)
        elif ev.type == "warning":
            _logger.warning(msg)
        elif ev.type in (
            "voice.audio.level",
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
        ):
            pass
        else:
            _logger.info(msg)

    event_bus.event_occurred.connect(_on_event)

    _initialized = True
    return _logger


def get_logger():
    if not _initialized:
        init_logger()
    return _logger


def read_recent_lines(n=200):
    """Used by the Console UI on startup to backfill history."""
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return lines[-n:]

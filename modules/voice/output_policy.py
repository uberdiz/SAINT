"""
modules/voice/output_policy.py

When and how loudly SAINT talks.

* Silent mode ("silent mode", "be quiet for 30 minutes"): SAINT keeps doing
  what it's asked but only speaks when it has to — a question, an error, a
  confirmation, a reminder or something you asked it to watch for. Replies
  still appear in the chat and the action notices. Expires on its own.
* Whisper replies: when you speak quietly, SAINT answers quietly
  (voice.whisper_replies / whisper_rms / whisper_gain).
"""

import re
import threading
import time

from core.config import config

# Replies that must be heard even in silent mode.
_MUST_SPEAK = re.compile(
    r"\?|\b(couldn'?t|can'?t|cannot|failed|error|isn'?t connected|not connected|unable|"
    r"don'?t have|wasn'?t able|sorry|went wrong|stopped there|should i|do you want)\b", re.I)
# Announcement sources that always speak (you asked for them).
_ALWAYS_SOURCES = {"reminder", "watch", "confirm", "silent"}      # notifications stay quiet in silent mode


class OutputPolicy:
    def __init__(self):
        self._lock = threading.Lock()
        self._silent_until = 0.0
        self._last_input_rms = 0.0
        self._last_input_at = 0.0
        self._levels: list = []                    # recent normal (non-whisper) input levels

    # -- silent mode ----------------------------------------------------- #
    def set_silent(self, minutes: float = 60.0):
        with self._lock:
            self._silent_until = time.time() + max(1.0, float(minutes)) * 60

    def clear_silent(self):
        with self._lock:
            self._silent_until = 0.0

    @property
    def silent(self) -> bool:
        return time.time() < self._silent_until

    def silent_remaining_min(self) -> int:
        return max(0, int(round((self._silent_until - time.time()) / 60)))

    def should_speak(self, text: str, source: str = "reply") -> bool:
        if not self.silent:
            return True
        if source in _ALWAYS_SOURCES:
            return True
        return bool(_MUST_SPEAK.search(text or ""))

    # -- whisper-quiet ----------------------------------------------------- #
    def note_input(self, rms: float):
        with self._lock:
            prev = self._last_input_rms
            self._last_input_rms = float(rms or 0.0)
            self._last_input_at = time.time()
            if prev and not self._is_whisper(prev):
                self._levels.append(prev)          # the user's normal speaking level
                del self._levels[:-30]

    def _is_whisper(self, rms: float) -> bool:
        """Quiet compared with how loud *this* user normally talks to SAINT
        (a fixed level missed real whispers: normal commands are ~0.06 RMS
        on this mic, a whisper ~0.03)."""
        floor = float(config.get("voice.whisper_rms", 0.02))
        if len(self._levels) >= 5:
            normal = sorted(self._levels)[len(self._levels) // 2]
            floor = max(floor, normal * float(config.get("voice.whisper_ratio", 0.55)))
        return 0 < rms < floor

    def gain(self) -> float:
        """Volume multiplier for the reply being spoken now."""
        if not config.get("voice.whisper_replies", True):
            return 1.0
        with self._lock:
            rms, at = self._last_input_rms, self._last_input_at
            whisper = self._is_whisper(rms)
        # Only the reply to the utterance just heard is quieter.
        if not rms or time.time() - at > 60:
            return 1.0
        if whisper:
            return max(0.1, min(1.0, float(config.get("voice.whisper_gain", 0.45))))
        return 1.0


output_policy = OutputPolicy()

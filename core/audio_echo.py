"""
core/audio_echo.py

Self-voice (echo) awareness for barge-in.

While SAINT speaks, the microphone keeps running so the user can interrupt.
The TTS playback loop reports the RMS level of every block it writes to the
sound card; the voice module compares live microphone energy with the level
SAINT is currently playing. A learned "coupling" factor (how much of the
speaker output leaks into the mic) predicts the echo level, and only energy
well above that prediction, sustained for a few hundred milliseconds, counts
as the user talking over SAINT.

This does not attempt full acoustic echo cancellation; it is a robust gate
that is cheap enough to run on every 30 ms audio frame. Transcript-level echo
filtering in core/conversation.py is the second line of defence.
"""

import collections
import threading
import time


class PlaybackMonitor:
    """Tracks the level of audio SAINT is playing right now."""

    def __init__(self, history_sec: float = 3.0):
        self._lock = threading.Lock()
        self._blocks = collections.deque()   # (start_time, end_time, rms)
        self._history_sec = history_sec
        self._last_end = 0.0

    def note_block(self, rms: float, duration_sec: float):
        """Called by the TTS playback thread right before a block is written."""
        now = time.monotonic()
        with self._lock:
            start = max(now, self._last_end)
            end = start + max(0.0, duration_sec)
            self._blocks.append((start, end, float(rms)))
            self._last_end = end
            cutoff = now - self._history_sec
            while self._blocks and self._blocks[0][1] < cutoff:
                self._blocks.popleft()

    def clear(self):
        with self._lock:
            self._blocks.clear()
            self._last_end = 0.0

    def level(self, lookback_sec: float = 0.25) -> float:
        """Max playback RMS over the last ``lookback_sec`` (covers device latency)."""
        now = time.monotonic()
        lo = now - lookback_sec
        with self._lock:
            levels = [rms for (s, e, rms) in self._blocks if e >= lo and s <= now + 0.05]
        return max(levels) if levels else 0.0

    def active(self, tail_sec: float = 0.35) -> bool:
        """True while audio is playing (plus a short acoustic tail)."""
        with self._lock:
            return time.monotonic() < self._last_end + tail_sec


class EchoGate:
    """Decides whether mic energy during playback is the user or SAINT's echo.

    coupling = EMA of (mic_rms / playback_rms) measured on frames that are
    *not* barge-in candidates. The gate opens when
        mic_rms > coupling * playback_rms * margin + floor
    for ``min_ms`` of consecutive frames.
    """

    def __init__(self, margin: float = 2.5, min_ms: int = 240, frame_ms: int = 30,
                 floor: float = 0.01, initial_coupling: float = 0.5):
        self.margin = margin
        self.min_frames = max(1, int(min_ms / frame_ms))
        self.floor = floor
        self.coupling = initial_coupling
        self._run = 0

    def reset_run(self):
        self._run = 0

    def update(self, mic_rms: float, playback_rms: float) -> bool:
        """Feed one frame. Returns True when a barge-in should fire."""
        predicted = self.coupling * playback_rms * self.margin + self.floor
        if mic_rms > predicted:
            self._run += 1
        else:
            self._run = 0
            # Learn the echo path only from frames we believe are pure echo.
            if playback_rms > 0.01:
                ratio = min(4.0, mic_rms / playback_rms)
                self.coupling = 0.97 * self.coupling + 0.03 * ratio
        if self._run >= self.min_frames:
            self._run = 0
            return True
        return False


playback_monitor = PlaybackMonitor()

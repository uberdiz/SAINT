"""
modules/voice/vad.py

Voice Activity Detection.

Two implementations:
  RmsVAD  — simple RMS energy threshold; zero extra dependencies.
  SileroVAD — higher accuracy (optional; requires silero-vad package).

The VoiceModule uses RmsVAD by default and will switch to Silero if
available. Both expose the same interface: feed(chunk) -> bool.
"""

import collections
import math
import threading
from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# RMS energy VAD
# ---------------------------------------------------------------------------
class RmsVAD:
    """
    Lightweight RMS-based voice activity detector with hysteresis.

    Maintains a noise floor estimate via a running minimum over short
    windows. Speech is detected when RMS exceeds (noise_floor + start_threshold).
    Speech ends when RMS falls below (noise_floor + end_threshold).
    """

    def __init__(
        self,
        threshold: float = 0.015,
        noise_suppression: bool = True,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        start_threshold: float = None,
        end_threshold: float = None,
    ):
        self.threshold = threshold
        self.start_threshold = start_threshold if start_threshold is not None else threshold
        self.end_threshold = end_threshold if end_threshold is not None else threshold * 0.5
        self.noise_suppression = noise_suppression
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self._frame_len = int(sample_rate * frame_ms / 1000)

        # Rolling noise floor estimate (10-second window)
        self._noise_window: collections.deque = collections.deque(
            maxlen=int(10_000 / frame_ms)
        )
        self._noise_floor: float = 0.0

        # Hysteresis state
        self._in_speech: bool = False

    def feed(self, chunk: np.ndarray) -> bool:
        """Return True if voice is detected in this chunk."""
        rms = self._rms(chunk)

        if self.noise_suppression:
            self._noise_window.append(rms)
            if self._noise_window:
                self._noise_floor = min(self._noise_window)

            effective_rms = max(0.0, rms - self._noise_floor)
        else:
            effective_rms = rms

        # Hysteresis: different thresholds for entering vs leaving speech
        if self._in_speech:
            # Currently in speech - use lower end threshold to stay in speech
            is_voice = effective_rms > self.end_threshold
            if not is_voice:
                self._in_speech = False
        else:
            # Currently in silence - use higher start threshold to enter speech
            is_voice = effective_rms > self.start_threshold
            if is_voice:
                self._in_speech = True

        return is_voice

    def reset(self):
        """Reset hysteresis state."""
        self._in_speech = False

    @staticmethod
    def _rms(chunk: np.ndarray) -> float:
        if len(chunk) == 0:
            return 0.0
        if chunk.dtype == np.int16:
            chunk = chunk.astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(chunk ** 2)))

    def get_level(self, chunk: np.ndarray) -> float:
        """Return 0-1 normalised audio level for the waveform meter."""
        rms = self._rms(chunk)
        # Normalise: typical speech RMS for 16-bit audio is ~0.05–0.3
        return min(1.0, rms / 0.3)


# ---------------------------------------------------------------------------
# Silero VAD (optional upgrade)
# ---------------------------------------------------------------------------
class SileroVAD:
    """
    Wrapper around silero-vad for more accurate detection.
    Falls back gracefully to RmsVAD if the model can't be loaded.
    """

    def __init__(self, threshold: float = 0.5, sample_rate: int = 16000):
        self._rms_fallback = RmsVAD(threshold=0.015)
        self._model = None
        self._utils = None
        self._threshold = threshold
        self._sample_rate = sample_rate
        self._ready = False
        self._load()

    def _load(self):
        try:
            import torch  # noqa: F401
            model, utils = torch.hub.load(  # type: ignore
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                trust_repo=True,
            )
            self._model = model
            self._utils = utils
            self._ready = True
        except Exception:
            self._ready = False

    def feed(self, chunk: np.ndarray) -> bool:
        if not self._ready or self._model is None:
            return self._rms_fallback.feed(chunk)
        try:
            import torch
            tensor = torch.from_numpy(chunk.astype(np.float32))
            conf = self._model(tensor, self._sample_rate).item()
            return conf > self._threshold
        except Exception:
            return self._rms_fallback.feed(chunk)

    def get_level(self, chunk: np.ndarray) -> float:
        return self._rms_fallback.get_level(chunk)


def make_vad(threshold: float = 0.015, noise_suppression: bool = True) -> RmsVAD:
    """Factory — returns best available VAD with hysteresis thresholds from config."""
    from core.config import config
    start_threshold = config.get("voice.vad_start_threshold", threshold)
    end_threshold = config.get("voice.vad_end_threshold", threshold * 0.5)
    return RmsVAD(
        threshold=threshold,
        noise_suppression=noise_suppression,
        start_threshold=start_threshold,
        end_threshold=end_threshold,
    )

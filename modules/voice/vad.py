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


class SileroOnnxVAD:
    """Streaming Silero VAD (v6 ONNX, bundled with faster-whisper).

    Unlike an energy threshold it tells *speech* from music, TV and fans, so
    with music playing an utterance still ends when the user stops talking.
    Runs on the CPU in ~0.1 ms per 32 ms window. The RMS VAD is kept as the
    source of the level meter and as a floor so near-silent frames never count.
    Same interface as RmsVAD: feed(chunk) -> bool, get_level(), reset().
    """

    WINDOW = 512       # samples per inference at 16 kHz
    CONTEXT = 64

    def __init__(self, start_prob: float = 0.5, end_prob: float = 0.35, min_rms: float = 0.004,
                 model_path: Optional[str] = None):
        import onnxruntime as ort
        if model_path is None:
            import faster_whisper
            import os
            model_path = os.path.join(os.path.dirname(faster_whisper.__file__), "assets", "silero_vad_v6.onnx")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 4
        self._sess = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
        self.start_prob = float(start_prob)
        self.end_prob = float(end_prob)
        self.min_rms = float(min_rms)
        self.threshold = self.min_rms           # compatibility with RmsVAD users
        self._rms = RmsVAD(threshold=min_rms)
        self.reset()

    def reset(self):
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._ctx = np.zeros(self.CONTEXT, dtype=np.float32)
        self._buf = np.zeros(0, dtype=np.float32)
        self._in_speech = False
        self.prob = 0.0

    def feed(self, chunk: np.ndarray) -> bool:
        if chunk.dtype == np.int16:
            chunk = chunk.astype(np.float32) / 32768.0
        rms = RmsVAD._rms(chunk)
        self._buf = np.concatenate((self._buf, chunk.astype(np.float32)))
        while len(self._buf) >= self.WINDOW:
            win, self._buf = self._buf[:self.WINDOW], self._buf[self.WINDOW:]
            x = np.concatenate((self._ctx, win))[None, :]
            self._ctx = win[-self.CONTEXT:]
            out, self._h, self._c = self._sess.run(None, {"input": x, "h": self._h, "c": self._c})
            self.prob = float(np.asarray(out).reshape(-1)[-1])
        if self._in_speech:
            self._in_speech = self.prob >= self.end_prob and rms >= self.min_rms * 0.5
        else:
            self._in_speech = self.prob >= self.start_prob and rms >= self.min_rms
        return self._in_speech

    def get_level(self, chunk: np.ndarray) -> float:
        return self._rms.get_level(chunk)


def make_vad(threshold: float = 0.015, noise_suppression: bool = True):
    """Factory — Silero (speech-vs-noise model) when available, else RMS."""
    from core.config import config
    import logging
    if config.get("voice.vad_backend", "silero") == "silero":
        try:
            vad = SileroOnnxVAD(start_prob=config.get("voice.vad_speech_prob", 0.5),
                                end_prob=config.get("voice.vad_speech_prob", 0.5) * 0.7,
                                min_rms=min(threshold, 0.006))
            logging.getLogger("saint.voice").info("voice.vad backend=silero")
            return vad
        except Exception as e:
            logging.getLogger("saint.voice").warning("voice.vad silero unavailable (%s) - using RMS", e)
    start_threshold = config.get("voice.vad_start_threshold", threshold)
    end_threshold = config.get("voice.vad_end_threshold", threshold * 0.5)
    return RmsVAD(
        threshold=threshold,
        noise_suppression=noise_suppression,
        start_threshold=start_threshold,
        end_threshold=end_threshold,
    )

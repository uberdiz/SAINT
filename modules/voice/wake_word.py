"""
modules/voice/wake_word.py

Local wake-word detection for "SAINT" / "Hey SAINT".

The detector runs SAINT's custom openWakeWord model (data/wake/hey_saint.onnx)
directly on onnxruntime, on the CPU. It re-implements openWakeWord's streaming
feature pipeline (the same three-stage design, same constants):

    16 kHz int16 audio (80 ms / 1280-sample frames)
      -> melspectrogram.onnx      (32 mel bins, 10 ms hop; spec/10 + 2)
      -> embedding_model.onnx     (76-frame mel window -> 96-d embedding)
      -> hey_saint.onnx           (last 16 embeddings -> sigmoid score)

Why not ``import openwakeword``? That package imports scikit-learn at import
time, and on machines with Windows Application Control / Smart App Control
sklearn's compiled DLLs can be blocked, which silently disabled the wake word.
Running the three ONNX graphs directly avoids that dependency entirely, keeps
per-frame cost at roughly a millisecond, and never touches the GPU.

Feature models (melspectrogram.onnx, embedding_model.onnx) come from the
openWakeWord project (Apache-2.0) and are shipped in data/wake/.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from core.paths import resolve_project_path, display_path

log = logging.getLogger("saint.wake")

WAKE_SAMPLE_RATE = 16000
WAKE_FRAME_SAMPLES = 1280          # 80 ms
_MEL_CONTEXT = 160 * 3             # extra samples the melspectrogram needs for continuity
_MEL_WINDOW = 76                   # mel frames per embedding window
_MEL_MAX = 10 * 97                 # ~10 s of mel frames kept
_FEATURE_MAX = 120                 # ~10 s of embeddings kept


@dataclass
class WakeStatus:
    ready: bool
    error: str = ""                # human-readable reason when not ready
    code: str = ""                 # machine-readable: MODEL_MISSING, RUNTIME_MISSING, ...
    model_path: str = ""
    threshold: float = 0.5


class WakeWordError(RuntimeError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class OnnxWakeWordDetector:
    """Streaming wake-word detector. Thread-safe; feed() from the audio thread."""

    def __init__(
        self,
        model_path: str = "data/wake/hey_saint.onnx",
        feature_dir: str = "data/wake",
        threshold: float = 0.5,
        refractory_sec: float = 2.0,
        trigger_frames: int = 1,
        keyword: str = "saint",
    ):
        self.keyword = keyword
        self.threshold = float(threshold)
        self.refractory_sec = float(refractory_sec)
        self.trigger_frames = max(1, int(trigger_frames))
        self._model_path = resolve_project_path(model_path)
        self._feature_dir = resolve_project_path(feature_dir)
        self._lock = threading.Lock()
        self._status = WakeStatus(ready=False, model_path=display_path(self._model_path),
                                  threshold=self.threshold)

        self._mel = self._emb = self._clf = None
        self._clf_input = "x"
        self._n_features = 16

        # Streaming state
        self._pending = np.empty(0, dtype=np.int16)
        self._raw = collections.deque(maxlen=WAKE_SAMPLE_RATE * 10)
        self._mel_buf = np.ones((_MEL_WINDOW, 32), dtype=np.float32)
        self._feat_buf = None
        self._above = 0
        self._last_fire = 0.0
        self.last_score = 0.0
        self.peak_score = 0.0          # max score since last read (for the UI meter)
        self.frames_processed = 0
        self.total_infer_ms = 0.0

        try:
            self._load()
            self._status = WakeStatus(ready=True, model_path=display_path(self._model_path),
                                      threshold=self.threshold)
            log.info("wake.ready model=%s threshold=%.2f trigger_frames=%d refractory=%.1fs",
                     display_path(self._model_path), self.threshold,
                     self.trigger_frames, self.refractory_sec)
        except WakeWordError as e:
            self._status = WakeStatus(ready=False, error=str(e), code=e.code,
                                      model_path=display_path(self._model_path),
                                      threshold=self.threshold)
            log.error("wake.unavailable code=%s detail=%s", e.code, e)

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #
    def _load(self):
        try:
            import onnxruntime as ort
        except Exception as e:  # pragma: no cover - depends on install
            raise WakeWordError(
                f"onnxruntime is not available ({e}). Install it with: pip install onnxruntime",
                "RUNTIME_MISSING") from e

        if not self._model_path.exists():
            raise WakeWordError(
                f"Wake-word model not found at {display_path(self._model_path)}. "
                "Place hey_saint.onnx there or set Settings > Wake Word > Model path.",
                "MODEL_MISSING")
        mel_path = self._feature_dir / "melspectrogram.onnx"
        emb_path = self._feature_dir / "embedding_model.onnx"
        for p in (mel_path, emb_path):
            if not p.exists():
                raise WakeWordError(
                    f"Wake-word feature model missing: {display_path(p)}. It ships in "
                    "data/wake/ (from the openWakeWord project).",
                    "FEATURES_MISSING")

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        providers = ["CPUExecutionProvider"]
        try:
            self._mel = ort.InferenceSession(str(mel_path), sess_options=opts, providers=providers)
            self._emb = ort.InferenceSession(str(emb_path), sess_options=opts, providers=providers)
            self._clf = ort.InferenceSession(str(self._model_path), sess_options=opts, providers=providers)
        except Exception as e:
            msg = str(e)
            if "External data" in msg or ".data" in msg:
                raise WakeWordError(
                    f"The wake-word model references an external weights file that is missing "
                    f"({msg[:160]}). Re-export it as a single file.", "MODEL_INVALID") from e
            raise WakeWordError(f"Could not load wake-word model: {msg[:200]}", "MODEL_INVALID") from e

        inp = self._clf.get_inputs()[0]
        self._clf_input = inp.name
        shape = inp.shape
        if len(shape) != 3 or shape[2] != 96:
            raise WakeWordError(
                f"Unexpected wake-word model input shape {shape}; expected [1, N, 96] "
                "(an openWakeWord classifier).", "MODEL_INVALID")
        self._n_features = int(shape[1]) if isinstance(shape[1], int) else 16
        self._reset_features()

    def _reset_features(self):
        # openWakeWord primes the embedding buffer with low-level noise so the
        # classifier has a full window from the first frame.
        rng = np.random.default_rng(1234)
        noise = rng.integers(-1000, 1000, WAKE_SAMPLE_RATE * 4).astype(np.int16)
        self._feat_buf = self._embed_clip(noise)
        self._mel_buf = np.ones((_MEL_WINDOW, 32), dtype=np.float32)
        self._raw.clear()
        self._pending = np.empty(0, dtype=np.int16)
        self._above = 0

    # ------------------------------------------------------------------ #
    # Feature pipeline
    # ------------------------------------------------------------------ #
    def _melspec(self, samples_i16: np.ndarray) -> np.ndarray:
        x = samples_i16.astype(np.float32)[None, :]
        out = self._mel.run(None, {"input": x})[0]
        return np.squeeze(out).astype(np.float32) / 10.0 + 2.0

    def _embed_clip(self, samples_i16: np.ndarray) -> np.ndarray:
        spec = self._melspec(samples_i16)
        windows = [spec[i:i + _MEL_WINDOW] for i in range(0, spec.shape[0], 8)
                   if spec[i:i + _MEL_WINDOW].shape[0] == _MEL_WINDOW]
        batch = np.expand_dims(np.array(windows), axis=-1).astype(np.float32)
        return self._emb.run(None, {"input_1": batch})[0].squeeze()

    def _process_frame(self, frame: np.ndarray) -> float:
        self._raw.extend(frame.tolist())
        raw = np.fromiter(self._raw, dtype=np.int16, count=len(self._raw))
        spec = self._melspec(raw[-(WAKE_FRAME_SAMPLES + _MEL_CONTEXT):])
        self._mel_buf = np.vstack((self._mel_buf, spec))[-_MEL_MAX:]
        window = self._mel_buf[-_MEL_WINDOW:].astype(np.float32)[None, :, :, None]
        emb = self._emb.run(None, {"input_1": window})[0].reshape(1, 96)
        self._feat_buf = np.vstack((self._feat_buf, emb))[-_FEATURE_MAX:]
        feats = self._feat_buf[-self._n_features:][None, :, :].astype(np.float32)
        return float(self._clf.run(None, {self._clf_input: feats})[0].reshape(-1)[0])

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    @property
    def ready(self) -> bool:
        return self._status.ready

    @property
    def status(self) -> WakeStatus:
        return self._status

    def feed(self, chunk_i16: np.ndarray) -> Optional[float]:
        """Feed 16 kHz int16 audio of any length.

        Returns the score when the wake word fires (score >= threshold for
        ``trigger_frames`` consecutive frames, outside the refractory period),
        otherwise None.
        """
        if not self._status.ready or chunk_i16 is None or len(chunk_i16) == 0:
            return None
        if chunk_i16.dtype != np.int16:
            chunk_i16 = (np.clip(chunk_i16, -1.0, 1.0) * 32767.0).astype(np.int16)

        fired: Optional[float] = None
        with self._lock:
            self._pending = np.concatenate((self._pending, chunk_i16))
            while len(self._pending) >= WAKE_FRAME_SAMPLES:
                frame = self._pending[:WAKE_FRAME_SAMPLES]
                self._pending = self._pending[WAKE_FRAME_SAMPLES:]
                t0 = time.perf_counter()
                try:
                    score = self._process_frame(frame)
                except Exception as e:  # never kill the audio loop
                    log.debug("wake.predict_error %s", e)
                    continue
                self.total_infer_ms += (time.perf_counter() - t0) * 1000
                self.frames_processed += 1
                self.last_score = score
                self.peak_score = max(self.peak_score, score)

                if score >= self.threshold:
                    self._above += 1
                else:
                    self._above = 0
                if self._above >= self.trigger_frames:
                    now = time.monotonic()
                    if now - self._last_fire >= self.refractory_sec:
                        self._last_fire = now
                        fired = score if fired is None else max(fired, score)
                    self._above = 0
        return fired

    def take_peak(self) -> float:
        """Return and reset the peak score (used for throttled UI updates)."""
        with self._lock:
            peak, self.peak_score = self.peak_score, 0.0
        return peak

    def suppress(self, seconds: float):
        """Ignore detections for a while (e.g. while SAINT itself says 'SAINT')."""
        with self._lock:
            self._last_fire = max(self._last_fire, time.monotonic() + seconds - self.refractory_sec)

    def reset(self):
        if not self._status.ready:
            return
        with self._lock:
            self._reset_features()

    @property
    def avg_infer_ms(self) -> float:
        return self.total_infer_ms / self.frames_processed if self.frames_processed else 0.0


# Backwards-compatible name used by older code/tests.
OpenWakeWordDetector = OnnxWakeWordDetector


def make_wake_word_detector() -> Optional[OnnxWakeWordDetector]:
    """Factory driven by config. Returns None when the wake word is disabled."""
    from core.config import config

    if not config.get("voice.wake_word_enabled", True):
        return None
    return OnnxWakeWordDetector(
        model_path=config.get("voice.wake_word_model_path", "data/wake/hey_saint.onnx"),
        feature_dir=config.get("voice.wake_word_feature_dir", "data/wake"),
        threshold=config.get("voice.wake_word_threshold", 0.5),
        refractory_sec=config.get("voice.wake_word_refractory_sec", 2.0),
        trigger_frames=config.get("voice.wake_word_trigger_frames", 1),
        keyword=config.get("voice.wake_word", "saint"),
    )

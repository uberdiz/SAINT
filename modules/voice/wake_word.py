"""
modules/voice/wake_word.py

Wake-word detection for SAINT ("SAINT" / "Hey SAINT").

Backend: openWakeWord (https://github.com/dscripka/openWakeWord) — Apache-2.0,
fully offline, ONNX inference. It runs alongside the always-on VAD/STT path:
the wake word does NOT gate the microphone. Instead, a detection opens a short
"wake window" during which the STT activation gate is bypassed, so an explicit
"Hey SAINT, skip this song" is always acted on, while un-prompted commands still
flow through the normal confidence gate.

openWakeWord ships pretrained models (alexa, hey_jarvis, hey_mycroft, ...), but
"SAINT" / "Hey SAINT" is NOT one of them — you must supply a custom-trained
model via `voice.wake_word_model_path`. Until one is present this detector loads
in a disabled state and logs how to get one, rather than crashing the pipeline.

Interface mirrors vad.py / stt.py:
    detector = make_wake_word_detector()
    score = detector.feed(chunk_int16_16k)   # float score if fired, else None
"""

import logging
import os
import threading
import time
from typing import Optional

import numpy as np

log = logging.getLogger("saint.voice.wake")

# openWakeWord expects 16 kHz, 16-bit PCM. 1280 samples == 80 ms is the
# documented per-call frame size for streaming inference.
WAKE_SAMPLE_RATE = 16000
WAKE_FRAME_SAMPLES = 1280

# Names of openWakeWord's own pretrained models. "saint" is intentionally absent.
_PRETRAINED = {
    "alexa", "hey_mycroft", "hey_jarvis", "hey_rhasspy",
    "timer", "weather",
}


class OpenWakeWordDetector:
    """Streaming wake-word detector backed by openWakeWord (ONNX).

    Graceful by design: if the package or model is unavailable the detector is
    marked not-ready and `feed()` becomes a no-op, so the voice pipeline keeps
    working without wake-word support.
    """

    def __init__(
        self,
        model_path: str = "",
        keyword: str = "saint",
        threshold: float = 0.5,
        refractory_sec: float = 2.0,
        inference_framework: str = "onnx",
    ):
        self.keyword = keyword
        self.threshold = float(threshold)
        self.refractory_sec = float(refractory_sec)
        self._model = None
        self._model_keys: list[str] = []
        self._ready = False
        self._buf = np.empty(0, dtype=np.int16)
        self._last_fire = 0.0
        self._lock = threading.Lock()
        self._load(model_path, inference_framework)

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #
    def _load(self, model_path: str, inference_framework: str):
        try:
            from openwakeword.model import Model  # type: ignore
            import openwakeword.utils as oww_utils  # type: ignore
        except Exception as e:
            log.warning(
                "wake.disabled reason=openwakeword_not_installed detail=%s "
                "(run: pip install openwakeword)", e
            )
            return

        # openWakeWord needs its shared melspectrogram + embedding feature
        # models. download_models() is a no-op once they are cached.
        try:
            oww_utils.download_models()
        except Exception as e:
            log.warning("wake.feature_model_download_failed detail=%s", e)

        wakeword_models: list[str] = []
        if model_path and os.path.exists(model_path):
            wakeword_models = [model_path]
            log.info("wake.model.custom path=%s", model_path)
        elif self.keyword in _PRETRAINED:
            wakeword_models = [self.keyword]
            log.info("wake.model.pretrained name=%s", self.keyword)
        else:
            log.warning(
                "wake.disabled reason=no_model keyword=%r — '%s' is not a "
                "pretrained openWakeWord model. Train a custom model and set "
                "voice.wake_word_model_path to its .onnx file. See "
                "https://github.com/dscripka/openWakeWord#training-new-models",
                self.keyword, self.keyword,
            )
            return

        try:
            self._model = Model(
                wakeword_models=wakeword_models,
                inference_framework=inference_framework,
            )
            # Keys in the prediction dict are the model basenames (custom) or
            # the pretrained name — track them so we can read the right score.
            self._model_keys = list(self._model.models.keys())
            self._ready = True
            log.info(
                "wake.ready backend=openwakeword framework=%s keys=%s thr=%.2f",
                inference_framework, self._model_keys, self.threshold,
            )
        except Exception as e:
            log.error("wake.load_failed detail=%s", e)
            self._model = None
            self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    # ------------------------------------------------------------------ #
    # Streaming inference
    # ------------------------------------------------------------------ #
    def feed(self, chunk_i16: np.ndarray) -> Optional[float]:
        """Feed a 16 kHz int16 audio chunk (any length).

        Returns the detection score (>= threshold) when the wake word fires,
        otherwise None. Audio is internally buffered into 1280-sample frames.
        """
        if not self._ready or self._model is None:
            return None
        if chunk_i16 is None or len(chunk_i16) == 0:
            return None

        if chunk_i16.dtype != np.int16:
            # Accept float32 in [-1, 1] too.
            chunk_i16 = (np.clip(chunk_i16, -1.0, 1.0) * 32767.0).astype(np.int16)

        with self._lock:
            self._buf = np.concatenate((self._buf, chunk_i16))
            best: Optional[float] = None
            while len(self._buf) >= WAKE_FRAME_SAMPLES:
                frame = self._buf[:WAKE_FRAME_SAMPLES]
                self._buf = self._buf[WAKE_FRAME_SAMPLES:]
                score = self._predict_frame(frame)
                if score is not None and (best is None or score > best):
                    best = score
            return best

    def _predict_frame(self, frame: np.ndarray) -> Optional[float]:
        try:
            preds = self._model.predict(frame)
        except Exception as e:
            log.debug("wake.predict_error detail=%s", e)
            return None

        # Highest score across our tracked model(s).
        score = 0.0
        for key in self._model_keys:
            score = max(score, float(preds.get(key, 0.0)))

        if score < self.threshold:
            return None

        now = time.perf_counter()
        if now - self._last_fire < self.refractory_sec:
            return None  # debounce repeated fires from one utterance
        self._last_fire = now
        return score

    def reset(self):
        with self._lock:
            self._buf = np.empty(0, dtype=np.int16)
        if self._ready and self._model is not None:
            try:
                self._model.reset()
            except Exception:
                pass


def make_wake_word_detector() -> Optional[OpenWakeWordDetector]:
    """Factory driven by config. Returns None when wake word is disabled."""
    from core.config import config

    if not config.get("voice.wake_word_enabled", False):
        return None

    detector = OpenWakeWordDetector(
        model_path=config.get("voice.wake_word_model_path", ""),
        keyword=config.get("voice.wake_word", "saint"),
        threshold=config.get("voice.wake_word_sensitivity", 0.5),
        refractory_sec=config.get("voice.wake_word_refractory_sec", 2.0),
        inference_framework=config.get("voice.wake_word_inference_framework", "onnx"),
    )
    return detector

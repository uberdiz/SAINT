"""
modules/voice/stt.py

Speech-to-Text engine abstraction.

Backends:
  FasterWhisperSTT — local Whisper via faster-whisper (CUDA float16 on GPU)
  MockSTT          — simulates transcription word-by-word; no audio hardware

Both expose:
  transcribe(audio_np: np.ndarray) -> STTResult
"""

import time
import threading
from dataclasses import dataclass, field
from typing import Optional, Callable

import numpy as np


@dataclass
class STTResult:
    text: str
    confidence: float   # 0.0 – 1.0
    language: str = "en"
    latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class STTEngine:
    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> STTResult:
        raise NotImplementedError

    def warm_up(self):
        """Optional: run a dummy pass to pre-load GPU kernels."""
        pass


# ---------------------------------------------------------------------------
# faster-whisper (CUDA)
# ---------------------------------------------------------------------------
class FasterWhisperSTT(STTEngine):
    """
    Uses the faster-whisper library (CTranslate2 backend) with CUDA.
    Model is loaded once on first use and cached for the session.
    """

    def __init__(
        self,
        model_name: str = "base.en",
        device: str = "cuda",
        compute_type: str = "float16",
        language: str = "en",
        hotwords: str = "",
    ):
        self._hotwords = hotwords or None   # biases decoding toward e.g. "SAINT"
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return
        try:
            from faster_whisper import WhisperModel  # type: ignore
            self._model = WhisperModel(
                self._model_name,
                device=self._device,
                compute_type=self._compute_type,
            )
        except Exception as e:
            err_str = str(e).lower()
            if self._device == "cuda" and ("cublas" in err_str or "cudnn" in err_str or "cuda" in err_str):
                import logging
                logging.warning(f"CUDA failed to load ({e}), falling back to CPU.")
                try:
                    self._device = "cpu"
                    self._compute_type = "int8"
                    self._model = WhisperModel(
                        self._model_name,
                        device=self._device,
                        compute_type=self._compute_type,
                    )
                    return
                except Exception as e2:
                    raise RuntimeError(f"Could not load faster-whisper model on CPU fallback: {e2}") from e2
            raise RuntimeError(f"Could not load faster-whisper model: {e}") from e

    def warm_up(self):
        """Run a silent dummy transcription to load CUDA kernels."""
        self._load()
        silence = np.zeros(16000, dtype=np.float32)
        self.transcribe(silence)

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> STTResult:
        with self._lock:
            self._load()

        audio_f32 = audio.astype(np.float32)
        # int16 arrays from sounddevice range from -32768 to 32767. Whisper needs [-1.0, 1.0].
        # Unconditionally normalise here to prevent distorted audio from reaching the model.
        audio_f32 = audio_f32 / 32768.0

        t0 = time.perf_counter()
        try:
            segments, info = self._model.transcribe(
                audio_f32,
                language=self._language if self._language != "auto" else None,
                beam_size=5,
                vad_filter=False,  # we do our own VAD
                hotwords=self._hotwords,
            )
            text_parts = []
            avg_logprob = 0.0
            count = 0
            for seg in segments:
                text_parts.append(seg.text.strip())
                avg_logprob += seg.avg_logprob
                count += 1

            text = " ".join(text_parts).strip()
            confidence = max(0.0, min(1.0, (avg_logprob / count + 1.0) if count else 0.0))
            language = getattr(info, "language", self._language)
        except Exception as e:
            err_str = str(e).lower()
            if self._device == "cuda" and ("cublas" in err_str or "cudnn" in err_str or "cuda" in err_str):
                import logging
                logging.warning(f"CUDA crashed during transcribe ({e}), falling back to CPU.")
                with self._lock:
                    self._device = "cpu"
                    self._compute_type = "int8"
                    from faster_whisper import WhisperModel
                    self._model = WhisperModel(
                        self._model_name,
                        device=self._device,
                        compute_type=self._compute_type,
                    )
                # Retry once on CPU
                return self.transcribe(audio, sample_rate)

            raise RuntimeError(f"STT transcription error: {e}") from e

        latency_ms = (time.perf_counter() - t0) * 1000
        return STTResult(
            text=text,
            confidence=confidence,
            language=language,
            latency_ms=round(latency_ms, 1),
        )


# ---------------------------------------------------------------------------
# Mock STT (for tests — no hardware required)
# ---------------------------------------------------------------------------
class MockSTT(STTEngine):
    """
    Simulates STT by accepting pre-programmed utterances.
    Call queue_utterance(text) before feeding audio.
    """

    def __init__(self, word_delay: float = 0.02):
        self._queue = []
        self._word_delay = word_delay
        self._lock = threading.Lock()
        # Partial callback — called with each word as it "arrives"
        self.on_partial: Optional[Callable[[str], None]] = None

    def queue_utterance(self, text: str, confidence: float = 0.95):
        with self._lock:
            self._queue.append((text, confidence))

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> STTResult:
        with self._lock:
            if self._queue:
                text, confidence = self._queue.pop(0)
            else:
                text = ""
                confidence = 0.0

        # Simulate partial transcript emissions
        if self.on_partial and text:
            words = text.split()
            accumulated = []
            for word in words:
                accumulated.append(word)
                self.on_partial(" ".join(accumulated))
                time.sleep(self._word_delay)

        return STTResult(
            text=text,
            confidence=confidence,
            language="en",
            latency_ms=len(text.split()) * self._word_delay * 1000,
        )


def make_stt(backend: str = "faster_whisper", **kwargs) -> STTEngine:
    """Factory."""
    if backend == "faster_whisper":
        return FasterWhisperSTT(**kwargs)
    return MockSTT()

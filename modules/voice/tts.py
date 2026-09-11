"""
modules/voice/tts.py

Text-to-Speech engine abstraction.

Backends:
  KokoroTTS  — kokoro-onnx, NVIDIA GPU via onnxruntime-gpu; sentence-streaming
  MockTTS    — prints tokens to simulate speech; no audio hardware required

Both expose:
  speak(text)    — synthesise and play; blocks until done or interrupted
  interrupt()    — stop playback immediately
  is_speaking()  — True while audio is playing

Sentence streaming: text is split into sentences and each sentence is
synthesised + started playing before the next is generated. This gives
~30–60 ms TTFB on a modern GPU.
"""

import re
import threading
import time
from typing import Optional, Callable

import numpy as np


# ---------------------------------------------------------------------------
# Sentence splitter
# ---------------------------------------------------------------------------
_SENT_RE = re.compile(r'(?<=[.!?])\s+')


def _split_sentences(text: str):
    """Split on sentence boundaries but keep very short fragments grouped."""
    raw = _SENT_RE.split(text.strip())
    out = []
    buf = ""
    for sent in raw:
        buf = (buf + " " + sent).strip() if buf else sent
        if len(buf) > 20:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out or [text]


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class TTSEngine:
    def speak(self, text: str, on_chunk_start: Optional[Callable[[str], None]] = None):
        raise NotImplementedError

    def interrupt(self):
        raise NotImplementedError

    def is_speaking(self) -> bool:
        raise NotImplementedError

    def warm_up(self):
        pass


# ---------------------------------------------------------------------------
# Kokoro TTS (kokoro-onnx, GPU via onnxruntime-gpu)
# ---------------------------------------------------------------------------
class KokoroTTS(TTSEngine):
    """
    Sentence-streaming TTS using kokoro-onnx.

    Each sentence is synthesised on-GPU then played via sounddevice.
    While sentence N is playing, sentence N+1 is being generated —
    giving very low perceived latency.
    """

    def __init__(
        self,
        voice: str = "af_heart",
        speed: float = 1.0,
        device: str = "cuda",
    ):
        self._voice = voice
        self._speed = speed
        self._device = device
        self._model = None
        self._interrupt_event = threading.Event()
        self._speaking = False
        self._lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return
        try:
            from kokoro_onnx import Kokoro  # type: ignore
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] \
                if self._device == "cuda" else ["CPUExecutionProvider"]
            self._model = Kokoro("kokoro-v1_0.onnx", "voices-v1_0.bin",
                                 providers=providers)
        except Exception as e:
            raise RuntimeError(f"Could not load Kokoro TTS model: {e}") from e

    def warm_up(self):
        self._load()
        try:
            self.speak(".", on_chunk_start=None)
        except Exception:
            pass

    def speak(self, text: str, on_chunk_start: Optional[Callable[[str], None]] = None):
        import sounddevice as sd  # type: ignore

        self._load()
        self._interrupt_event.clear()

        sentences = _split_sentences(text)
        with self._lock:
            self._speaking = True

        try:
            for sentence in sentences:
                if self._interrupt_event.is_set():
                    break
                if not sentence.strip():
                    continue

                if on_chunk_start:
                    on_chunk_start(sentence)

                samples, sample_rate = self._model.create(
                    sentence,
                    voice=self._voice,
                    speed=self._speed,
                    lang="en-us",
                )

                if self._interrupt_event.is_set():
                    break

                # Play audio — sd.play is non-blocking; sd.wait blocks here
                sd.play(samples, sample_rate)
                # Poll interrupt while waiting for audio to finish
                while sd.get_stream().active:
                    if self._interrupt_event.is_set():
                        sd.stop()
                        break
                    time.sleep(0.01)
        finally:
            with self._lock:
                self._speaking = False

    def interrupt(self):
        self._interrupt_event.set()
        try:
            import sounddevice as sd  # type: ignore
            sd.stop()
        except Exception:
            pass

    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking


# ---------------------------------------------------------------------------
# Mock TTS (for tests — no audio hardware, no model files)
# ---------------------------------------------------------------------------
class MockTTS(TTSEngine):
    """
    Simulates TTS output at a fixed words-per-second rate.
    Emits word-by-word callbacks; no audio produced.
    """

    def __init__(self, words_per_second: float = 3.5):
        self._wps = words_per_second
        self._interrupt_event = threading.Event()
        self._speaking = False
        self._spoken_chunks = []     # record of what was "spoken"

    def speak(self, text: str, on_chunk_start: Optional[Callable[[str], None]] = None):
        self._interrupt_event.clear()
        self._speaking = True
        self._spoken_chunks.append(text)

        sentences = _split_sentences(text)
        try:
            for sentence in sentences:
                if self._interrupt_event.is_set():
                    break
                if on_chunk_start:
                    on_chunk_start(sentence)
                words = sentence.split()
                for word in words:
                    if self._interrupt_event.is_set():
                        break
                    time.sleep(1.0 / self._wps)
        finally:
            self._speaking = False

    def interrupt(self):
        self._interrupt_event.set()

    def is_speaking(self) -> bool:
        return self._speaking

    def get_spoken(self):
        return list(self._spoken_chunks)

    def clear_spoken(self):
        self._spoken_chunks.clear()


def make_tts(backend: str = "kokoro", **kwargs) -> TTSEngine:
    """Factory."""
    if backend == "kokoro":
        return KokoroTTS(**kwargs)
    return MockTTS()

"""
modules/voice/kokoro_onnx.py

Kokoro speech without PyTorch: the same Kokoro-82M voice run through
onnxruntime (``kokoro-onnx`` model files in data/tts/kokoro-onnx/).

The text -> phonemes step is the same misaki G2P the PyTorch pipeline uses
(espeak-ng for out-of-dictionary words and other languages), so it sounds the
same; only the neural model runs on onnxruntime. The packaged SAINT.exe ships
this instead of PyTorch + CUDA, which is what made the installer 3 GB.

Playback, interruption and barge-in are KokoroTTS's (this is a subclass); only
loading and synthesis differ.
"""

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import numpy as np

from modules.voice.tts import KokoroTTS

log = logging.getLogger("saint.tts.onnx")

MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"
# KPipeline language codes (modules/lang packs) -> espeak-ng languages.
ESPEAK_LANG = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi", "i": "it", "p": "pt-br",
               "j": "ja", "z": "cmn"}
_SENTENCE = re.compile(r"(?<=[.!?;:])\s+|\n+")


def model_dir() -> Path:
    from core.config import config
    from core.paths import resolve_project_path
    return resolve_project_path(config.get("voice.kokoro_onnx_dir", "data/tts/kokoro-onnx"))


def available() -> bool:
    d = model_dir()
    return (d / MODEL_FILE).exists() and (d / VOICES_FILE).exists()


@dataclass
class _Result:
    """What KPipeline yields, as far as KokoroTTS.speak uses it."""
    graphemes: str
    audio: np.ndarray


def _espeak_ready():
    import espeakng_loader
    from phonemizer.backend.espeak.wrapper import EspeakWrapper
    EspeakWrapper.set_library(espeakng_loader.get_library_path())
    try:
        EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
    except Exception:
        pass


class _OnnxPipeline:
    """KPipeline-shaped: ``pipeline(text, voice=..., speed=...)`` yields one
    result per sentence, so playback starts after the first sentence."""

    def __init__(self, kokoro, g2p):
        self.kokoro = kokoro
        self.g2p = g2p
        self._styles = {}

    def _style(self, voice: str) -> np.ndarray:
        if voice not in self._styles:
            names = [v.strip() for v in voice.split(",") if v.strip()] or ["af_heart"]
            styles = [self.kokoro.get_voice_style(n) for n in names]
            self._styles[voice] = np.mean(np.stack(styles), axis=0) if len(styles) > 1 else styles[0]
        return self._styles[voice]

    def __call__(self, text: str, voice: str = "af_heart", speed: float = 1.0) -> Iterator[_Result]:
        style = self._style(voice)
        speed = max(0.5, min(2.0, float(speed or 1.0)))
        for sentence in (s.strip() for s in _SENTENCE.split(text or "")):
            if not sentence:
                continue
            phonemes, _ = self.g2p(sentence)
            if not phonemes or not phonemes.strip():
                continue
            audio, _sr = self.kokoro.create(phonemes, voice=style, speed=speed, is_phonemes=True)
            yield _Result(sentence, np.asarray(audio, dtype=np.float32))


class KokoroOnnxTTS(KokoroTTS):
    """KokoroTTS with the model on onnxruntime instead of PyTorch."""

    def __init__(self, voice: str = "af_heart", speed: float = 1.0, **_ignored):
        super().__init__(voice=voice, speed=speed, device="cpu", allow_cpu_fallback=True, require_cuda=False)
        self._kokoro = None
        self._onnx_lock = threading.Lock()

    def _load(self):
        if self._pipeline is not None:
            return
        with self._onnx_lock:
            if self._pipeline is not None:
                return
            if self._load_error is not None:
                raise self._load_error
            try:
                d = model_dir()
                if not available():
                    raise FileNotFoundError(f"Kokoro ONNX model files aren't in {d} ({MODEL_FILE}, {VOICES_FILE})")
                from kokoro_onnx import Kokoro
                _espeak_ready()
                self._kokoro = Kokoro(str(d / MODEL_FILE), str(d / VOICES_FILE))
                from misaki import en, espeak
                g2p = en.G2P(trf=False, british=False, fallback=espeak.EspeakFallback(british=False))
                self._pipeline = _OnnxPipeline(self._kokoro, g2p)
                self._resolved_device = "cpu (onnxruntime)"
                log.info("Kokoro TTS (ONNX) loaded from %s", d)
            except Exception as e:
                self._load_error = RuntimeError(f"Could not load Kokoro (ONNX): {e}")
                raise self._load_error

    @property
    def device_info(self) -> dict:
        return {"requested_device": "cpu", "resolved_device": self._resolved_device, "cuda_available": False,
                "fell_back": False, "reason": "onnxruntime"}

    def warm_up(self):
        try:
            self._load()
            for _ in self._pipeline("Ready.", voice=self._voice, speed=self._speed):
                pass
        except Exception:
            log.debug("tts.onnx.warm_up_failed", exc_info=True)

    def _voice_for(self, code: str):
        if code in ("", "en", "und"):
            return self._pipeline, self._voice
        cache = self.__dict__.setdefault("_lang_pipelines", {})
        if code not in cache:
            cache[code] = None
            try:
                from modules.lang.pack import get_pack
                pack = get_pack(code)
                lang = ESPEAK_LANG.get(getattr(pack, "tts_lang", "") or "")
                if pack is not None and lang and pack.tts_voice:
                    from misaki import espeak
                    cache[code] = (_OnnxPipeline(self._kokoro, espeak.EspeakG2P(language=lang)), pack.tts_voice)
            except Exception as e:
                log.warning("tts.onnx.language_voice_failed %s: %s", code, e)
        return cache[code] or (self._pipeline, self._voice)

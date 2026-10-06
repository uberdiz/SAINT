"""
modules/voice/sapi.py

The voice SAINT falls back to when Kokoro can't load: Windows' own speech
(SAPI 5 — "Microsoft David / Zira / Aria"), which every Windows PC has.

Kokoro needs its model files (downloaded on first run) and either PyTorch or
onnxruntime; when any of that is missing SAINT used to go silent for good
("TTS service in error state" on every reply). This keeps it talking while
Kokoro is downloaded or repaired.

Each sentence is synthesised into memory at 24 kHz and played through
KokoroTTS's own playback thread, so volume, whisper replies, barge-in (echo
monitoring) and interruption work exactly as with Kokoro.
"""

import logging
import re
import threading
from typing import Iterator

import numpy as np

from modules.voice.tts import KokoroTTS

log = logging.getLogger("saint.tts.sapi")

_SAFT24kHz16BitMono = 26                  # SpeechAudioFormatType
_SENTENCE = re.compile(r"(?<=[.!?;:])\s+|\n+")


class _Result:
    """What KPipeline yields, as far as KokoroTTS.speak uses it."""

    def __init__(self, graphemes: str, audio: np.ndarray):
        self.graphemes, self.audio = graphemes, audio


class _SapiPipeline:
    """KPipeline-shaped: ``pipeline(text, voice=..., speed=...)`` yields one result per sentence."""

    def __init__(self, voice_hint: str = ""):
        self._hint = (voice_hint or "").lower()
        self._local = threading.local()          # COM objects belong to the thread that made them

    def _voice(self):
        v = getattr(self._local, "voice", None)
        if v is None:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            v = win32com.client.Dispatch("SAPI.SpVoice")
            if self._hint:
                voices = v.GetVoices()
                for i in range(voices.Count):
                    if self._hint in voices.Item(i).GetDescription().lower():
                        v.Voice = voices.Item(i)
                        break
            self._local.voice = v
        return v

    def describe(self) -> str:
        try:
            return self._voice().Voice.GetDescription()
        except Exception:
            return "Windows voice"

    def __call__(self, text: str, voice: str = "", speed: float = 1.0) -> Iterator[_Result]:
        import win32com.client
        v = self._voice()
        v.Rate = max(-10, min(10, int(round((float(speed or 1.0) - 1.0) * 10))))
        for sentence in (s.strip() for s in _SENTENCE.split(text or "")):
            if not sentence:
                continue
            stream = win32com.client.Dispatch("SAPI.SpMemoryStream")
            fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
            fmt.Type = _SAFT24kHz16BitMono
            stream.Format = fmt
            v.AudioOutputStream = stream
            v.Speak(sentence, 0)                     # synchronous, into memory
            data = bytes(stream.GetData())
            audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            yield _Result(sentence, audio)


def available() -> bool:
    try:
        import win32com.client  # noqa: F401
        return True
    except Exception:
        return False


class SapiTTS(KokoroTTS):
    """KokoroTTS with Windows' built-in voice instead of the Kokoro model."""

    def __init__(self, voice: str = "", speed: float = 1.0, reason: str = "", **_ignored):
        from core.config import config
        super().__init__(voice=voice, speed=speed, device="cpu", allow_cpu_fallback=True, require_cuda=False)
        self._hint = str(config.get("voice.sapi_voice", "") or "")
        self._reason = reason or "Kokoro isn't available"

    def _load(self):
        if self._pipeline is not None:
            return
        with self._load_lock:
            if self._pipeline is not None:
                return
            try:
                pipe = _SapiPipeline(self._hint)
                name = pipe.describe()               # proves SAPI works on this PC
                self._pipeline = pipe
                self._resolved_device = f"Windows voice ({name})"
                log.info("tts.sapi.ready voice=%r (%s)", name, self._reason)
            except Exception as e:
                self._load_error = RuntimeError(f"Windows speech isn't available: {e}")
                raise self._load_error

    @property
    def device_info(self) -> dict:
        return {"requested_device": "kokoro", "resolved_device": self._resolved_device, "cuda_available": False,
                "fell_back": True, "reason": self._reason}

    def warm_up(self):
        try:
            self._load()
        except Exception:
            log.debug("tts.sapi.warm_up_failed", exc_info=True)

    def _voice_for(self, code: str):
        return self._pipeline, self._voice

    def _chain(self, text: str):
        yield from self._pipeline(text, voice=self._voice, speed=self._speed)

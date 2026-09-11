"""
modules/voice/module.py

VoiceModule — Milestone 1 implementation.

Responsibilities:
  - Capture audio from the selected microphone (sounddevice)
  - Run VAD to detect speech onset / offset
  - Collect speech frames into an utterance buffer
  - Route the utterance to the STT engine
  - Emit VOICE_STT_PARTIAL and VOICE_STT_FINAL events
  - Detect user interruption while SAINT is speaking
  - Support Always-On and Push-to-Talk modes
  - Expose audio level for the waveform meter UI

The module does NOT call the AI or TTS directly — it only fires events.
The ConversationController (core/conversation.py) wires everything together.
"""

import queue
import threading
import time
from typing import Optional

import numpy as np

from modules.base import BaseModule
from modules.voice.vad import make_vad, RmsVAD
from modules.voice.stt import make_stt, STTEngine, MockSTT
from core.events import event_bus, EventType
from core.config import config

# Audio settings
SAMPLE_RATE = 16000     # Hz — Whisper requires 16 kHz
CHANNELS = 1
CHUNK_MS = 30           # milliseconds per VAD frame
CHUNK_FRAMES = int(SAMPLE_RATE * CHUNK_MS / 1000)


class VoiceModule(BaseModule):
    name = "Voice"
    description = "Voice input and speech recognition (Milestone 1)."

    def __init__(self):
        super().__init__()
        self.subtasks = {
            "Wake Word Detection": False,
            "Speech-to-Text": True,
            "Text-to-Speech": True,
            "Voice Activity Detection": True,
            "Always Listening": True,
            "Push-to-Talk": True,
            "Silence Detection": True,
            "Mic Selection": True,
            "Noise Suppression": True,
            "Interruption Detection": True,
        }

        # State
        self._listening = False
        self._ptt_held = False         # push-to-talk button held
        self._speaking = False         # SAINT is currently speaking (external flag)

        # Audio machinery
        self._audio_queue: queue.Queue = queue.Queue()
        self._stream = None
        self._vad: Optional[RmsVAD] = None
        self._stt: Optional[STTEngine] = None

        # Worker threads
        self._capture_thread: Optional[threading.Thread] = None
        self._process_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Current audio level (0-1) — updated by capture callback
        self._audio_level: float = 0.0

    # ------------------------------------------------------------------ #
    # Enable / disable
    # ------------------------------------------------------------------ #
    def enable(self):
        super().enable()
        self._init_engines()

    def _init_engines(self):
        sensitivity = config.get("voice.mic_sensitivity", 0.015)
        noise_sup = config.get("voice.noise_suppression", True)
        self._vad = make_vad(threshold=sensitivity, noise_suppression=noise_sup)

        stt_backend = config.get("voice.stt_backend", "faster_whisper")
        if stt_backend == "faster_whisper":
            stt_device = config.get("voice.stt_device", "cuda")
            stt_compute = config.get("voice.stt_compute_type", "float16")
            stt_model = config.get("voice.stt_model", "tiny.en")
            stt_lang = config.get("voice.stt_language", "en")
            self._stt = make_stt(
                "faster_whisper",
                model_name=stt_model,
                device=stt_device,
                compute_type=stt_compute,
                language=stt_lang,
            )
        else:
            self._stt = make_stt("mock")

    # ------------------------------------------------------------------ #
    # Listening control
    # ------------------------------------------------------------------ #
    def start_listening(self):
        if self._listening:
            return
        self._listening = True
        self._stop_event.clear()
        self._process_thread = threading.Thread(
            target=self._process_loop, daemon=True, name="voice-process"
        )
        self._process_thread.start()
        self._start_stream()
        event_bus.emit_event(EventType.VOICE_LISTENING_START)

    def stop_listening(self):
        if not self._listening:
            return
        self._listening = False
        self._stop_event.set()
        self._stop_stream()
        if self._process_thread:
            self._process_thread.join(timeout=3.0)
        event_bus.emit_event(EventType.VOICE_LISTENING_STOP)

    # ------------------------------------------------------------------ #
    # Push-to-talk
    # ------------------------------------------------------------------ #
    def ptt_press(self):
        self._ptt_held = True

    def ptt_release(self):
        self._ptt_held = False

    # ------------------------------------------------------------------ #
    # External state (set by ConversationController)
    # ------------------------------------------------------------------ #
    def set_saint_speaking(self, speaking: bool):
        """Let the voice module know SAINT is speaking (for interrupt detection)."""
        self._speaking = speaking

    # ------------------------------------------------------------------ #
    # Audio level (for waveform meter)
    # ------------------------------------------------------------------ #
    @property
    def audio_level(self) -> float:
        return self._audio_level

    # ------------------------------------------------------------------ #
    # Sounddevice stream
    # ------------------------------------------------------------------ #
    def _start_stream(self):
        try:
            import sounddevice as sd  # type: ignore

            device = config.get("voice.mic_device", None)
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_FRAMES,
                device=device,
                callback=self._audio_callback,
            )
            self._stream.start()
        except Exception as e:
            event_bus.emit_event(EventType.ERROR, {"error": f"Mic open failed: {e}"})
            self._listening = False

    def _stop_stream(self):
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _audio_callback(self, indata, frames, time_info, status):
        """Called by sounddevice on every audio chunk (runs in audio thread)."""
        if status:
            pass  # underruns etc — don't crash
        chunk = indata[:, 0].copy()  # mono
        self._audio_queue.put(chunk)
        # Update level for UI meter
        if self._vad:
            self._audio_level = self._vad.get_level(chunk)
            event_bus.emit_event(EventType.VOICE_AUDIO_LEVEL, {"level": self._audio_level})

    # ------------------------------------------------------------------ #
    # Processing loop (runs in process thread)
    # ------------------------------------------------------------------ #
    def _process_loop(self):
        mode = config.get("voice.mode", "always_on")
        silence_ms = config.get("voice.silence_duration_ms", 400)
        silence_frames = int(silence_ms / CHUNK_MS)

        speech_frames = []
        silence_count = 0
        in_speech = False

        while not self._stop_event.is_set():
            try:
                chunk = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            chunk_np = chunk.astype(np.float32) / 32768.0

            # Mode gate
            if mode == "push_to_talk":
                is_active = self._ptt_held
            else:
                is_active = True

            if not is_active:
                continue

            # VAD
            is_voice = self._vad.feed(chunk_np) if self._vad else False

            # Interrupt detection: user speaks while SAINT is speaking
            if self._speaking and is_voice:
                event_bus.emit_event(EventType.VOICE_INTERRUPT, {})

            if is_voice:
                if not in_speech:
                    in_speech = True
                    silence_count = 0
                    event_bus.emit_event(EventType.VOICE_SPEECH_START)
                speech_frames.append(chunk)
                silence_count = 0
            else:
                if in_speech:
                    silence_count += 1
                    speech_frames.append(chunk)  # include trailing silence
                    if silence_count >= silence_frames:
                        # Utterance complete — transcribe
                        in_speech = False
                        self._transcribe(speech_frames)
                        speech_frames = []
                        silence_count = 0

    def _transcribe(self, frames):
        """Transcribe a collected utterance (runs in process thread)."""
        if not frames or self._stt is None:
            return

        audio = np.concatenate(frames)
        t0 = time.perf_counter()

        try:
            result = self._stt.transcribe(audio, sample_rate=SAMPLE_RATE)
        except Exception as e:
            event_bus.emit_event(EventType.VOICE_STT_ERROR, {"error": str(e)})
            return

        latency_ms = (time.perf_counter() - t0) * 1000
        event_bus.emit_event(EventType.VOICE_SPEECH_END)

        if not result.text.strip():
            return  # empty transcription — ignore

        event_bus.emit_event(EventType.VOICE_STT_FINAL, {
            "text": result.text,
            "confidence": result.confidence,
            "latency_ms": round(latency_ms, 1),
        })
        event_bus.emit_event(EventType.LATENCY_STT, {"ms": round(latency_ms, 1)})

    # ------------------------------------------------------------------ #
    # Mock injection (for tests)
    # ------------------------------------------------------------------ #
    def inject_utterance(self, text: str, confidence: float = 0.95):
        """
        Directly fire a VOICE_STT_FINAL event without real audio.
        Used by tests and the ConversationController's mock path.
        """
        event_bus.emit_event(EventType.VOICE_STT_FINAL, {
            "text": text,
            "confidence": confidence,
            "latency_ms": 0.0,
        })

    def inject_interrupt(self):
        """Fire a VOICE_INTERRUPT event (for tests)."""
        event_bus.emit_event(EventType.VOICE_INTERRUPT, {})

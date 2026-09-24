"""
modules/voice/module.py

VoiceModule — microphone capture, wake word, VAD, STT and barge-in.

Listening phases (independent of the UI):

    WAKE      the local wake-word model runs on every frame. Speech is
              buffered tentatively. A short utterance the model did not fire
              on is transcribed once and accepted only if it *starts* with
              "SAINT" / "Hey SAINT" (second-stage wake: the ONNX model is
              trained mostly on "Hey SAINT" and barely scores a bare "SAINT").
    COMMAND   "Hey SAINT" was heard (or SAINT was interrupted / the
              conversation window is open): the next utterance is captured and
              sent to STT. The conversation window
              (voice.wake_word_followup_sec) starts when SAINT stops talking,
              so follow-ups like "skip that" need no wake word.
              The utterance that contained the wake phrase is included, so
              "Hey SAINT, play Daft Punk" and "Hey SAINT ... play Daft Punk"
              both work. Times out back to WAKE if nothing is said.
    OPEN      wake word disabled / unavailable: every utterance goes to STT
              and must pass the transcript activation gate.

Barge-in: while SAINT is speaking the mic keeps running. Microphone energy is
compared with the level of the audio SAINT is playing (core.audio_echo), so
SAINT's own voice through the speakers does not trigger an interruption but
the user talking over it does. On barge-in the controller stops TTS and the
user's utterance (including the frames that triggered the barge-in) becomes
the next command.

The module never calls the AI or TTS directly — it only emits events. The
ConversationController (core/conversation.py) wires everything together.
"""

import collections
import logging
import queue
import re
import string
import threading
import time
from enum import Enum
from typing import Optional

import numpy as np

from modules.base import BaseModule
from modules.voice.vad import make_vad, RmsVAD
from modules.voice.stt import make_stt, STTEngine
from modules.voice.wake_word import make_wake_word_detector, OnnxWakeWordDetector
from core.assistant_state import assistant_state, AssistantState
from core.audio_echo import playback_monitor, EchoGate
from core.events import event_bus, EventType
from core.config import config

log = logging.getLogger("saint.voice")

# Audio settings
SAMPLE_RATE = 16000     # Hz — Whisper and the wake model require 16 kHz
CHANNELS = 1
CHUNK_MS = 30           # milliseconds per VAD frame
CHUNK_FRAMES = int(SAMPLE_RATE * CHUNK_MS / 1000)
PREROLL_MS = 1500       # audio kept before speech onset / barge-in
MAX_TENTATIVE_MS = 12000
DEFAULT_CONVERSATION_SEC = 15.0

# STT session states (kept for diagnostics)
STT_STATE_TRANSCRIBING = "TRANSCRIBING"
STT_STATE_SUBMITTED = "SUBMITTED"
STT_STATE_COMPLETED = "COMPLETED"


class ListenPhase(str, Enum):
    WAKE = "wake"
    COMMAND = "command"
    OPEN = "open"


def _rms(x: np.ndarray) -> float:
    if len(x) == 0:
        return 0.0
    if x.dtype == np.int16:
        x = x.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(np.square(x))))


class VoiceModule(BaseModule):
    name = "Voice"
    description = "Wake word, voice input, speech recognition and interruption handling."

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

        self._voice_active = False
        self._listening = False
        self._ptt_held = False
        self._speaking = False

        self._audio_queue: queue.Queue = queue.Queue()
        self._stream = None
        self._actual_sr = SAMPLE_RATE
        self._vad: Optional[RmsVAD] = None
        self._stt: Optional[STTEngine] = None
        self._wake: Optional[OnnxWakeWordDetector] = None
        self._wake_error: str = ""

        self._phase = ListenPhase.OPEN
        self._phase_lock = threading.RLock()
        self._command_deadline = 0.0
        self._command_reason = ""
        self._command_window = 0.0
        self._prev_passive = None          # (audio_clock_end, frames) of the last passive utterance
        self._audio_clock = 0.0            # seconds of microphone audio processed
        self._awaiting_stt = False
        self._last_wake_time = 0.0

        self._process_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._stt_cancelled = threading.Event()
        self._stt_worker: Optional[threading.Thread] = None
        self._last_audio_time = 0.0
        self._mic_error: str = ""

        self._speech_session_id = 0
        self._speech_id_lock = threading.Lock()

        self._tts_playback_active = False
        self._tts_playback_lock = threading.Lock()

        self._audio_level = 0.0
        self._last_level_event_time = 0.0
        self._last_score_event_time = 0.0

        self._stt_device_info = ""
        self._stt_session_state: dict = {}
        self._stt_session_state_lock = threading.Lock()

        self._echo_gate = EchoGate(
            margin=config.get("voice.barge_in_echo_margin", 2.0),
            min_ms=config.get("voice.barge_in_min_ms", 240),
            frame_ms=CHUNK_MS,
            initial_coupling=0.3,
        )
        self._barge_in_count = 0

        event_bus.subscribe(self._on_bus_event)

    # ------------------------------------------------------------------ #
    # Properties / diagnostics
    # ------------------------------------------------------------------ #
    @property
    def voice_active(self) -> bool:
        return self._voice_active

    @property
    def audio_level(self) -> float:
        return self._audio_level

    @property
    def phase(self) -> ListenPhase:
        with self._phase_lock:
            return self._phase

    @property
    def wake_mode(self) -> bool:
        return bool(self._wake is not None and self._wake.ready)

    def wake_status(self) -> dict:
        enabled = bool(config.get("voice.wake_word_enabled", True))
        if not enabled:
            return {"enabled": False, "ready": False, "error": "", "code": "DISABLED"}
        if self._wake is None:
            return {"enabled": True, "ready": False,
                    "error": self._wake_error or "Wake word not initialised yet.",
                    "code": "NOT_LOADED"}
        st = self._wake.status
        return {"enabled": True, "ready": st.ready, "error": st.error, "code": st.code,
                "model_path": st.model_path, "threshold": st.threshold,
                "avg_infer_ms": round(self._wake.avg_infer_ms, 2)}

    @property
    def diagnostics(self) -> dict:
        return {
            "voice_active": self._voice_active,
            "listening": self._listening,
            "ptt_active": self._ptt_held,
            "phase": self.phase.value,
            "stream_active": bool(self._stream is not None and getattr(self._stream, "active", False)),
            "stt_loaded": self._stt is not None,
            "stt_device": self._stt_device_info,
            "speech_session_id": self._speech_session_id,
            "wake": self.wake_status(),
            "mic_error": self._mic_error,
            "echo_coupling": round(self._echo_gate.coupling, 3),
            "barge_ins": self._barge_in_count,
        }

    # ------------------------------------------------------------------ #
    # Enable / engines
    # ------------------------------------------------------------------ #
    def enable(self):
        super().enable()
        if self._vad is None or self._stt is None:
            self._init_engines()

    def _init_engines(self):
        sensitivity = config.get("voice.mic_sensitivity", 0.015)
        noise_sup = config.get("voice.noise_suppression", True)
        self._vad = make_vad(threshold=sensitivity, noise_suppression=noise_sup)

        stt_backend = config.get("voice.stt_backend", "faster_whisper")
        if stt_backend == "faster_whisper":
            stt_device = config.get("voice.stt_device", "cuda")
            stt_compute = config.get("voice.stt_compute_type", "float16")
            stt_model = config.get("voice.stt_model", "base.en")
            self._stt = make_stt(
                "faster_whisper",
                model_name=stt_model,
                device=stt_device,
                compute_type=stt_compute,
                language=config.get("voice.stt_language", "en"),
                hotwords=config.get("voice.stt_hotwords", "SAINT"),
            )
            self._stt_device_info = f"{stt_device}/{stt_compute}/{stt_model}"
            threading.Thread(target=self._warmup_stt, daemon=True, name="voice-warmup").start()
        else:
            self._stt = make_stt("mock")
            self._stt_device_info = "mock"

        self.reload_wake_word()

    def reload_wake_word(self):
        """(Re)create the wake-word detector from current settings."""
        try:
            self._wake = make_wake_word_detector()
            self._wake_error = ""
        except Exception as e:  # defensive: never break voice because of the wake word
            self._wake = None
            self._wake_error = f"Wake word failed to initialise: {e}"
            log.exception("wake.init_failed")
        status = self.wake_status()
        self.subtasks["Wake Word Detection"] = bool(status.get("ready"))
        event_bus.emit_event(EventType.WAKE_STATUS, status)
        if status.get("enabled") and not status.get("ready"):
            event_bus.emit_event(EventType.WAKE_ERROR, {"error": status.get("error"),
                                                        "code": status.get("code")})
        with self._phase_lock:
            self._phase = ListenPhase.WAKE if self.wake_mode else ListenPhase.OPEN
        if self._listening:
            self._apply_resting_state()

    def _warmup_stt(self):
        try:
            if self._stt:
                self._stt.warm_up()
        except Exception as e:
            log.warning("stt.warmup_failed %s", e)
            event_bus.emit_event(EventType.VOICE_STT_ERROR, {"error": f"STT warm-up failed: {e}"})

    def _apply_resting_state(self):
        if not self._listening:
            assistant_state.set_resting(AssistantState.OFFLINE,
                                        detail=self._mic_error)
        elif self.wake_mode:
            assistant_state.set_resting(AssistantState.WAKE_LISTENING)
        else:
            detail = ""
            if config.get("voice.wake_word_enabled", True):
                detail = "Wake word unavailable — responding to all speech"
            assistant_state.set_resting(AssistantState.LISTENING, detail=detail)

    # ------------------------------------------------------------------ #
    # Listening lifecycle
    # ------------------------------------------------------------------ #
    def start_listening(self) -> bool:
        if self._listening:
            return True
        if self._vad is None:
            self._init_engines()
        self._stop_event.clear()
        self._stt_cancelled.clear()
        self._mic_error = ""
        if not self._start_stream():
            return False
        self._voice_active = True
        self._listening = True
        with self._phase_lock:
            self._phase = ListenPhase.WAKE if self.wake_mode else ListenPhase.OPEN
        self._process_thread = threading.Thread(target=self._process_loop, daemon=True,
                                                name="voice-process")
        self._process_thread.start()
        event_bus.emit_event(EventType.VOICE_LISTENING_START, {"phase": self.phase.value})
        log.info("voice.listening.start phase=%s", self.phase.value)
        self._apply_resting_state()
        return True

    def stop_listening(self):
        if not self._listening:
            return
        self._voice_active = False
        self._listening = False
        self._stop_event.set()
        self._stop_stream()
        self._stt_cancelled.set()
        if self._process_thread:
            self._process_thread.join(timeout=3.0)
            self._process_thread = None
        event_bus.emit_event(EventType.VOICE_LISTENING_STOP)
        log.info("voice.listening.stop")
        self._apply_resting_state()

    def stop(self):
        self.stop_listening()

    def ptt_toggle(self, held: bool):
        self._ptt_held = held

    # ------------------------------------------------------------------ #
    # External state (set by the conversation controller)
    # ------------------------------------------------------------------ #
    def set_saint_speaking(self, speaking: bool):
        self._speaking = speaking

    def set_tts_playback_active(self, active: bool):
        with self._tts_playback_lock:
            if self._tts_playback_active != active:
                self._tts_playback_active = active
                log.debug("voice.tts.playback %s", "start" if active else "stop")
        if not active:
            self._echo_gate.reset_run()

    def is_tts_playback_active(self) -> bool:
        with self._tts_playback_lock:
            return self._tts_playback_active

    def _saint_is_talking(self) -> bool:
        return self._speaking or self.is_tts_playback_active()

    _music_playing = False              # updated from spotify.playback.changed events

    def _on_bus_event(self, ev):
        if ev.type == EventType.SPOTIFY_PLAYBACK_CHANGED:
            self._music_playing = bool((ev.payload or {}).get("is_playing"))
        if ev.type == EventType.CONVERSATION_TURN_END:
            if not (self._listening and self.wake_mode):
                return
            if ev.payload.get("expects_reply"):
                # SAINT asked a question (e.g. a confirmation) — accept the
                # answer without requiring the wake word again.
                self._enter_command(8.0, reason="awaiting reply", chime=True)
                return
            base = float(config.get("voice.wake_word_followup_sec", DEFAULT_CONVERSATION_SEC) or 0.0)
            if base <= 0:
                self._return_to_rest_phase("turn end")
                return
            # Adaptive: an action reply ("Playing music.", "Opened Chrome.") means
            # a follow-up ("skip", "louder", "close it") is likely, so we extend
            # the window. A bare Q&A collapses to the base timeout. Capped so
            # SAINT still returns to wake-word listening if the user walks away.
            extra = float(config.get("voice.wake_word_followup_action_bonus_sec", 15.0) or 0.0)
            follow = base + extra if ev.payload.get("was_action") else base
            follow = min(follow, float(config.get("voice.wake_word_followup_max_sec", 45.0) or follow))
            reason = "follow-up (action)" if ev.payload.get("was_action") else "follow-up"
            self._enter_command(follow, reason=reason, chime=False)

    # ------------------------------------------------------------------ #
    # Sounddevice stream
    # ------------------------------------------------------------------ #
    def _start_stream(self) -> bool:
        try:
            import sounddevice as sd
            device = config.get("voice.mic_device", None)
            try:
                device_info = sd.query_devices(device, "input")
            except Exception:
                if device is None:
                    raise
                log.warning("voice.mic.configured_device_missing device=%s — using default", device)
                device = None
                device_info = sd.query_devices(None, "input")
            self._actual_sr = int(device_info["default_samplerate"])
            blocksize = int(self._actual_sr * CHUNK_MS / 1000)
            self._stream = sd.InputStream(
                samplerate=self._actual_sr, channels=CHANNELS, dtype="int16",
                blocksize=blocksize, device=device, callback=self._audio_callback,
            )
            self._stream.start()
            self._last_audio_time = time.monotonic()
            log.info("voice.mic.open device=%s name=%r sr=%d", device,
                     device_info.get("name"), self._actual_sr)
            return True
        except Exception as e:
            self._mic_error = f"Microphone unavailable: {e}"
            log.error("voice.mic.open_failed %s", e)
            event_bus.emit_event(EventType.ERROR, {"error": self._mic_error, "source": "voice"})
            assistant_state.set_resting(AssistantState.OFFLINE, detail=self._mic_error)
            assistant_state.set(AssistantState.ERROR, self._mic_error)
            self._stream = None
            return False

    def _stop_stream(self):
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _audio_callback(self, indata, frames, time_info, status):
        if not self._voice_active:
            return
        self._audio_queue.put(indata[:, 0].copy())

    # ------------------------------------------------------------------ #
    # Phase helpers
    # ------------------------------------------------------------------ #
    def _enter_command(self, timeout_sec: float, reason: str, chime: bool = False):
        with self._phase_lock:
            self._phase = ListenPhase.COMMAND
            self._command_deadline = time.monotonic() + timeout_sec
            self._command_window = timeout_sec
            self._command_reason = reason
            self._awaiting_stt = False
        log.info("voice.phase.command reason=%s window=%.1fs", reason, timeout_sec)
        detail = "Conversation active — no wake word needed" if reason == "follow-up" else reason
        assistant_state.set(AssistantState.COMMAND_LISTENING, detail)
        if chime and config.get("voice.wake_word_chime", True):
            _play_chime()

    def _return_to_rest_phase(self, reason: str = ""):
        with self._phase_lock:
            self._phase = ListenPhase.WAKE if self.wake_mode else ListenPhase.OPEN
            self._awaiting_stt = False
        if reason:
            log.info("voice.phase.rest reason=%s", reason)
        if assistant_state.state in (AssistantState.COMMAND_LISTENING,
                                     AssistantState.WAKE_DETECTED,
                                     AssistantState.PROCESSING,
                                     AssistantState.LISTENING,
                                     AssistantState.ERROR):
            assistant_state.return_to_rest()

    def _on_wake_word(self, score: float, source: str = "model"):
        self._last_wake_time = time.monotonic()
        log.info("voice.wake_word.detected score=%.3f source=%s", score, source)
        event_bus.emit_event(EventType.VOICE_WAKE_WORD, {
            "keyword": config.get("voice.wake_word", "saint"),
            "score": round(float(score), 3),
            "source": source,
        })
        assistant_state.set(AssistantState.WAKE_DETECTED, f"score {score:.2f}")
        self._enter_command(float(config.get("voice.wake_word_command_timeout_sec", 6.0)),
                            reason="wake word", chime=True)

    # ------------------------------------------------------------------ #
    # Processing loop
    # ------------------------------------------------------------------ #
    def _process_loop(self):
        mode = config.get("voice.mode", "always_on")
        silence_frames = max(1, int(config.get("voice.silence_duration_ms", 700) / CHUNK_MS))
        min_speech_frames = max(1, int(config.get("voice.min_speech_duration_ms", 300) / CHUNK_MS))
        min_speech_rms = config.get("voice.min_speech_rms", 0.005)
        barge_enabled = config.get("voice.barge_in_enabled", True)
        debug_scores = config.get("voice.wake_word_debug_scores", False)
        preroll = collections.deque(maxlen=int(PREROLL_MS / CHUNK_MS))
        max_tentative = int(MAX_TENTATIVE_MS / CHUNK_MS)
        # Hard cap for an addressed utterance (e.g. TV or music vocals that
        # never pause): cut it and transcribe what we have.
        max_utterance = int(float(config.get("voice.max_utterance_sec", 15.0)) * 1000 / CHUNK_MS)

        seg: list = []
        in_speech = False
        silence_count = 0
        speech_count = 0
        sid = 0
        barge_capture = False
        last_restart_try = 0.0

        while not self._stop_event.is_set():
            try:
                chunk = self._audio_queue.get(timeout=0.25)
            except queue.Empty:
                # Mic watchdog: a vanished device stops delivering audio.
                now = time.monotonic()
                if self._listening and now - self._last_audio_time > 3.0 and now - last_restart_try > 5.0:
                    last_restart_try = now
                    log.warning("voice.mic.stalled — reopening input stream")
                    self._stop_stream()
                    if self._start_stream():
                        self._mic_error = ""
                        self._apply_resting_state()
                self._check_command_timeout(in_speech)
                continue
            if self._stop_event.is_set() or not self._voice_active:
                break
            self._last_audio_time = time.monotonic()

            chunk16 = self._to_16k(chunk)
            self._audio_clock += len(chunk16) / SAMPLE_RATE
            chunk_f = chunk16.astype(np.float32) / 32768.0
            mic_rms = _rms(chunk_f)
            preroll.append(chunk16)
            self._update_level(chunk_f)

            if mode == "push_to_talk" and not self._ptt_held and not in_speech:
                continue

            playing = playback_monitor.active()
            ref = playback_monitor.level() if playing else 0.0
            raw_voice = self._vad.feed(chunk_f) if self._vad else False
            if playing:
                predicted = (self._echo_gate.coupling * ref * self._echo_gate.margin
                             + self._echo_gate.floor)
                is_voice = raw_voice and mic_rms > predicted
            else:
                is_voice = raw_voice

            # ---- SAINT is speaking: only barge-in detection runs ------------
            if self._saint_is_talking() and not barge_capture:
                if barge_enabled and raw_voice and self._echo_gate.update(mic_rms, ref):
                    self._barge_in_count += 1
                    log.info("voice.barge_in mic_rms=%.4f playback_rms=%.4f coupling=%.3f",
                             mic_rms, ref, self._echo_gate.coupling)
                    event_bus.emit_event(EventType.VOICE_BARGE_IN, {
                        "mic_rms": round(mic_rms, 4), "playback_rms": round(ref, 4)})
                    event_bus.emit_event(EventType.VOICE_INTERRUPT, {"source": "barge_in"})
                    # Capture the user's utterance starting a little before
                    # the gate fired so the first word is not clipped.
                    barge_capture = True
                    in_speech = True
                    silence_count = 0
                    speech_count = self._echo_gate.min_frames
                    with self._speech_id_lock:
                        self._speech_session_id += 1
                        sid = self._speech_session_id
                    seg = list(preroll)[-(self._echo_gate.min_frames + 10):]
                    self._enter_command(8.0, reason="interruption")
                elif not raw_voice:
                    self._echo_gate.reset_run()
                continue

            # ---- wake word --------------------------------------------------
            if self._wake is not None and self._wake.ready:
                score = self._wake.feed(chunk16)
                self._emit_wake_score(debug_scores)
                if score is not None:
                    if playing:
                        log.debug("voice.wake_word.ignored_during_playback score=%.3f", score)
                    elif self.phase == ListenPhase.WAKE:
                        self._on_wake_word(score)

            # ---- segmentation -----------------------------------------------
            if is_voice:
                if not in_speech:
                    in_speech = True
                    silence_count = 0
                    speech_count = 0
                    with self._speech_id_lock:
                        self._speech_session_id += 1
                        sid = self._speech_session_id
                    # 600 ms pre-roll: VAD onset can lag a soft first word
                    # ("Saint" under music) and clipping it loses the wake word.
                    seg = list(preroll)[-20:]
                    if self.phase != ListenPhase.WAKE:
                        event_bus.emit_event(EventType.VOICE_SPEECH_START, {"session_id": sid})
                else:
                    seg.append(chunk16)
                speech_count += 1
                silence_count = 0
                if len(seg) > max_tentative and self.phase == ListenPhase.WAKE:
                    seg = seg[-max_tentative:]
                elif len(seg) > max_utterance and self.phase != ListenPhase.WAKE:
                    log.info("voice.utterance.cap frames=%d", len(seg))
                    in_speech = False
                    was_barge = barge_capture
                    barge_capture = False
                    self._end_segment(seg, speech_count, sid, min_speech_frames,
                                      min_speech_rms, was_barge)
                    seg, speech_count, silence_count = [], 0, 0
            elif in_speech:
                silence_count += 1
                seg.append(chunk16)
                if silence_count >= silence_frames:
                    in_speech = False
                    was_barge = barge_capture
                    barge_capture = False
                    self._end_segment(seg, speech_count, sid, min_speech_frames,
                                      min_speech_rms, was_barge)
                    seg = []
                    speech_count = 0
                    silence_count = 0

            self._check_command_timeout(in_speech)

        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break

    def _to_16k(self, chunk: np.ndarray) -> np.ndarray:
        sr = self._actual_sr
        if sr == SAMPLE_RATE:
            return chunk.astype(np.int16, copy=False)
        f = chunk.astype(np.float32)
        target_len = max(1, int(round(len(f) * SAMPLE_RATE / sr)))
        x_old = np.linspace(0, 1, len(f))
        x_new = np.linspace(0, 1, target_len)
        return np.interp(x_new, x_old, f).astype(np.int16)

    def _update_level(self, chunk_f: np.ndarray):
        level = self._vad.get_level(chunk_f) if self._vad else 0.0
        self._audio_level = level
        now = time.monotonic()
        if now - self._last_level_event_time > 0.1:
            self._last_level_event_time = now
            event_bus.emit_event(EventType.VOICE_AUDIO_LEVEL, {"level": level})

    def _emit_wake_score(self, debug_scores: bool):
        now = time.monotonic()
        if now - self._last_score_event_time < 0.1:
            return
        self._last_score_event_time = now
        peak = self._wake.take_peak()
        event_bus.emit_event(EventType.VOICE_WAKE_SCORE, {
            "score": round(peak, 3), "threshold": self._wake.threshold})
        if debug_scores and peak >= 0.1:
            log.debug("wake.score peak=%.3f threshold=%.2f", peak, self._wake.threshold)

    def _check_command_timeout(self, in_speech: bool):
        if self._saint_is_talking():
            # The conversation window counts from the end of SAINT's reply.
            with self._phase_lock:
                if self._phase == ListenPhase.COMMAND:
                    self._command_deadline = time.monotonic() + self._command_window
            return
        with self._phase_lock:
            if (self._phase != ListenPhase.COMMAND or in_speech or self._awaiting_stt
                    or time.monotonic() < self._command_deadline):
                return
            reason = self._command_reason
        log.info("voice.command.timeout reason=%s", reason)
        event_bus.emit_event(EventType.VOICE_COMMAND_TIMEOUT, {"reason": reason})
        self._return_to_rest_phase("command timeout")

    # ------------------------------------------------------------------ #
    # Segment end
    # ------------------------------------------------------------------ #
    def _end_segment(self, frames, speech_count, sid, min_speech_frames, min_speech_rms,
                     was_barge: bool):
        phase = self.phase
        if phase == ListenPhase.WAKE:
            # Speech the wake model did not fire on. Short utterances get one
            # transcript check ("SAINT, skip this" — the model barely scores a
            # bare "SAINT"); they are acted on only if the transcript *starts*
            # with the wake word. Everything else is dropped untranscribed.
            dur = len(frames) * CHUNK_MS / 1000.0
            if (config.get("voice.wake_word_transcript_check", True) and self._stt is not None
                    and 0.3 <= dur <= float(config.get("voice.wake_word_transcript_max_sec", 7.0))
                    and self._validate_speech_session(frames, speech_count, min_speech_frames, min_speech_rms)):
                peak = getattr(self._wake, "recent_peak", lambda _s: 0.0)(dur + 1.0)
                now = self._audio_clock
                check = frames
                prev = self._prev_passive
                # "SAINT, <pause> skip this song" is often split at the pause:
                # judge the two pieces together so the wake word and the
                # command stay one utterance.
                if prev and now - dur - prev[0] <= 1.5:
                    gap = [np.zeros(CHUNK_FRAMES, dtype=np.int16)] * 10
                    check = prev[1] + gap + frames
                self._prev_passive = (now, frames)
                log.debug("voice.wake.transcript_check dur=%.1fs joined=%s model_peak=%.3f",
                          dur, check is not frames, peak)
                self._transcribe(check, sid, wake_initiated=False, wake_check=True)
            return
        if not self._validate_speech_session(frames, speech_count, min_speech_frames, min_speech_rms):
            self._emit_vad_reject(sid, speech_count, len(frames), frames)
            return
        wake_initiated = phase == ListenPhase.COMMAND
        with self._phase_lock:
            follow_up = wake_initiated and self._command_reason == "follow-up"
        if wake_initiated:
            with self._phase_lock:
                self._awaiting_stt = True
            assistant_state.set(AssistantState.PROCESSING, "transcribing")
        self._transcribe(frames, sid, wake_initiated=wake_initiated, allow_during_tts=was_barge,
                         follow_up=follow_up)

    def _validate_speech_session(self, frames: list, speech_frame_count: int,
                                 min_speech_frames: int, min_speech_rms: float) -> bool:
        if not frames:
            return False
        if speech_frame_count < min_speech_frames:
            return False
        if speech_frame_count / len(frames) < 0.1:
            return False
        audio = np.concatenate(frames)
        return _rms(audio) >= min_speech_rms

    def _emit_vad_reject(self, session_id: int, speech_frames: int, total_frames: int, frames: list = None):
        rms = _rms(np.concatenate(frames)) if frames else 0.0
        ratio = speech_frames / total_frames if total_frames else 0.0
        event_bus.emit_event(EventType.VOICE_STT_SKIP, {
            "session_id": session_id, "reason": "insufficient_speech",
            "speech_frames": speech_frames, "total_frames": total_frames,
            "speech_ratio": round(ratio, 3), "rms": round(rms, 6),
        })

    @staticmethod
    def _trim_silence(frames: list, pad: int = 3) -> list:
        """Trim leading/trailing near-silent frames (thread-safe; no shared VAD)."""
        if len(frames) <= pad * 2 + 1:
            return frames
        levels = np.array([_rms(f) for f in frames])
        thresh = max(0.004, 0.1 * float(levels.max()))
        voiced = np.nonzero(levels >= thresh)[0]
        if len(voiced) == 0:
            return frames
        start = max(0, int(voiced[0]) - pad)
        end = min(len(frames) - 1, int(voiced[-1]) + pad)
        return frames[start:end + 1]

    # ------------------------------------------------------------------ #
    # STT
    # ------------------------------------------------------------------ #
    def _transcribe(self, frames, session_id: int = 0, wake_initiated: bool = False,
                    allow_during_tts: bool = False, wake_check: bool = False, follow_up: bool = False):
        if not frames or self._stt is None:
            self._emit_stt_error(session_id, "empty_audio_buffer")
            self._stt_done(session_id, wake_initiated, "")
            return
        t0_total = time.perf_counter()

        def _run():
            text = ""
            try:
                text = self._stt_worker_func(frames, session_id, t0_total,
                                             wake_initiated, allow_during_tts,
                                             wake_check=wake_check, follow_up=follow_up) or ""
            except Exception as e:
                log.exception("stt.worker_failed")
                self._emit_stt_error(session_id, f"STT failed: {e}")
            finally:
                self._stt_done(session_id, wake_initiated or (wake_check and self.phase == ListenPhase.COMMAND),
                               text)

        self._stt_worker = threading.Thread(target=_run, daemon=True, name=f"stt-{session_id}")
        self._stt_worker.start()

    def _stt_done(self, session_id: int, wake_initiated: bool, text: str):
        """Decide the next listening phase after a transcription finishes."""
        if not wake_initiated:
            if not text and assistant_state.state == AssistantState.PROCESSING:
                assistant_state.return_to_rest()
            return
        with self._phase_lock:
            self._awaiting_stt = False
            if self._phase != ListenPhase.COMMAND:
                return
        if text:
            # A command was produced; the controller takes over.
            with self._phase_lock:
                self._phase = ListenPhase.WAKE if self.wake_mode else ListenPhase.OPEN
        else:
            # Only the wake phrase (or unintelligible speech) — keep waiting
            # for the actual command.
            timeout = float(config.get("voice.wake_word_command_timeout_sec", 6.0))
            with self._phase_lock:
                self._command_deadline = time.monotonic() + timeout
            assistant_state.set(AssistantState.COMMAND_LISTENING, self._command_reason)

    def _stt_worker_func(self, frames: list, session_id: int, t0_total: float,
                         wake_initiated: bool = False, allow_during_tts: bool = False,
                         wake_check: bool = False, follow_up: bool = False) -> str:
        """Background STT worker. Returns the accepted transcript ('' if rejected)."""
        if self.is_tts_playback_active() and not allow_during_tts and not wake_initiated:
            self._emit_stt_error(session_id, "tts_playback_active_during_stt")
            event_bus.emit_event(EventType.VOICE_STT_SKIP, {"session_id": session_id,
                                                            "reason": "tts_playback_active"})
            return ""
        if self._stt_cancelled.is_set() or not self._voice_active:
            self._emit_stt_error(session_id, "voice_inactive")
            return ""

        trimmed = self._trim_silence(frames)
        audio = np.concatenate(trimmed)
        if len(audio) == 0:
            self._emit_stt_error(session_id, "empty_audio_buffer")
            return ""
        audio_duration_ms = round(len(audio) / SAMPLE_RATE * 1000, 1)
        rms = round(_rms(audio), 6)

        with self._stt_session_state_lock:
            self._stt_session_state[session_id] = STT_STATE_TRANSCRIBING
        t1 = time.perf_counter()
        try:
            result = self._stt.transcribe(audio, sample_rate=SAMPLE_RATE)
        except Exception as e:
            log.error("stt.transcribe_failed session=%s %s", session_id, e)
            self._emit_stt_error(session_id, f"Speech recognition failed: {e}")
            return ""
        t2 = time.perf_counter()
        inference_ms = (t2 - t1) * 1000
        total_ms = (t2 - t0_total) * 1000
        with self._stt_session_state_lock:
            self._stt_session_state[session_id] = STT_STATE_SUBMITTED

        if result is None:
            self._emit_stt_error(session_id, "stt_returned_none")
            return ""
        raw_text = (getattr(result, "text", "") or "").strip()
        confidence = float(getattr(result, "confidence", 0.0) or 0.0)
        if wake_check:
            # Second-stage wake: only a transcript that starts with the wake
            # word counts. Nothing else from passive listening is kept or shown.
            if not self._starts_with_wake(raw_text) and not self._head_says_wake(audio):
                # Never log what bystanders said — only that it wasn't addressed to SAINT.
                log.debug("voice.wake.transcript_rejected session=%s words=%d", session_id, len(raw_text.split()))
                return ""
            text = self._strip_wake_prefix(raw_text)
            bare = not text.strip() or self._is_punctuation_only(text.strip())
            if self.phase == ListenPhase.WAKE or not bare:
                self._on_wake_word(0.0, source="transcript")
            wake_initiated = True
            if bare:
                return ""          # bare "SAINT" — the command window is now open
            with self._phase_lock:
                self._awaiting_stt = True
            wake_check = False
        else:
            text = self._strip_wake_prefix(raw_text) if (wake_initiated or self.wake_mode) else raw_text
        log.info("stt.result session=%s conf=%.2f wake=%s ms=%.0f text=%r",
                 session_id, confidence, wake_initiated, inference_ms, raw_text)
        event_bus.emit_event(EventType.VOICE_SPEECH_END, {"session_id": session_id})

        if not text.strip() or self._is_punctuation_only(text.strip()):
            event_bus.emit_event(EventType.VOICE_STT_SKIP, {
                "session_id": session_id, "reason": "empty_after_wake_strip" if raw_text else "empty",
                "text": raw_text})
            return ""

        ok, reason = self._passes_activation_gate(text, confidence,
                                                  wake_initiated=wake_initiated and not follow_up,
                                                  follow_up=follow_up)
        if not ok:
            log.info("voice.activation.rejected session=%s reason=%s conf=%.2f text=%r",
                     session_id, reason, confidence, text)
            event_bus.emit_event(EventType.VOICE_STT_SKIP, {
                "session_id": session_id, "reason": f"activation_gate:{reason}",
                "text": text, "confidence": round(confidence, 3)})
            return ""

        event_bus.emit_event(EventType.VOICE_STT_FINAL, {
            "text": text,
            "confidence": confidence,
            "latency_ms": round(total_ms, 1),
            "inference_ms": round(inference_ms, 1),
            "session_id": session_id,
            "wake": wake_initiated,
            "diagnostics": {
                "audio_duration_ms": audio_duration_ms,
                "audio_rms": rms,
                "model": getattr(self._stt, "_model_name", "unknown"),
                "device": getattr(self._stt, "_device", "unknown"),
            },
        })
        event_bus.emit_event(EventType.LATENCY_STT, {"ms": round(total_ms, 1)})
        with self._stt_session_state_lock:
            self._stt_session_state[session_id] = STT_STATE_COMPLETED
        return text

    # ------------------------------------------------------------------ #
    # Transcript helpers
    # ------------------------------------------------------------------ #
    _WAKE_PREFIX = re.compile(
        r"^\s*(?:(?:hey|hi|ok(?:ay)?)[\s,.!]+)?(?:saint(?:s|e|'s)?|sant|sane)\b[\s,.:;!?-]*",
        re.IGNORECASE)

    # Whisper sometimes renders "Hey SAINT" as "Hey, St." — only strip that
    # form after a greeting, so "St. Louis weather" is left alone.
    _WAKE_PREFIX_ALT = re.compile(
        r"^\s*(?:hey|hi|ok(?:ay)?)[\s,.!]+(?:st\.?|saint(?:s|e|'s)?|sant|sane)(?=[\s,.!?]|$)[\s,.:;!?-]*",
        re.IGNORECASE)

    def _strip_wake_prefix(self, text: str) -> str:
        """Remove a leading 'saint' / 'hey saint' from a transcript.

        The wake phrase is part of the captured utterance ("Hey SAINT, skip
        this song"), so strip it before the command reaches the agent. A bare
        wake word strips to "".
        """
        if not text:
            return text
        stripped = self._WAKE_PREFIX.sub("", text, count=1)
        if stripped == text:
            stripped = self._WAKE_PREFIX_ALT.sub("", text, count=1)
        if stripped == text:
            return text
        return stripped.strip()

    _WAKE_START = re.compile(
        r"^\s*(?:(?:um+|uh+|so|okay|ok|oh)[\s,.!]+)?(?:(?:hey|hi|ok(?:ay)?|yo)[\s,.!]+)?"
        r"(?:saint(?:s|e|'s)?|sant|sane)(?=[\s,.!?]|$)", re.IGNORECASE)
    _WAKE_START_ALT = re.compile(r"^\s*(?:hey|hi|ok(?:ay)?)[\s,.!]+st\.?(?=[\s,.!?]|$)", re.IGNORECASE)

    def _head_says_wake(self, audio: np.ndarray) -> bool:
        """Whisper sometimes drops a short leading "SAINT" from a longer
        transcript. Re-check just the first ~1.4 s (pre-roll + first word)."""
        head = audio[:int(SAMPLE_RATE * 1.4)]
        if len(audio) < SAMPLE_RATE * 1.6 or self._stt is None:
            return False
        try:
            r = self._stt.transcribe(head, sample_rate=SAMPLE_RATE)
        except Exception:
            return False
        text = (getattr(r, "text", "") or "").strip()
        ok = bool(re.match(r"^\W*(?:(?:hey|hi|ok(?:ay)?)\W+)?saint(?:s)?\b", text, re.I))
        log.debug("voice.wake.head_check ok=%s", ok)
        return ok

    def _starts_with_wake(self, text: str) -> bool:
        return bool(self._WAKE_START.match(text or "") or self._WAKE_START_ALT.match(text or ""))

    # Short spoken commands SAINT accepts as a follow-up even while music is
    # playing (music transcribed as random lyrics gets rejected instead of sent
    # to the agent). Anything longer needs the wake word again.
    _MUSIC_FOLLOWUP_OK = re.compile(
        r"^(?:skip(?:\s+it|\s+this(?:\s+song)?)?|next(?:\s+song|\s+track)?|previous|back|"
        r"pause(?:\s+it|\s+the\s+music)?|resume|play|stop|"
        r"louder|quieter|volume\s+(?:up|down|to\s+\d+)|turn\s+(?:it\s+)?(?:up|down)|"
        r"shuffle|smart\s+shuffle|repeat|"
        r"what(?:'s| is)\s+(?:this|playing|the\s+song)|who(?:'s| is)\s+(?:this|singing)|"
        r"like\s+this|love\s+this|i\s+like\s+this|i\s+love\s+this|"
        r"i\s+don'?t\s+like\s+this|dislike\s+this|thumbs\s+(?:up|down)|"
        r"add\s+this\s+to\s+.+|queue\s+.+|play\s+something\s+.+)"
        r"[.!?]?$", re.IGNORECASE)

    def _passes_activation_gate(self, text: str, confidence: float, wake_initiated: bool = False,
                                follow_up: bool = False):
        """Transcript-level gate for speech that SAINT was not addressed with.

        Returns (ok, reason). Wake-initiated commands always pass: the user
        asked for SAINT by name.
        """
        if wake_initiated:
            return True, "wake_word"
        min_conf = config.get("voice.min_stt_confidence", 0.5)
        short_conf = config.get("voice.short_utterance_confidence", 0.7)
        stripped = (text or "").strip()
        lower = stripped.lower()
        words = stripped.split()

        if re.search(r"\b(alexa|hey google|ok(ay)? google|siri|hey siri|cortana)\b", lower):
            return False, "foreign_wake_word"
        hallucinations = {"you", "thank you", "thanks for watching", "bye", "okay", "ok",
                          "yeah", "so", "uh", "um", "hmm", "the"}
        if lower.strip(".!?, ") in hallucinations and confidence < short_conf:
            return False, "likely_hallucination"
        if len(words) <= 1 and confidence < (0.45 if follow_up else short_conf):
            return False, "short_low_confidence"
        # MUSIC GUARD: when Spotify is playing, follow-ups get bombarded with
        # transcribed lyrics. Reject anything that isn't a short playback-style
        # command; the user can still say "Hey SAINT, <anything>" to bypass.
        if follow_up and self._music_playing and config.get("voice.music_strict_followup", True):
            if not self._MUSIC_FOLLOWUP_OK.match(stripped.strip(".!? ")):
                return False, "music_playing_needs_wake_word"
        if follow_up:
            min_conf = min(min_conf, float(config.get("voice.followup_min_confidence", 0.25)))
        if confidence > 0 and confidence < min_conf:
            return False, f"low_confidence_{confidence:.2f}"
        return True, ""

    @staticmethod
    def _is_punctuation_only(text: str) -> bool:
        return all(ch in string.punctuation or ch.isspace() for ch in text or "")

    def _emit_stt_error(self, session_id: int, error: str):
        event_bus.emit_event(EventType.VOICE_STT_ERROR, {"error": error, "session_id": session_id})

    # ------------------------------------------------------------------ #
    # Injection (text input, tests)
    # ------------------------------------------------------------------ #
    def inject_utterance(self, text: str, confidence: float = 0.95, source: str = "inject"):
        import random
        event_bus.emit_event(EventType.VOICE_STT_FINAL, {
            "text": text,
            "confidence": confidence,
            "latency_ms": 0.0,
            "session_id": random.randint(1, 1_000_000),
            "source": source,
        })

    def inject_interrupt(self):
        event_bus.emit_event(EventType.VOICE_INTERRUPT, {})


# ---------------------------------------------------------------------- #
# Wake chime
# ---------------------------------------------------------------------- #
_CHIME = None


def _play_chime():
    """Short, quiet two-tone chime confirming the wake word (non-blocking)."""
    global _CHIME
    try:
        import sounddevice as sd
        sr = 24000
        if _CHIME is None:
            t1 = np.arange(int(sr * 0.07)) / sr
            t2 = np.arange(int(sr * 0.09)) / sr
            tone = np.concatenate([np.sin(2 * np.pi * 880 * t1), np.sin(2 * np.pi * 1320 * t2)])
            env = np.minimum(1.0, np.minimum(np.arange(len(tone)), np.arange(len(tone))[::-1]) / (sr * 0.01))
            _CHIME = (0.18 * tone * env).astype(np.float32)
        playback_monitor.note_block(_rms(_CHIME), len(_CHIME) / sr)
        sd.play(_CHIME, sr, blocking=False)
    except Exception as e:
        log.debug("voice.chime_failed %s", e)

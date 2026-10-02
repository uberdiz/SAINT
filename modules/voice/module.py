"""
modules/voice/module.py

VoiceModule — microphone capture, wake word, VAD, STT and barge-in.

Listening phases (independent of the UI):

    WAKE      the local wake-word model runs on every frame. Speech is
              buffered tentatively. A short utterance the model did not fire
              on is transcribed once and accepted only if it *starts* with
              "SAINT" / "Hey SAINT" (second-stage wake: the ONNX model is
              trained mostly on "Hey SAINT" and barely scores a bare "SAINT").
              While Spotify plays, that same check also accepts a bare
              playback hot-word ("skip", "pause", "louder") — no wake word.
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
from core import dialog
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
PREROLL_FRAMES = 20     # 600 ms of that pre-roll starts every segment
# Windows where speech is addressed to SAINT (not a follow-up that may be room chatter).
_ADDRESSED = ("awaiting reply", "wake word")
MISSED = "didn't catch that"
_REPLY_HINT = "yes no yeah nope okay sure cancel stop the first one the second one the third one"
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
        self._last_stt_language = ""      # the language Whisper heard (modules/lang uses it as a hint)
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
        self._missed = False               # the last addressed utterance came back empty
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
        # "Stop" spotter while SAINT talks (see _stop_spotter).
        self._spot_frames = collections.deque(maxlen=int(1500 / CHUNK_MS))
        self._spot_voiced = collections.deque(maxlen=int(1500 / CHUNK_MS))
        self._spot_busy = False
        self._spot_hit = False
        self._spot_at = 0.0
        self._spotted_text = ""
        self._tts_text = ""
        self._floor_cache = 0.025
        self._floor_at = 0.0

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
            stt_language = config.get("voice.stt_language", "en")
            initial_prompt = ""
            if config.get("language.multilingual_stt", False):
                # Whisper picks the language for each utterance (and copes with a second one in the
                # same sentence); the ".en" models can't, so use the multilingual one.
                stt_model = config.get("voice.stt_model_multilingual", "small")
                stt_language = "auto"
                initial_prompt = config.get("voice.stt_initial_prompt", "Hey SAINT. Hola SAINT. Play some música.")
            self._stt = make_stt(
                "faster_whisper",
                model_name=stt_model,
                device=stt_device,
                compute_type=stt_compute,
                language=stt_language,
                hotwords=config.get("voice.stt_hotwords", "SAINT"),
                initial_prompt=initial_prompt,
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
            self._reset_spotter()

    def is_tts_playback_active(self) -> bool:
        with self._tts_playback_lock:
            return self._tts_playback_active

    def _saint_is_talking(self) -> bool:
        return self._speaking or self.is_tts_playback_active()

    # ------------------------------------------------------------------ #
    # Talking over SAINT
    # ------------------------------------------------------------------ #
    # What people say to make SAINT stop talking.
    _STOP_PHRASE = re.compile(r"\b(?:stop|shut up|be quiet|quiet|enough|hold on|hang on|wait|cancel|never ?mind|"
                              r"okay okay|ok ok|no no|pause|saint|shush|shh+)\b", re.IGNORECASE)

    @staticmethod
    def _background_floor() -> float:
        """Speech quieter than this, not said to SAINT by name, is the room or the
        speakers (a video, a game, a call), not the user talking to SAINT."""
        floor = float(config.get("voice.background_rms", 0.012))
        try:
            from modules.voice.output_policy import output_policy
            normal = output_policy.normal_level()
        except Exception:
            normal = 0.0
        if normal > 0:
            floor = max(floor, min(0.03, normal * float(config.get("voice.background_ratio", 0.25))))
        return floor

    def _barge_floor(self) -> float:
        """The quietest mic level that can be the user talking over SAINT:
        a fixed minimum, raised to a share of how loud they normally talk."""
        now = time.monotonic()
        if now - self._floor_at > 2.0:
            from modules.voice.output_policy import output_policy
            base = float(config.get("voice.barge_in_min_rms", 0.025))
            ratio = float(config.get("voice.barge_in_level_ratio", 0.4))
            self._floor_cache = max(base, output_policy.normal_level() * ratio)
            self._floor_at = now
        return self._floor_cache

    def _reset_spotter(self):
        self._spot_frames.clear()
        self._spot_voiced.clear()
        self._spot_hit = False

    def _stop_spotter(self, chunk16, raw_voice: bool, mic_rms: float) -> bool:
        """While SAINT talks, short bursts of speech are transcribed on the side:
        "stop" / "shut up" / "wait" interrupts even when the echo gate can't
        tell the user from SAINT's own voice (speakers, loud music). Words SAINT
        is saying itself don't count."""
        if not config.get("voice.barge_in_spotter", True) or self._stt is None:
            return False
        if self._spot_hit:
            self._spot_hit = False
            return True
        self._spot_frames.append(chunk16)
        self._spot_voiced.append(bool(raw_voice and mic_rms >= 0.6 * self._floor_cache))
        voiced = sum(self._spot_voiced)
        now = time.monotonic()
        # Check once a burst of speech (>= 240 ms) has ended — or every second while the
        # sound never stops (music on, a loud room): the burst then never "ends", and
        # "stop" said over it went unheard (2026-10-01).
        ended = not self._spot_voiced[-1]
        if self._spot_busy or voiced < 8 or now - self._spot_at < (0.6 if ended else 1.0):
            return False
        self._spot_busy = True
        self._spot_at = now
        audio = np.concatenate(list(self._spot_frames))

        def run():
            try:
                res = self._stt.transcribe(audio, sample_rate=SAMPLE_RATE)
                heard = (getattr(res, "text", "") or "").strip()
                m = self._STOP_PHRASE.search(heard) if heard else None
                if m and self._music_playing:
                    from modules.spotify.lyrics import lyrics_service
                    if lyrics_service.matches_current(heard):
                        m = None                  # the song singing "wait" / "stop", not the user
                if m and (not self._saint_said(m.group(0)) or not self._is_own_echo(heard)):
                    # A stop word SAINT isn't saying can't be its echo, even when the rest of
                    # the transcript is SAINT's sentence ("…what I found about stop").
                    self._spotted_text = heard
                    self._spot_hit = True
            except Exception:
                log.debug("voice.spotter_failed", exc_info=True)
            finally:
                self._spot_busy = False
        threading.Thread(target=run, daemon=True, name="voice-stop-spotter").start()
        return False

    def _saint_said(self, words: str) -> bool:
        """Is ``words`` in the sentence SAINT is speaking?"""
        return bool(re.search(rf"\b{re.escape(words.lower())}\b", (self._tts_text or "").lower()))

    def _is_own_echo(self, heard: str) -> bool:
        """Is ``heard`` just SAINT's own sentence picked up by the mic?"""
        said = set(re.findall(r"[a-z']+", (self._tts_text or "").lower()))
        words = re.findall(r"[a-z']+", heard.lower())
        if not words or not said:
            return False
        return sum(w in said for w in words) >= 0.6 * len(words)

    _music_playing = False              # updated from spotify.playback.changed events
    _music_loaded = False               # a track is loaded (playing or paused)

    def _on_bus_event(self, ev):
        if ev.type == EventType.TTS_SPEAK_START:
            self._tts_text = str((ev.payload or {}).get("text") or "")
            return
        if ev.type == EventType.SPOTIFY_PLAYBACK_CHANGED:
            self._music_playing = bool((ev.payload or {}).get("is_playing"))
            self._music_loaded = bool((ev.payload or {}).get("track"))
        if ev.type == EventType.CONVERSATION_TURN_END:
            if not (self._listening and self.wake_mode):
                return
            if ev.payload.get("expects_reply"):
                # SAINT asked a question (e.g. a confirmation) — accept the
                # answer without requiring the wake word again.
                # The window lasts as long as the question does (it used to
                # close after 8 s while the question stayed open for 30 s).
                # No chime by default: right after SAINT's own voice it sounded
                # like a glitch (2026-09-28); the status shows it's waiting.
                self._enter_command(dialog.reply_window(8.0), reason="awaiting reply",
                                    chime=bool(config.get("voice.reply_chime", False)))
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
        from core.game_mode import game_mode
        if not game_mode.feature("wake_word"):            # Gaming Mode setting: wake word off
            log.info("voice.wake_word.ignored gaming_mode score=%.3f", score)
            return
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
        # A command gets a longer pause before it counts as finished: people
        # breathe between the steps of "open my browser, search YouTube for X...".
        command_silence_frames = max(silence_frames,
                                     int(config.get("voice.command_silence_ms", 1000) / CHUNK_MS))
        # An answer to SAINT's question ("Yes.") is one breath: don't make the
        # user wait a whole second of silence before SAINT reacts.
        reply_silence_frames = max(1, int(config.get("voice.reply_silence_ms", 550) / CHUNK_MS))
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
        seg_reason = None          # command reason when the segment started (None: passive)
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
                self._echo_gate.speech_floor = self._barge_floor()
                gate_fired = barge_enabled and raw_voice and self._echo_gate.update(mic_rms, ref)
                spotted = barge_enabled and self._stop_spotter(chunk16, raw_voice, mic_rms)
                if gate_fired or spotted:
                    if spotted:
                        log.info("voice.barge_in.spotted %r", self._spotted_text)
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
                    # A spotted "stop" was already said: keep it in the utterance.
                    seg = list(self._spot_frames) if spotted else list(preroll)[-(self._echo_gate.min_frames + 10):]
                    self._reset_spotter()
                    seg_reason = "interruption"         # not whatever the previous segment was
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
                    seg = list(preroll)[-PREROLL_FRAMES:]
                    with self._phase_lock:
                        seg_reason = self._command_reason if self._phase == ListenPhase.COMMAND else None
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
                                      min_speech_rms, was_barge, seg_reason, trailing=silence_count)
                    seg, speech_count, silence_count = [], 0, 0
            elif in_speech:
                silence_count += 1
                seg.append(chunk16)
                if self.phase != ListenPhase.COMMAND:
                    needed = silence_frames
                elif seg_reason == "awaiting reply" and dialog.expects_short_answer():
                    needed = reply_silence_frames
                else:
                    needed = command_silence_frames
                if silence_count >= needed:
                    in_speech = False
                    was_barge = barge_capture
                    barge_capture = False
                    self._end_segment(seg, speech_count, sid, min_speech_frames,
                                      min_speech_rms, was_barge, seg_reason, trailing=silence_count)
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
                     was_barge: bool, started_reason: Optional[str] = None, trailing: int = 0):
        phase = self.phase
        if phase == ListenPhase.WAKE and started_reason:
            # The user started talking while SAINT was listening for a command
            # and kept going after an early fragment was handled: the rest of
            # the sentence is still addressed to SAINT, not background speech.
            log.info("voice.segment.continued reason=%s", started_reason)
            self._enter_command(float(config.get("voice.wake_word_command_timeout_sec", 6.0)), started_reason)
            phase = ListenPhase.COMMAND
        if phase == ListenPhase.WAKE:
            # Speech the wake model did not fire on. Short utterances get one
            # transcript check ("SAINT, skip this" — the model barely scores a
            # bare "SAINT"); they are acted on only if the transcript *starts*
            # with the wake word. Everything else is dropped untranscribed.
            dur = len(frames) * CHUNK_MS / 1000.0
            hot = self._hotwords_active()
            # A quick "skip" is shorter than the normal minimum speech length.
            min_frames = min(min_speech_frames, max(1, int(180 / CHUNK_MS))) if hot else min_speech_frames
            checking = config.get("voice.wake_word_transcript_check", True) or hot
            max_dur = float(config.get("voice.wake_word_transcript_max_sec", 7.0))
            valid = self._validate_speech_session(frames, speech_count, min_frames, min_speech_rms)
            if hot and checking and not (0.3 <= dur <= max_dur and valid):
                log.debug("voice.passive.skipped dur=%.2fs speech_frames=%d valid=%s", dur, speech_count, valid)
            if (checking and self._stt is not None and 0.3 <= dur <= max_dur and valid):
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
        # SAINT is listening for a command or an answer: "Yes." / "Stop." is
        # only 4–8 voiced frames, far below the 300 ms minimum that keeps
        # clicks and coughs out of passive listening (logged 2026-09-25: the
        # reply to "Do you want me to close Disk Cleanup?" was dropped here).
        # The ratio is measured over the spoken part, not the pre-roll and
        # the trailing silence that every segment carries.
        min_frames = min_speech_frames
        if phase == ListenPhase.COMMAND:
            short_ms = config.get("voice.min_command_speech_ms", 120)
            if started_reason == "awaiting reply":
                short_ms = min(short_ms, 90)
            min_frames = min(min_speech_frames, max(1, int(short_ms / CHUNK_MS)))
        pad = min(len(frames) - speech_count, PREROLL_FRAMES + trailing) if phase == ListenPhase.COMMAND else 0
        if not self._validate_speech_session(frames, speech_count, min_frames, min_speech_rms, pad=pad):
            self._emit_vad_reject(sid, speech_count, len(frames), frames, reason=started_reason)
            if started_reason in _ADDRESSED and speech_count >= 2:
                # Speech aimed at SAINT was too faint/short to use: say so on
                # screen instead of silently waiting ("why didn't it respond?").
                assistant_state.set(AssistantState.COMMAND_LISTENING, MISSED)
            return
        wake_initiated = phase == ListenPhase.COMMAND
        with self._phase_lock:
            follow_up = wake_initiated and self.unaddressed_window(self._command_reason)
        if wake_initiated:
            with self._phase_lock:
                self._awaiting_stt = True
            assistant_state.set(AssistantState.PROCESSING, "transcribing")
        self._transcribe(frames, sid, wake_initiated=wake_initiated, allow_during_tts=was_barge,
                         follow_up=follow_up)

    def _validate_speech_session(self, frames: list, speech_frame_count: int,
                                 min_speech_frames: int, min_speech_rms: float, pad: int = 0) -> bool:
        """``pad``: frames known not to be speech (pre-roll + trailing silence),
        left out of the voiced-ratio check."""
        if not frames:
            return False
        if speech_frame_count < min_speech_frames:
            return False
        if speech_frame_count / max(1, len(frames) - max(0, pad)) < 0.1:
            return False
        audio = np.concatenate(frames)
        return _rms(audio) >= min_speech_rms

    def _emit_vad_reject(self, session_id: int, speech_frames: int, total_frames: int, frames: list = None,
                         reason: Optional[str] = None):
        rms = _rms(np.concatenate(frames)) if frames else 0.0
        ratio = speech_frames / total_frames if total_frames else 0.0
        # Visible at the default log level: "why didn't it hear me?" starts here.
        log.info("voice.vad.rejected window=%s speech_ms=%d rms=%.4f", reason or "-",
                 speech_frames * CHUNK_MS, rms)
        event_bus.emit_event(EventType.VOICE_STT_SKIP, {
            "session_id": session_id, "reason": "insufficient_speech",
            "speech_frames": speech_frames, "total_frames": total_frames,
            "speech_ratio": round(ratio, 3), "rms": round(rms, 6), "window": reason or "",
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
            missed, self._missed = self._missed, False
            assistant_state.set(AssistantState.COMMAND_LISTENING, MISSED if missed else self._command_reason)

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
            # SAINT asked yes/no or "which one?": nudge Whisper toward those
            # words (it scores a lone "Yes." near 0 and sometimes mishears it).
            hint = _REPLY_HINT if wake_initiated and dialog.expects_short_answer() else ""
            try:
                result = self._stt.transcribe(audio, sample_rate=SAMPLE_RATE, hint=hint)
                self._last_stt_language = getattr(result, "language", "") or ""
            except TypeError:                     # an STT backend without hints
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
        if raw_text and config.get("voice.repetition_filter", True):
            from modules.voice.stt_filters import clean_transcript
            cleaned = clean_transcript(raw_text)
            if cleaned is None:
                log.info("voice.stt.repetition_dropped session=%s words=%d", session_id, len(raw_text.split()))
                event_bus.emit_event(EventType.VOICE_STT_SKIP, {
                    "session_id": session_id, "reason": "repetition", "text": raw_text[:80]})
                return ""
            raw_text = cleaned
        hot = False
        if wake_check:
            # Second-stage wake: only a transcript that starts with the wake
            # word counts — or, while music plays, a bare playback hot-word
            # ("skip", "pause"). Nothing else from passive listening is kept or shown.
            hot = not self._starts_with_wake(raw_text) and self._is_music_hotword(raw_text, confidence)
            if hot and 0 < rms < self._background_floor():
                # "Next" / "skip it" sung by the song itself, coming back through the
                # speakers: far quieter than the user saying it (2026-10-01).
                log.info("voice.hotword.too_quiet rms=%.4f text=%r", rms, raw_text)
                return ""
            if hot:
                text = raw_text.strip()
                log.info("voice.hotword session=%s conf=%.2f text=%r", session_id, confidence, text)
                event_bus.emit_event(EventType.VOICE_HOTWORD, {"text": text.strip(".!?, ")})
                wake_initiated = True
            else:
                if not self._starts_with_wake(raw_text) and not self._head_says_wake(audio):
                    # Never log what bystanders said — only that it wasn't addressed to SAINT.
                    log.debug("voice.wake.transcript_rejected session=%s words=%d",
                              session_id, len(raw_text.split()))
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
            if follow_up and self._starts_with_wake(raw_text):
                # "Hey SAINT" said during a follow-up window addresses SAINT by
                # name: treat it as a fresh wake, not an unaddressed follow-up.
                follow_up = False
                if not text.strip() or self._is_punctuation_only(text.strip()):
                    self._on_wake_word(0.0, source="transcript")
        log.info("stt.result session=%s conf=%.2f wake=%s ms=%.0f text=%r",
                 session_id, confidence, wake_initiated, inference_ms, raw_text)
        event_bus.emit_event(EventType.VOICE_SPEECH_END, {"session_id": session_id})

        if not text.strip() or self._is_punctuation_only(text.strip()):
            with self._phase_lock:
                self._missed = not raw_text.strip() and self._command_reason in _ADDRESSED
            event_bus.emit_event(EventType.VOICE_STT_SKIP, {
                "session_id": session_id, "reason": "empty_after_wake_strip" if raw_text else "empty",
                "text": raw_text})
            return ""

        if self._FRAGMENT.match(text.strip()):
            # "and" / "um" is the start of a sentence cut at a breath, never a
            # request: keep listening for the rest instead of answering it.
            event_bus.emit_event(EventType.VOICE_STT_SKIP, {
                "session_id": session_id, "reason": "fragment", "text": text})
            return ""

        ok, reason = self._passes_activation_gate(text, confidence,
                                                  wake_initiated=wake_initiated and not follow_up,
                                                  follow_up=follow_up, audio=audio, rms=rms)
        if ok and wake_initiated and not follow_up and not hot and not self._music_playing \
                and self._command_reason in _ADDRESSED and config.get("voice.learn_my_voice", True):
            # Said to SAINT by name (or answering it): the user's voice. Builds the
            # profile that tells them apart from songs, videos and other people.
            threading.Thread(target=self._learn_voice, args=(audio,), daemon=True, name="voice-profile").start()
        if not ok:
            log.info("voice.activation.rejected session=%s reason=%s conf=%.2f text=%r",
                     session_id, reason, confidence, text)
            event_bus.emit_event(EventType.VOICE_STT_SKIP, {
                "session_id": session_id, "reason": f"activation_gate:{reason}",
                "text": text, "confidence": round(confidence, 3)})
            return ""

        from modules.voice.output_policy import output_policy
        output_policy.note_input(rms)
        event_bus.emit_event(EventType.VOICE_STT_FINAL, {
            "text": text,
            "confidence": confidence,
            "latency_ms": round(total_ms, 1),
            "inference_ms": round(inference_ms, 1),
            "session_id": session_id,
            "wake": wake_initiated,
            "language": self._last_stt_language,
            **({"source": "hotword"} if hot else {}),
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
        r"^\s*(?:(?:hey|hay|they|hi|ok(?:ay)?|oye|hola|ey|eh|salut|hallo|ciao|ol[aá]|oi|ehi)[\s,.!]+)?"
        r"(?:saint(?:s|e|'s)?|sant|sane)\b[\s,.:;!?-]*",
        re.IGNORECASE)

    # Whisper sometimes renders "Hey SAINT" as "Hey, St." — only strip that
    # form after a greeting, so "St. Louis weather" is left alone.
    _WAKE_PREFIX_ALT = re.compile(
        r"^\s*(?:hey|hay|they|hi|ok(?:ay)?|oye|hola|ey|eh|salut|hallo|ciao|ol[aá]|oi|ehi)[\s,.!]+(?:st\.?|saint(?:s|e|'s)?|sant|sane)(?=[\s,.!?]|$)[\s,.:;!?-]*",
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
        r"^\s*(?:(?:um+|uh+|so|okay|ok|oh|pues|bueno|euh)[\s,.!]+)?(?:(?:hey|hay|they|hi|ok(?:ay)?|oye|hola|ey|eh|salut|hallo|ciao|ol[aá]|oi|ehi|yo)[\s,.!]+)?"
        r"(?:saint(?:s|e|'s)?|sant|sane)(?=[\s,.!?]|$)", re.IGNORECASE)
    _WAKE_START_ALT = re.compile(r"^\s*(?:hey|hay|they|hi|ok(?:ay)?)[\s,.!]+st\.?(?=[\s,.!?]|$)", re.IGNORECASE)

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
        ok = bool(re.match(r"^\W*(?:(?:hey|hay|they|hi|ok(?:ay)?)\W+)?saint(?:s)?\b", text, re.I))
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
        r"louder|quieter|volume\s+(?:up|down|to\s+\d+)|turn\s+(?:it\s+)?(?:back\s+)?(?:up|down)(?:\s+a\s+(?:little|bit))?|"
        r"shuffle|smart\s+shuffle|repeat|"
        r"what(?:'s| is)\s+(?:this|playing|the\s+song)|who(?:'s| is)\s+(?:this|singing)|"
        r"like\s+this|love\s+this|i\s+like\s+this|i\s+love\s+this|"
        r"i\s+don'?t\s+like\s+this|dislike\s+this|thumbs\s+(?:up|down)|"
        r"add\s+this\s+to\s+.+|queue\s+.+|play\s+something\s+.+)"
        r"[.!?]?$", re.IGNORECASE)

    # Playback commands accepted with NO wake word while music is active. The
    # whole utterance must be one of these, so lyrics and chatter don't count.
    _MUSIC_HOTWORD = re.compile(
        r"^(?:skip(?:\s+(?:it|this|that|song|track|(?:this|that|the)\s+(?:song|track|one)))?|next(?:\s+(?:song|track))?|"
        r"go\s+back|previous\s+(?:song|track)|last\s+song|"
        r"pause(?:\s+(?:it|music|the\s+music|spotify))?|resume(?:\s+(?:the\s+)?music)?|unpause|"
        r"(?:play|keep\s+playing)\s+(?:the\s+)?music|"
        r"louder|quieter|volume\s+(?:up|down)|turn\s+(?:it|the\s+music)\s+(?:up|down)|"
        r"(?:i\s+)?(?:like|love)\s+this(?:\s+song)?)$", re.IGNORECASE)

    # One-word (or two-word) answers and reactions right after SAINT speaks or
    # while talking over it: "Yup.", "No.", "Wait.", "Shut up." Whisper scores
    # them near 0, so they used to be dropped as noise (2026-09-30).
    _SHORT_REPLY = re.compile(
        r"^(?:yes|yeah|yep|yup|ya|yea|sure|okay|ok|alright|all right|correct|exactly|perfect|please|"
        r"no|nope|nah|not that|wrong|never ?mind|forget it|cancel|stop|stop it|stop talking|wait|hold on|"
        r"hang on|shut up|be quiet|quiet|enough|thanks|thank you|cool|nice|great|good|do it|go ahead|"
        r"that one|the first one|the second one|the last one|again|repeat that|what|huh|louder|quieter)"
        r"(?:[\s,.!?]+(?:saint|please|thanks|then))*[\s.!?]*$", re.IGNORECASE)
    _NAMED = re.compile(r"\bsaint\b", re.IGNORECASE)

    _FRAGMENT = re.compile(r"^(?:and|and then|then|so|um+|uh+|but|or|also|like|okay so)[\s.,!?…-]*$", re.IGNORECASE)

    # A question put to SAINT ("What's that reminder for?"), as opposed to a
    # lyric or a remark. Whisper only adds the '?' when it hears a question.
    _DIRECT_QUESTION = re.compile(
        r"^(?:what|what's|whats|who|who's|where|where's|when|why|how|is|are|can|could|will|would|do|does|"
        r"did|should)\b.*\?$", re.IGNORECASE)

    # A request put to SAINT that its router may not know yet ("Open YouTube on my
    # main screen" was rejected three times in a row on 2026-09-28). Room talk
    # rarely starts with a command verb; lyrics are still held back by the music guard.
    _REQUEST = re.compile(
        r"^(?:(?:and|also|now|ok(?:ay)?|so|please|hey)[\s,]+)*(?:(?:can|could|would|will) you\s+|please\s+)?"
        r"(?:open|close|launch|start|play|pause|resume|stop|skip|search|google|look up|find|show|turn|set|switch|"
        r"move|put|minimi[sz]e|maximi[sz]e|click|double click|right click|mute|unmute|take|make|scroll|press|"
        r"type|read|go (?:to|back)|bring up|pull up|tell me|remind me|lower|raise)\b", re.IGNORECASE)

    # Remarks and interjections that are never meant for SAINT.
    _CHATTER = re.compile(
        r"^(?:(?:ok(?:ay)?|oh|ah|yeah|yep|nah|no|wow|lol|damn|shit|fuck|dude|bro|man|huh|hmm|what|"
        r"why not|oh my god|omg|see|look|nice|cool|great|right|sure|well)[\s.,!?]*){1,6}$"
        r"|^(?:see how|i can'?t believe|that'?s (?:so|crazy|funny|wild)|you'?re (?:so )?(?:right|good))\b",
        re.IGNORECASE)

    @staticmethod
    def _ai_expects_reply() -> bool:
        """SAINT's last answer asked the user something (the LLM path)."""
        try:
            from core.module_manager import module_manager
            return bool(getattr(module_manager.get("ai"), "expects_reply", False))
        except Exception:
            return False

    @staticmethod
    def _answers_set_aside(text: str) -> bool:
        """A plain yes/no to a question the user talked past a moment ago."""
        try:
            from modules.agent.confirm import confirmations
            return confirmations.can_answer(text)
        except Exception:
            return False

    @staticmethod
    def _question_pending() -> bool:
        """SAINT asked a yes/no or pick-one question that is still open."""
        try:
            from modules.agent.confirm import choices, confirmations
            return confirmations.pending is not None or choices.pending is not None
        except Exception:
            return False

    @staticmethod
    def unaddressed_window(reason: str) -> bool:
        """Speech in a follow-up window ("follow-up", "follow-up (action)") or
        while talking over SAINT ("interruption") wasn't addressed to SAINT by
        name: it must pass the follow-up gate, or a video playing or a
        conversation in the room becomes a request (logged 2026-09-25)."""
        reason = reason or ""
        return reason.startswith("follow-up") or reason == "interruption"

    @staticmethod
    def _is_command(text: str) -> bool:
        """Does the agent recognise ``text`` as something to do?"""
        try:
            from modules.agent.agent import agent
            return agent.accepts_followup(text)
        except Exception as e:
            log.debug("voice.is_command_failed %s", e)
            return False

    def _hotwords_active(self) -> bool:
        """Music is playing, or paused while the Spotify widget is on screen."""
        if not config.get("voice.music_hotwords", True):
            return False
        return self._music_playing or (self._music_loaded and bool(config.get("widgets.spotify", False)))

    def _is_music_hotword(self, text: str, confidence: float) -> bool:
        if not self._hotwords_active():
            return False
        said = (text or "").strip().strip(".!?,").strip()
        if not self._MUSIC_HOTWORD.match(said):
            return False
        # Whisper gives one- and two-word utterances near-zero confidence
        # ("Skip." came back at 0.02), so it can't judge them; the exact
        # whole-utterance match above is the safeguard for those.
        if len(said.split()) <= 2:
            return True
        floor = float(config.get("voice.hotword_min_confidence", 0.2))
        if 0 < confidence < floor:
            log.info("voice.hotword.rejected conf=%.2f floor=%.2f text=%r", confidence, floor, text)
            return False
        return True

    def _learn_voice(self, audio):
        try:
            from modules.voice.speaker import speaker_profile
            if speaker_profile.learn(audio):
                log.debug("voice.profile.learned samples=%d", speaker_profile.samples)
        except Exception:
            log.debug("voice.profile.learn_failed", exc_info=True)

    @staticmethod
    def _voice_is_user(audio) -> Optional[bool]:
        """True/False from the user's voice profile; None when it can't tell."""
        if audio is None:
            return None
        try:
            from modules.voice.speaker import speaker_profile
            return speaker_profile.is_user(audio)
        except Exception:
            log.debug("voice.profile.score_failed", exc_info=True)
            return None

    def _note_singing(self, audio, rms: float, text: str):
        """The user singing along to the song that's playing (a lyric heard in
        *their* voice, not the speakers'): a strong "likes this song" signal."""
        is_user = self._voice_is_user(audio)
        if is_user is False:
            return
        if is_user is None:
            # No voice profile yet: only speech at their normal talking level counts
            # (song vocals leaking from speakers are much quieter on the mic).
            from modules.voice.output_policy import output_policy
            normal = output_policy.normal_level()
            if not normal or rms < 0.6 * normal:
                return
        try:
            from core.module_manager import module_manager
            sp = module_manager.get("spotify")
            tools = getattr(sp, "tools", None)
            if tools is not None and tools.note_sang_along(text):
                log.info("voice.sang_along words=%d", len(text.split()))
        except Exception:
            log.debug("voice.sang_along_failed", exc_info=True)

    def _passes_activation_gate(self, text: str, confidence: float, wake_initiated: bool = False,
                                follow_up: bool = False, audio=None, rms: float = 0.0):
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
        # Wake word off: every sound in the room reaches here unaddressed. It has
        # to be something SAINT would act on, a question, or an answer — the same
        # bar as a follow-up — or lyrics and room talk become requests ("INTRO
        # MUSIC", "this is tough.", "wow" all got replies on 2026-09-30).
        wake = getattr(self, "_wake", None)
        open_mic = not follow_up and not (wake is not None and wake.ready)
        interrupting = getattr(self, "_command_reason", "") == "interruption"

        if re.search(r"\b(alexa|hey google|ok(ay)? google|siri|hey siri|cortana)\b", lower):
            return False, "foreign_wake_word"
        # The song singing, not the user: "Open up your eyes." (a lyric) opened the dashboard
        # and was learned as a skill on 2026-09-29. Checked against the synced lyrics near
        # the current position, so a real command still gets through.
        if self._music_playing and len(words) >= 3 and config.get("voice.lyrics_filter", True):
            try:
                from modules.spotify.lyrics import lyrics_service
                if lyrics_service.matches_current(stripped):
                    if config.get("spotify.learn_singing", True):
                        self._note_singing(audio, rms, stripped)
                    return False, "song_lyrics"
            except Exception as e:
                log.debug("voice.lyrics_check_failed %s", e)
        # Not the user's voice (a video, a song, someone else) and not said to
        # SAINT by name: ignored when "Only respond to my voice" is on.
        if (follow_up or open_mic) and config.get("voice.speaker_filter", False) \
                and self._voice_is_user(audio) is False:
            return False, "not_your_voice"
        # An answer to SAINT's own question ("Yeah." after "Close Disk Cleanup?")
        # is never a hallucination, however short or low-scored.
        answering = self._ai_expects_reply() or self._question_pending() or self._answers_set_aside(stripped)
        # Too quiet to be the user talking to SAINT: a video, the game, a call or
        # the TV coming through the speakers. Background speech that became
        # requests on 2026-09-30 ("So we'll have next." skipped a song) was at
        # 0.005-0.016 RMS; the user's own commands at 0.04-0.16.
        if rms > 0 and not answering and rms < self._background_floor():
            return False, "too_quiet_background"
        # Said SAINT's name somewhere ("SHUT UP SAINT SHUT UP" while it talked):
        # addressed, even though it didn't start with the wake word.
        if len(words) <= 10 and self._NAMED.search(stripped) and confidence >= 0.2:
            return True, "named"
        short_reply = bool(self._SHORT_REPLY.match(stripped))
        # A plain follow-up also needs the user's loudness: Whisper makes "Yeah."
        # out of noise, and noise is quiet.
        if short_reply and (interrupting or answering or (follow_up and not self._music_playing and rms > 0)):
            # Talking over SAINT or answering right after it spoke: a one-word
            # answer is the most likely thing to say, never noise to drop.
            return True, "short_reply"
        hallucinations = {"you", "thank you", "thanks for watching", "bye", "okay", "ok",
                          "yeah", "so", "uh", "um", "hmm", "the"}
        if lower.strip(".!?, ") in hallucinations and confidence < short_conf and not answering:
            return False, "likely_hallucination"
        # Whisper scores short commands low ("Click it." came back at 0.13, "Press
        # enter." at 0.08), so something SAINT recognises as a command gets a
        # lower confidence floor — in a follow-up window or in open listening.
        command = bool(self._MUSIC_FOLLOWUP_OK.match(stripped.strip(".!? "))) or self._is_command(stripped)
        command_conf = float(config.get("voice.followup_command_min_confidence", 0.1))
        # One-word follow-ups ("Pause.") come back at confidence 0.00; a word
        # SAINT recognises as a command is trusted, anything else isn't.
        if len(words) <= 1 and not command and confidence < (0.45 if follow_up else short_conf):
            return False, "short_low_confidence"
        # MUSIC GUARD: when Spotify is playing, follow-ups get bombarded with
        # transcribed lyrics. Keep what SAINT would act on (a command it
        # recognises, an answer to its question) or a short direct question;
        # drop chatter and lyrics. "Hey SAINT, <anything>" always bypasses.
        if (follow_up or open_mic) and self._music_playing and config.get("voice.music_strict_followup", True):
            if not (command or answering or self._DIRECT_QUESTION.match(stripped) and len(words) <= 14):
                return False, "music_playing_needs_wake_word"
        # Talking to someone else: without the wake word, only something SAINT
        # would act on, a direct question, or an answer SAINT asked for counts.
        if (follow_up and config.get("voice.followup_requires_intent", True)) or open_mic:
            if self._CHATTER.match(stripped) and not command:
                return False, ("followup" if follow_up else "open_mic") + "_chatter"
            request = bool(self._REQUEST.match(stripped)) and len(words) <= 14
            if not (command or request or self._DIRECT_QUESTION.match(stripped) or self._ai_expects_reply()):
                return False, ("followup" if follow_up else "open_mic") + "_no_intent"
        if follow_up:
            min_conf = min(min_conf, float(config.get("voice.followup_min_confidence", 0.25)))
        if command:
            min_conf = 0.0 if len(words) <= 3 else min(min_conf, command_conf)
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

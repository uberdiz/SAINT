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
import logging
from typing import Optional

import numpy as np

from modules.base import BaseModule
from modules.voice.vad import make_vad, RmsVAD
from modules.voice.stt import make_stt, STTEngine, MockSTT
from modules.voice.voice_state import get_voice_state, VoiceState
from core.events import event_bus, EventType
from core.config import config

# Audio settings
SAMPLE_RATE = 16000     # Hz — Whisper requires 16 kHz
CHANNELS = 1
CHUNK_MS = 30           # milliseconds per VAD frame
CHUNK_FRAMES = int(SAMPLE_RATE * CHUNK_MS / 1000)

# Audio level logging throttle
_LAST_LEVEL_LOG = 0.0
_LAST_LEVEL_LOG_TIME = 0.0

# STT session state machine
STT_STATE_CREATED = "CREATED"
STT_STATE_RECORDING = "RECORDING"
STT_STATE_ENDED = "ENDED"
STT_STATE_TRANSCRIBING = "TRANSCRIBING"
STT_STATE_TRANSCRIBED = "TRANSCRIBED"
STT_STATE_SUBMITTED = "SUBMITTED"
STT_STATE_COMPLETED = "COMPLETED"


class VoiceModule(BaseModule):
    name = "Voice"
    description = "Voice input and speech recognition."

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

        # Use centralized voice state machine
        self._voice_state = get_voice_state()

        # Voice State Manager — single authoritative state
        self._voice_active = False     # microphone capture is active
        self._listening = False         # listening process is running
        self._ptt_held = False
        self._speaking = False

        # Interrupt debouncing
        self._last_interrupt_time = 0.0
        self._interrupt_debounce_sec = 0.5  # 500ms debounce

        # Audio machinery
        self._audio_queue: queue.Queue = queue.Queue()
        self._stream = None
        self._vad: Optional[RmsVAD] = None
        self._stt: Optional[STTEngine] = None

        # Worker threads
        self._capture_thread: Optional[threading.Thread] = None
        self._process_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Speech session ID — each speech session gets a unique ID
        self._speech_session_id: int = 0
        self._speech_id_lock = threading.Lock()

        # Cancel STT processing flag
        self._stt_cancelled = threading.Event()

        # TTS playback state — set by TTS engine when audio actually plays
        self._tts_playback_active = False
        self._tts_playback_lock = threading.Lock()

        # Current turn_id being processed (for queue cancellation)
        self._current_turn_id: int = -1
        self._turn_id_lock = threading.Lock()

        # Barge-in: track pending interrupt from VAD during TTS
        self._pending_barge_in = False

        # STT worker thread (non-blocking Whisper)
        self._stt_worker: Optional[threading.Thread] = None
        self._stt_worker_queue: queue.Queue = queue.Queue()
        self._stt_worker_stop = threading.Event()
        self._audio_level: float = 0.0
        self._last_level_event_time: float = 0.0

        # Diagnostics
        self._stt_device_info: str = ""
        self._tts_device_info: str = ""
        self._worker_id_counter: int = 0
        self._active_workers: dict = {}
        self._worker_id_lock = threading.Lock()

        # STT session state machine per session
        self._stt_session_state: dict = {}
        self._stt_session_state_lock = threading.Lock()

        # Audio level logging throttle
        self._last_level_log_time = 0.0

    @property
    def voice_active(self) -> bool:
        """Single authoritative answer: is microphone capture active?"""
        return self._voice_active

    @property
    def audio_level(self) -> float:
        return self._audio_level

    @property
    def diagnostics(self) -> dict:
        return {
            "voice_active": self._voice_active,
            "listening": self._listening,
            "ptt_active": self._ptt_held,
            "stream_active": self._stream is not None and hasattr(self._stream, 'active') and self._stream.active if self._stream else False,
            "stt_loaded": self._stt is not None,
            "tts_loaded": False,
            "stt_device": self._stt_device_info,
            "tts_device": self._tts_device_info,
            "active_workers": list(self._active_workers.values()),
            "speech_session_id": self._speech_session_id,
        }

    # ------------------------------------------------------------------ #
    # Worker tracking
    # ------------------------------------------------------------------ #
    def _register_worker(self, name: str) -> int:
        with self._worker_id_lock:
            self._worker_id_counter += 1
            wid = self._worker_id_counter
            self._active_workers[str(wid)] = name
            return wid

    def _unregister_worker(self, wid: int):
        with self._worker_id_lock:
            self._active_workers.pop(str(wid), None)

    def _log_worker_state(self):
        with self._worker_id_lock:
            if self._active_workers:
                pass

    # ------------------------------------------------------------------ #
    # Enable / disable
    # ------------------------------------------------------------------ #
    def enable(self):
        super().enable()
        # Idempotent: only init engines once
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
            stt_model = config.get("voice.stt_model", "tiny.en")
            stt_lang = config.get("voice.stt_language", "en")
            self._stt = make_stt(
                "faster_whisper",
                model_name=stt_model,
                device=stt_device,
                compute_type=stt_compute,
                language=stt_lang,
            )
            self._stt_device_info = f"{stt_device}/{stt_compute}/{stt_model}"
            threading.Thread(
                target=self._warmup_stt,
                daemon=True,
                name="voice-warmup",
            ).start()
        else:
            self._stt = make_stt("mock")
            self._stt_device_info = "mock"

    def _warmup_stt(self):
        try:
            if self._stt:
                self._stt.warm_up()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Listening control — single authoritative lifecycle
    # ------------------------------------------------------------------ #
    def start_listening(self):
        if self._listening:
            return
        self._voice_active = True
        self._listening = True
        self._stop_event.clear()
        self._stt_cancelled.clear()
        self._register_worker("mic_capture")
        self._process_thread = threading.Thread(
            target=self._process_loop, daemon=True, name="voice-process"
        )
        self._process_thread.start()
        self._start_stream()
        event_bus.emit_event(EventType.VOICE_LISTENING_START)

    def stop_listening(self):
        if not self._listening:
            return
        self._voice_active = False
        self._listening = False
        self._stop_event.set()
        self._stop_stream()
        self._stt_cancelled.set()
        if self._stt_worker and self._stt_worker.is_alive():
            self._stt_worker.join(timeout=2.0)
        if self._process_thread:
            self._process_thread.join(timeout=3.0)
            self._process_thread = None
        event_bus.emit_event(EventType.VOICE_LISTENING_STOP)

    def stop(self):
        """Full shutdown: stop everything."""
        self.stop_listening()
        self._stream = None

    # ------------------------------------------------------------------ #
    # Push-to-talk
    # ------------------------------------------------------------------ #
    def ptt_toggle(self, held: bool):
        self._ptt_held = held

    # ------------------------------------------------------------------ #
    # External state
    # ------------------------------------------------------------------ #
    def set_saint_speaking(self, speaking: bool):
        self._speaking = speaking

    def set_tts_playback_active(self, active: bool):
        with self._tts_playback_lock:
            if self._tts_playback_active != active:
                self._tts_playback_active = active
                import logging
                logging.getLogger("saint.voice").info(
                    f"voice.tts.playback {'start' if active else 'stop'}"
                )

    def is_tts_playback_active(self) -> bool:
        with self._tts_playback_lock:
            return self._tts_playback_active

    # ------------------------------------------------------------------ #
    # Audio level — throttled
    # ------------------------------------------------------------------ #
    @property
    def audio_level(self) -> float:
        return self._audio_level

    def _emit_audio_level(self, level: float):
        now = time.perf_counter()
        # Throttle to ~30Hz for UI, ~2Hz for logging
        self._last_level_event_time = now
        event_bus.emit_event(EventType.VOICE_AUDIO_LEVEL, {"level": level})

    # ------------------------------------------------------------------ #
    # Sounddevice stream
    # ------------------------------------------------------------------ #
    def _start_stream(self):
        try:
            import sounddevice as sd

            device = config.get("voice.mic_device", None)

            device_info = sd.query_devices(device, 'input')
            actual_sr = int(device_info['default_samplerate'])
            self._actual_sr = actual_sr

            blocksize = int(actual_sr * CHUNK_MS / 1000)

            self._stream = sd.InputStream(
                samplerate=actual_sr,
                channels=CHANNELS,
                dtype="int16",
                blocksize=blocksize,
                device=device,
                callback=self._audio_callback,
            )
            self._stream.start()
        except Exception as e:
            event_bus.emit_event(EventType.ERROR, {"error": f"Mic open failed: {e}"})
            self._voice_active = False
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
        if status:
            pass
        chunk = indata[:, 0].copy()
        if not self._voice_active:
            return
        self._audio_queue.put(chunk)
        if self._vad:
            level = self._vad.get_level(chunk)
            self._audio_level = level
            now = time.perf_counter()
            if now - self._last_level_event_time > 1.0 / 10:
                self._last_level_event_time = now
                self._emit_audio_level(level)

    # ------------------------------------------------------------------ #
    # Processing loop
    # ------------------------------------------------------------------ #
    def _process_loop(self):
        mode = config.get("voice.mode", "always_on")
        silence_ms = config.get("voice.silence_duration_ms", 400)
        silence_frames = int(silence_ms / CHUNK_MS)

        # Minimum speech validation settings
        min_speech_ms = config.get("voice.min_speech_duration_ms", 300)
        min_speech_frames = int(min_speech_ms / CHUNK_MS)
        min_speech_rms = config.get("voice.min_speech_rms", 0.005)

        speech_frames = []
        silence_count = 0
        in_speech = False
        speech_frame_count = 0  # Count of frames where VAD detected speech

        while not self._stop_event.is_set():
            try:
                chunk = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            # Check cancellation (e.g., stop_listening was called)
            if self._stop_event.is_set() or not self._voice_active:
                break

            chunk_np = chunk.astype(np.float32) / 32768.0

            actual_sr = getattr(self, "_actual_sr", SAMPLE_RATE)
            if actual_sr != SAMPLE_RATE:
                target_len = int(len(chunk_np) * SAMPLE_RATE / actual_sr)
                x_old = np.linspace(0, 1, len(chunk_np))
                x_new = np.linspace(0, 1, target_len)
                chunk_np = np.interp(x_new, x_old, chunk_np).astype(np.float32)
                chunk = (chunk_np * 32768.0).astype(np.int16)

            if mode == "push_to_talk":
                is_active = self._ptt_held
            else:
                is_active = self._voice_active

            if not is_active:
                continue

            is_voice = self._vad.feed(chunk_np) if self._vad else False

            # Log VAD detection during TTS for debugging
            if is_voice and self._speaking:
                logging.getLogger("saint.voice").debug(
                    f"voice.vad.detected session_id={self._speech_session_id} "
                    f"speaking={self._speaking} tts_playback={self.is_tts_playback_active()}"
                )

            # Barge-in: user spoke during TTS playback — immediate interrupt
            if self._speaking and is_voice:
                now = time.perf_counter()
                time_since_last = now - self._last_interrupt_time
                if time_since_last >= self._interrupt_debounce_sec:
                    self._last_interrupt_time = now
                    self._pending_barge_in = True
                    event_bus.emit_event(EventType.VOICE_INTERRUPT, {})
                    logging.getLogger("saint.voice").info(
                        f"voice.interruption.detected session_id={self._speech_session_id} "
                        f"reason=barge_in"
                    )
                    # Do NOT transcribe during TTS — barge-in handles it via interrupt
                    continue

            if is_voice:
                if not in_speech:
                    in_speech = True
                    silence_count = 0
                    speech_frame_count = 0
                    with self._speech_id_lock:
                        self._speech_session_id += 1
                        sid = self._speech_session_id
                    event_bus.emit_event(EventType.VOICE_SPEECH_START, {"session_id": sid})
                speech_frames.append(chunk)
                speech_frame_count += 1
                silence_count = 0
            else:
                if in_speech:
                    silence_count += 1
                    speech_frames.append(chunk)
                    if silence_count >= silence_frames:
                        in_speech = False
                        # During TTS playback, skip Whisper — run validation only
                        # and only transcribe if TTS is no longer active
                        is_tts_active = self.is_tts_playback_active()
                        if is_tts_active:
                            self._emit_vad_reject(sid, speech_frame_count, len(speech_frames), speech_frames)
                            event_bus.emit_event(EventType.VOICE_STT_SKIP, {
                                "session_id": sid,
                                "reason": "tts_playback_active",
                                "speech_frames": speech_frame_count,
                                "total_frames": len(speech_frames),
                            })
                            speech_frames = []
                            silence_count = 0
                            speech_frame_count = 0
                            continue
                        # Normal validation + transcription
                        if self._validate_speech_session(
                            speech_frames, speech_frame_count, min_speech_frames, min_speech_rms
                        ):
                            self._transcribe(speech_frames, sid)
                        else:
                            self._emit_vad_reject(sid, speech_frame_count, len(speech_frames), speech_frames)
                        speech_frames = []
                        silence_count = 0
                        speech_frame_count = 0

        # Flush remaining audio queue
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break

    def _validate_speech_session(
        self, frames: list, speech_frame_count: int, min_speech_frames: int, min_speech_rms: float
    ) -> bool:
        """
        Validate that a speech session contains meaningful speech before sending to STT.
        
        Checks:
        - Minimum number of speech frames (VAD-positive frames)
        - Minimum overall RMS energy
        - Speech ratio (speech frames / total frames)
        """
        if not frames:
            return False
        
        total_frames = len(frames)
        if total_frames == 0:
            return False
            
        # Check minimum speech frames
        if speech_frame_count < min_speech_frames:
            return False
            
        # Check speech ratio - at least 10% of frames should be speech
        speech_ratio = speech_frame_count / total_frames
        if speech_ratio < 0.1:
            return False
            
        # Check overall RMS energy
        audio = np.concatenate(frames)
        rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2) / 32768.0 ** 2))
        if rms < min_speech_rms:
            return False
            
        return True

    def _emit_vad_reject(self, session_id: int, speech_frames: int, total_frames: int, frames: list = None):
        """Log when a speech session is rejected by VAD validation."""
        rms = 0.0
        if frames:
            audio = np.concatenate(frames)
            if len(audio) > 0:
                rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2) / (32768.0 ** 2)))
        
        speech_ratio = speech_frames / total_frames if total_frames > 0 else 0.0
        
        event_bus.emit_event(EventType.VOICE_STT_DEBUG, {
            "trace": (
                f"VAD REJECT session_id={session_id} "
                f"speech_frames={speech_frames} total_frames={total_frames} "
                f"speech_ratio={speech_ratio:.3f} rms={rms:.6f}"
            ),
        })
        event_bus.emit_event(EventType.VOICE_STT_SKIP, {
            "session_id": session_id,
            "reason": "insufficient_speech",
            "speech_frames": speech_frames,
            "total_frames": total_frames,
            "speech_ratio": round(speech_ratio, 3),
            "rms": round(rms, 6),
        })

    def _trim_silence(self, frames: list, silence_frames: int) -> list:
        """
        Trim leading/trailing silence frames from captured audio.
        Keeps the silence_frames that were used for VAD end detection.
        """
        if not frames or len(frames) <= silence_frames * 2:
            return frames
            
        # Convert to numpy for analysis
        audio_chunks = [f.astype(np.float32) / 32768.0 for f in frames]
        
        # Find first and last speech frames using VAD
        first_speech_idx = 0
        last_speech_idx = len(frames) - 1
        
        # Reset VAD for analysis
        if self._vad:
            self._vad.reset()
            
        # Find first speech
        for i, chunk in enumerate(audio_chunks):
            if self._vad and self._vad.feed(chunk):
                first_speech_idx = i
                break
                
        # Find last speech (search backwards)
        if self._vad:
            self._vad.reset()
        for i in range(len(audio_chunks) - 1, -1, -1):
            if self._vad and self._vad.feed(audio_chunks[i]):
                last_speech_idx = i
                break
                
        # Add some padding around speech (keep some context)
        pad_frames = min(silence_frames, 3)  # ~90ms padding
        start_idx = max(0, first_speech_idx - pad_frames)
        end_idx = min(len(frames) - 1, last_speech_idx + pad_frames)
        
        if start_idx >= end_idx:
            return frames
            
        return frames[start_idx:end_idx + 1]

    def _transcribe(self, frames, session_id: int = 0):
        """Run STT transcription in a background thread (non-blocking)."""
        if not frames or self._stt is None:
            self._emit_stt_error(session_id, "empty_audio_buffer")
            return

        t0_total = time.perf_counter()

        def _run():
            try:
                self._stt_worker_func(frames, session_id, t0_total)
            except Exception as e:
                self._emit_stt_error(session_id, str(e))

        self._stt_worker = threading.Thread(
            target=_run, daemon=True, name=f"stt-{session_id}"
        )
        self._stt_worker.start()

    def _stt_worker_func(self, frames: list, session_id: int, t0_total: float):
        """Background STT worker — runs Whisper without blocking the voice loop."""
        # Bail out if TTS playback started while we were queued
        if self.is_tts_playback_active():
            self._emit_stt_error(session_id, "tts_playback_active_during_stt")
            event_bus.emit_event(EventType.VOICE_STT_SKIP, {
                "session_id": session_id,
                "reason": "tts_playback_active",
            })
            return

        # Trim leading/trailing silence before transcription
        silence_ms = config.get("voice.silence_duration_ms", 400)
        silence_frames = int(silence_ms / CHUNK_MS)
        trimmed = self._trim_silence(frames, silence_frames)
        if not trimmed:
            self._emit_stt_error(session_id, "empty_after_trim")
            return

        with self._stt_session_state_lock:
            self._stt_session_state[session_id] = STT_STATE_TRANSCRIBING

        if self._stt_cancelled.is_set():
            with self._stt_session_state_lock:
                self._stt_session_state[session_id] = STT_STATE_SUBMITTED
            self._emit_stt_error(session_id, "cancelled_by_stop_listening")
            return

        if not self._voice_active:
            with self._stt_session_state_lock:
                self._stt_session_state[session_id] = STT_STATE_SUBMITTED
            self._emit_stt_error(session_id, "voice_inactive")
            return

        t0 = time.perf_counter()
        audio = np.concatenate(trimmed)
        t1 = time.perf_counter()
        queue_ms = (t1 - t0) * 1000

        audio_duration_ms = round(len(audio) / SAMPLE_RATE * 1000, 1)
        total_samples = len(audio)
        chunks = len(trimmed)
        sample_rate = SAMPLE_RATE
        channels = CHANNELS
        rms = round(float(np.sqrt(np.mean(audio.astype(np.float32) ** 2) / (32768.0 ** 2))), 6)

        self._emit_stt_buffer(session_id, chunks, total_samples, audio_duration_ms, sample_rate, channels)

        if total_samples == 0:
            with self._stt_session_state_lock:
                self._stt_session_state[session_id] = STT_STATE_SUBMITTED
            self._emit_stt_error(session_id, "empty_audio_buffer")
            return

        self._emit_stt_submit(session_id)

        try:
            self._emit_stt_inference_start(session_id)
            result = self._stt.transcribe(audio, sample_rate=SAMPLE_RATE)
            self._emit_stt_inference_end(session_id)
        except Exception as e:
            self._emit_stt_error(session_id, str(e))
            with self._stt_session_state_lock:
                self._stt_session_state[session_id] = STT_STATE_SUBMITTED
            return

        t2 = time.perf_counter()
        inference_ms = (t2 - t1) * 1000
        total_ms = (t2 - t0_total) * 1000

        if result is None:
            self._emit_stt_error(session_id, "stt_returned_none")
            with self._stt_session_state_lock:
                self._stt_session_state[session_id] = STT_STATE_SUBMITTED
            return

        result_text = getattr(result, "text", "") or ""
        result_confidence = getattr(result, "confidence", 0.0) or 0.0

        self._emit_stt_result(session_id, result_text, result_confidence, inference_ms, total_ms)

        event_bus.emit_event(EventType.VOICE_SPEECH_END, {
            "session_id": session_id,
        })

        # Reject empty or whitespace-only results
        if not result_text.strip():
            self._emit_stt_error(session_id, "empty_transcription_result")
            with self._stt_session_state_lock:
                self._stt_session_state[session_id] = STT_STATE_SUBMITTED
            return

        # Reject punctuation-only transcripts (hallucinations from silence)
        stripped = result_text.strip()
        if self._is_punctuation_only(stripped):
            self._emit_stt_error(session_id, "punctuation_only_transcript")
            event_bus.emit_event(EventType.VOICE_STT_SKIP, {
                "session_id": session_id,
                "reason": "punctuation_only",
                "text": stripped,
            })
            with self._stt_session_state_lock:
                self._stt_session_state[session_id] = STT_STATE_SUBMITTED
            return

        event_bus.emit_event(EventType.VOICE_STT_FINAL, {
            "text": result_text,
            "confidence": result_confidence,
            "latency_ms": round(total_ms, 1),
            "inference_ms": round(inference_ms, 1),
            "session_id": session_id,
            "diagnostics": {
                "sample_rate": SAMPLE_RATE,
                "channels": CHANNELS,
                "audio_duration_ms": audio_duration_ms,
                "audio_rms": rms,
                "model": getattr(self._stt, '_model_name', 'unknown'),
                "device": getattr(self._stt, '_device', 'unknown'),
                "queue_ms": round(queue_ms, 1),
                "inference_ms": round(inference_ms, 1),
                "total_stt_ms": round(total_ms, 1),
                "num_chunks": chunks,
                "total_samples": total_samples,
            },
        })
        event_bus.emit_event(EventType.LATENCY_STT, {"ms": round(total_ms, 1)})

        with self._stt_session_state_lock:
            self._stt_session_state[session_id] = STT_STATE_COMPLETED

    def _is_punctuation_only(self, text: str) -> bool:
        """Check if text consists only of punctuation and whitespace."""
        if not text:
            return True
        # Remove all punctuation and whitespace, check if anything remains
        import string
        for char in text:
            if char not in string.punctuation and not char.isspace():
                return False
        return True

    def _emit_stt_submit(self, session_id: int):
        event_bus.emit_event(EventType.VOICE_STT_DEBUG, {
            "trace": f"STT SUBMIT session_id={session_id}",
        })

    def _emit_stt_buffer(self, session_id, chunks, samples, duration_ms, sr, ch):
        event_bus.emit_event(EventType.VOICE_STT_DEBUG, {
            "trace": (
                f"STT BUFFER session_id={session_id} "
                f"chunks={chunks} samples={samples} duration_ms={duration_ms} "
                f"sample_rate={sr} channels={ch}"
            ),
        })

    def _emit_stt_inference_start(self, session_id: int):
        event_bus.emit_event(EventType.VOICE_STT_DEBUG, {
            "trace": f"STT INFERENCE START session_id={session_id}",
        })

    def _emit_stt_inference_end(self, session_id: int):
        event_bus.emit_event(EventType.VOICE_STT_DEBUG, {
            "trace": f"STT INFERENCE END session_id={session_id}",
        })

    def _emit_stt_result(self, session_id, text, confidence, inference_ms, total_ms):
        event_bus.emit_event(EventType.VOICE_STT_DEBUG, {
            "trace": (
                f"STT RESULT session_id={session_id} text=\"{text}\" "
                f"confidence={confidence} inference_ms={round(inference_ms,1)} "
                f"total_ms={round(total_ms,1)}"
            ),
        })

    def _emit_stt_error(self, session_id: int, error: str):
        event_bus.emit_event(EventType.VOICE_STT_ERROR, {
            "error": error, "session_id": session_id,
        })

    # ------------------------------------------------------------------ #
    # Mock injection
    # ------------------------------------------------------------------ #
    def inject_utterance(self, text: str, confidence: float = 0.95):
        import random
        session_id = random.randint(1, 1_000_000)
        event_bus.emit_event(EventType.VOICE_STT_FINAL, {
            "text": text,
            "confidence": confidence,
            "latency_ms": 0.0,
            "session_id": session_id,
        })

    def inject_interrupt(self):
        event_bus.emit_event(EventType.VOICE_INTERRUPT, {})

"""
modules/voice/tts_service.py

Persistent TTS Service - SINGLETON architecture for reliable TTS.

This module provides a thread-safe, singleton TTS service that:
- Loads the model ONCE and keeps it in GPU memory
- Manages a proper state machine (UNINITIALIZED -> LOADING -> READY -> etc.)
- Provides warm-up during initialization
- Handles cancellation safely with turn IDs
- Filters control tokens like [silence]
- Provides audio diagnostics

State Machine:
  UNINITIALIZED -> LOADING -> READY -> SYTHESIZING -> PLAYING -> READY
                   |           |          |
                   v           v          v
                 ERROR      ERROR     STOPPING -> READY
"""

import threading
import time
import hashlib
from enum import Enum, auto
from typing import Optional, Callable, Tuple, List, Dict, Any
from dataclasses import dataclass, field
import logging

import numpy as np

from core.events import event_bus, EventType
from core.config import config

logger = logging.getLogger("saint.tts_service")


class TTSState(Enum):
    UNINITIALIZED = auto()
    LOADING = auto()
    READY = auto()
    SYNTHESIZING = auto()
    PLAYING = auto()
    STOPPING = auto()
    ERROR = auto()
    SHUTDOWN = auto()


@dataclass
class TTSRequest:
    """A single TTS synthesis request with tracking metadata."""
    request_id: str
    turn_id: int
    text: str
    sequence_id: int = 0
    timestamp: float = field(default_factory=time.perf_counter)

    def __hash__(self):
        return hash(self.request_id)


@dataclass
class AudioDiagnostics:
    """Audio quality diagnostics for generated audio."""
    sample_rate: int = 0
    channels: int = 1
    dtype: str = "int16"
    duration_ms: float = 0.0
    min_val: float = 0.0
    max_val: float = 0.0
    rms: float = 0.0
    peak: float = 0.0
    clipping_detected: bool = False
    is_valid: bool = True
    reject_reason: str = ""


# Control tokens that should NEVER be synthesized
CONTROL_TOKENS = {
    "[silence]", "[SILENCE]", "<silence>", "[SILENT]", "<SILENT>",
    "[pause]", "[PAUSE]", "<pause>",
    "[interrupted]", "[INTERRUPTED]",
    "...", "…",  # Ellipsis
    "[thinking]", "[THINKING]",
    "[action]", "[ACTION]",
    "[error]", "[ERROR]",
}


def is_control_token(text: str) -> bool:
    """Check if text is a control token that should not be synthesized."""
    stripped = text.strip()
    if not stripped:
        return True
    if stripped in CONTROL_TOKENS:
        return True
    # Check for bracket patterns like [anything]
    if stripped.startswith("[") and stripped.endswith("]"):
        return True
    if stripped.startswith("<") and stripped.endswith(">"):
        return True
    # Check for whitespace-only
    if not stripped or stripped.isspace():
        return True
    return False


def should_skip_text(text: str) -> Tuple[bool, str]:
    """
    Determine if text should be skipped before TTS.
    Returns (should_skip, reason).
    """
    stripped = text.strip()

    if not stripped:
        return True, "empty_text"

    if stripped.isspace():
        return True, "whitespace_only"

    if stripped in CONTROL_TOKENS:
        return True, f"control_token:{stripped}"

    # Check for bracket patterns
    if stripped.startswith("[") and stripped.endswith("]"):
        return True, f"bracket_token:{stripped}"

    if stripped.startswith("<") and stripped.endswith(">"):
        return True, f"angle_token:{stripped}"

    return False, ""


def analyze_audio(samples: np.ndarray, sample_rate: int) -> AudioDiagnostics:
    """Analyze audio buffer for quality issues."""
    diag = AudioDiagnostics()
    diag.sample_rate = sample_rate
    diag.channels = 1 if samples.ndim == 1 else samples.shape[0]

    if len(samples) == 0:
        diag.is_valid = False
        diag.reject_reason = "empty_audio"
        return diag

    diag.duration_ms = len(samples) / sample_rate * 1000 if sample_rate > 0 else 0

    # Compute statistics on float values
    float_samples = samples.astype(np.float64) / 32768.0 if samples.dtype == np.int16 else samples.astype(np.float64)

    diag.min_val = float(np.min(float_samples))
    diag.max_val = float(np.max(float_samples))
    diag.rms = float(np.sqrt(np.mean(float_samples ** 2)))
    diag.peak = max(abs(diag.min_val), abs(diag.max_val))

    # Check for clipping
    if diag.peak >= 0.99:
        diag.clipping_detected = True

    # Check for invalid audio (all zeros, NaN, Inf)
    if np.any(np.isnan(float_samples)) or np.any(np.isinf(float_samples)):
        diag.is_valid = False
        diag.reject_reason = "nan_or_inf"
    elif diag.rms < 1e-6:
        diag.is_valid = False
        diag.reject_reason = "silent_audio"
    elif diag.peak > 10.0:  # Values way out of range
        diag.is_valid = False
        diag.reject_reason = "invalid_range"

    return diag


class TTSService:
    """
    Singleton TTS service with proper lifecycle management.

    This is THE authoritative TTS interface for SAINT.
    Use get_tts_service() to obtain the singleton instance.
    """

    _instance: Optional['TTSService'] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # Prevent re-initialization
        if hasattr(self, '_initialized') and self._initialized:
            return
        self._initialized = True

        # State machine
        self._state = TTSState.UNINITIALIZED
        self._state_lock = threading.RLock()

        # Backend engine (QwenTTS, KokoroTTS, MockTTS)
        self._engine = None
        self._engine_type = ""

        # Request tracking
        self._current_request: Optional[TTSRequest] = None
        self._request_lock = threading.Lock()
        self._request_counter = 0
        self._seen_requests: Dict[str, float] = {}  # request_hash -> timestamp
        self._seen_requests_lock = threading.Lock()

        # Turn tracking for safe cancellation
        self._active_turn_id: int = -1
        self._turn_lock = threading.Lock()

        # Playback state
        self._is_speaking = False
        self._speaking_lock = threading.Lock()

        # Interrupt signal
        self._interrupt_event = threading.Event()

        # Diagnostics
        self._last_diagnostics: Dict[str, Any] = {}
        self._total_synthesis_time = 0.0
        self._total_audio_generated = 0.0
        self._request_count = 0

        # Loading state
        self._load_error: Optional[Exception] = None
        self._warmup_done = False

        # Background initialization
        self._init_thread: Optional[threading.Thread] = None

        logger.info("TTSService singleton created")

    @property
    def state(self) -> TTSState:
        with self._state_lock:
            return self._state

    def _set_state(self, new_state: TTSState):
        with self._state_lock:
            old_state = self._state
            self._state = new_state
        logger.debug(f"TTS state: {old_state.name} -> {new_state.name}")
        event_bus.emit_event(EventType.TTS_STATE_CHANGE, {
            "old_state": old_state.name,
            "new_state": new_state.name,
        })

    @property
    def is_ready(self) -> bool:
        return self.state == TTSState.READY

    def is_speaking(self) -> bool:
        with self._speaking_lock:
            return self._is_speaking

    def get_diagnostics(self) -> Dict[str, Any]:
        """Get TTS diagnostics for UI display."""
        return {
            "state": self.state.name,
            "engine_type": self._engine_type,
            "is_ready": self.is_ready,
            "is_speaking": self.is_speaking(),
            "active_turn_id": self._active_turn_id,
            "current_request": self._current_request.request_id if self._current_request else None,
            "request_count": self._request_count,
            "total_synthesis_ms": round(self._total_synthesis_time * 1000, 1),
            "total_audio_ms": round(self._total_audio_generated * 1000, 1),
            "avg_rtf": round(self._total_synthesis_time / max(self._total_audio_generated, 0.001), 3) if self._total_audio_generated > 0 else 0,
            "load_error": str(self._load_error) if self._load_error else None,
            "warmup_done": self._warmup_done,
        }

    # ------------------------------------------------------------------ #
    # Initialization
    # ------------------------------------------------------------------ #

    def initialize(self, backend: str = None, blocking: bool = False, **kwargs) -> bool:
        """
        Initialize the TTS service.

        Args:
            backend: TTS backend ("qwen", "kokoro", "mock")
            blocking: If True, wait for initialization to complete
            **kwargs: Backend-specific options

        Returns:
            True if initialization started successfully
        """
        with self._state_lock:
            if self._state not in (TTSState.UNINITIALIZED, TTSState.ERROR):
                logger.debug(f"TTS already initialized (state={self._state.name})")
                return True

        backend = backend or config.get("voice.tts_backend", "kokoro")

        # Store kwargs for lazy initialization
        self._backend_config = {
            "backend": backend,
            **kwargs
        }

        if blocking:
            return self._do_initialize()
        else:
            # Start background initialization
            self._init_thread = threading.Thread(
                target=self._do_initialize,
                daemon=True,
                name="tts-init"
            )
            self._init_thread.start()
            return True

    def _do_initialize(self) -> bool:
        """Perform actual initialization (loads model)."""
        self._set_state(TTSState.LOADING)

        try:
            backend = self._backend_config.get("backend", "mock")
            kwargs = {k: v for k, v in self._backend_config.items() if k != "backend"}

            logger.info(f"Initializing TTS backend: {backend}")

            if backend == "qwen":
                from modules.voice.tts import QwenTTS
                self._engine = QwenTTS(**kwargs)
                self._engine_type = "qwen"
            elif backend == "kokoro":
                from modules.voice.tts import KokoroTTS
                self._engine = KokoroTTS(**kwargs)
                self._engine_type = "kokoro"
            else:
                from modules.voice.tts import MockTTS
                self._engine = MockTTS()
                self._engine_type = "mock"

            # Trigger lazy loading of the model
            if hasattr(self._engine, '_load'):
                self._engine._load()

            self._load_error = None
            self._set_state(TTSState.READY)

            # Perform warmup
            self._do_warmup()

            logger.info(f"TTS service ready: {self._engine_type}")
            return True

        except Exception as e:
            self._load_error = e
            self._set_state(TTSState.ERROR)
            logger.error(f"TTS initialization failed: {e}")
            event_bus.emit_event(EventType.TTS_ERROR, {
                "error": f"TTS initialization failed: {e}",
                "phase": "initialization",
            })
            return False

    def _do_warmup(self):
        """Perform warmup synthesis to preload kernels."""
        if self._warmup_done:
            return

        if self._engine is None:
            return

        try:
            logger.debug("Performing TTS warmup...")
            warmup_start = time.perf_counter()

            # Warmup with short text - discard audio
            self._engine._interrupt_event.clear()

            # For Qwen, we need to trigger model inference
            if hasattr(self._engine, '_generate_word'):
                try:
                    wavs, sr = self._engine._generate_word(".")
                    logger.debug(f"Warmup generated {len(wavs)} audio chunks")
                except Exception as e:
                    logger.warning(f"Warmup generation warning: {e}")
            elif hasattr(self._engine, 'warm_up'):
                self._engine.warm_up()

            warmup_ms = (time.perf_counter() - warmup_start) * 1000
            self._warmup_done = True

            logger.info(f"TTS warmup complete in {warmup_ms:.0f}ms")

            event_bus.emit_event(EventType.TTS_WARMUP_COMPLETE, {
                "warmup_ms": round(warmup_ms, 1),
                "engine": self._engine_type,
            })

        except Exception as e:
            logger.warning(f"TTS warmup failed (non-fatal): {e}")
            self._warmup_done = True  # Don't retry warmup

    def warmup(self):
        """Public warmup API - idempotent."""
        self._do_warmup()

    # ------------------------------------------------------------------ #
    # Synthesis
    # ------------------------------------------------------------------ #

    def synthesize(
        self,
        text: str,
        turn_id: int = 0,
        on_chunk_start: Optional[Callable[[str], None]] = None,
        request_id: str = None,
    ) -> bool:
        """
        Synthesize and play text.

        Args:
            text: Text to synthesize
            turn_id: Conversation turn ID for cancellation
            on_chunk_start: Callback when each chunk starts playing
            request_id: Optional request ID for deduplication

        Returns:
            True if synthesis completed, False if skipped or interrupted
        """
        # Check if text should be skipped
        should_skip, skip_reason = should_skip_text(text)
        if should_skip:
            logger.debug(f"Skipping TTS for control text: {skip_reason}")
            event_bus.emit_event(EventType.TTS_SKIPPED, {
                "reason": skip_reason,
                "text_preview": text[:50],
                "turn_id": turn_id,
            })
            return False

        # Check state - wait for model if still loading
        state = self.state
        if state == TTSState.SHUTDOWN:
            logger.warning("TTS service is shut down")
            return False
        if state == TTSState.ERROR:
            logger.warning("TTS service in error state")
            return False
        if state == TTSState.LOADING:
            # Wait for model to finish loading (up to 30 seconds)
            logger.info("TTS model loading, waiting for readiness...")
            wait_start = time.perf_counter()
            max_wait = 30.0
            while self.state == TTSState.LOADING:
                time.sleep(0.1)
                if time.perf_counter() - wait_start > max_wait:
                    logger.error("TTS model failed to load within 30 seconds")
                    return False
            # Re-check state after waiting
            state = self.state
            if state != TTSState.READY:
                logger.warning(f"TTS not ready after loading (state={state.name})")
                return False
            logger.info(f"TTS ready after {time.perf_counter() - wait_start:.1f}s wait")

        # Deduplication check
        if request_id:
            request_hash = hashlib.md5(f"{request_id}:{text}".encode()).hexdigest()
            with self._seen_requests_lock:
                if request_hash in self._seen_requests:
                    logger.debug(f"Duplicate TTS request: {request_id}")
                    event_bus.emit_event(EventType.TTS_DUPLICATE, {
                        "request_id": request_id,
                        "turn_id": turn_id,
                    })
                    return False
                # Clean old entries (keep last 100)
                if len(self._seen_requests) > 100:
                    oldest = sorted(self._seen_requests.items(), key=lambda x: x[1])[:50]
                    for k, _ in oldest:
                        del self._seen_requests[k]
                self._seen_requests[request_hash] = time.perf_counter()

        # Create request
        with self._request_lock:
            self._request_counter += 1
            if not request_id:
                request_id = f"tts_{turn_id}_{self._request_counter}"

            request = TTSRequest(
                request_id=request_id,
                turn_id=turn_id,
                text=text,
                sequence_id=self._request_counter,
            )
            self._current_request = request

        # Update turn tracking
        with self._turn_lock:
            self._active_turn_id = turn_id

        # Mark as speaking
        with self._speaking_lock:
            self._is_speaking = True

        # Clear interrupt flag
        self._interrupt_event.clear()

        # Update state
        self._set_state(TTSState.SYNTHESIZING)

        # Emit request event
        event_bus.emit_event(EventType.TTS_REQUEST, {
            "request_id": request_id,
            "turn_id": turn_id,
            "text_length": len(text),
            "text_preview": text[:100],
            "sequence_id": request.sequence_id,
        })

        synthesis_start = time.perf_counter()

        try:
            # Call engine's speak method
            # The engine handles word-by-word generation and playback
            if self._engine is None:
                logger.error("TTS engine is None")
                return False

            self._engine.speak(text, on_chunk_start=on_chunk_start)

            synthesis_time = time.perf_counter() - synthesis_start

            # Update stats
            self._total_synthesis_time += synthesis_time
            self._request_count += 1

            was_interrupted = self._interrupt_event.is_set()

            if was_interrupted:
                self._set_state(TTSState.STOPPING)
                event_bus.emit_event(EventType.TTS_INTERRUPTED, {
                    "request_id": request_id,
                    "turn_id": turn_id,
                })
            else:
                # Estimate audio duration (rough approximation)
                # Real duration tracking would need callback from engine
                estimated_audio = len(text.split()) * 0.3  # ~300ms per word avg
                self._total_audio_generated += estimated_audio

            # Return to ready state
            with self._state_lock:
                if self._state not in (TTSState.SHUTDOWN, TTSState.ERROR):
                    self._set_state(TTSState.READY)

            return not was_interrupted

        except Exception as e:
            logger.error(f"TTS synthesis error: {e}")
            event_bus.emit_event(EventType.TTS_ERROR, {
                "error": str(e),
                "request_id": request_id,
                "turn_id": turn_id,
            })
            # Don't set to ERROR state for synthesis failures, just log
            with self._state_lock:
                if self._state not in (TTSState.SHUTDOWN, TTSState.ERROR):
                    self._set_state(TTSState.READY)
            return False

        finally:
            with self._speaking_lock:
                self._is_speaking = False
            with self._request_lock:
                self._current_request = None

    def interrupt(self, turn_id: int = None):
        """
        Interrupt current synthesis.

        Args:
            turn_id: If provided, only interrupt if this matches active turn
        """
        with self._turn_lock:
            if turn_id is not None and self._active_turn_id != turn_id:
                logger.debug(f"Ignoring interrupt for old turn {turn_id} (active: {self._active_turn_id})")
                return
            self._active_turn_id = -1

        self._interrupt_event.set()

        if self._engine and hasattr(self._engine, 'interrupt'):
            self._engine.interrupt()

        # Stop sounddevice playback
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass

        logger.debug("TTS interrupted")

    def stop(self):
        """Stop TTS and return to ready state."""
        self.interrupt()
        self._set_state(TTSState.STOPPING)

        # Wait briefly for playback to stop
        time.sleep(0.05)

        with self._state_lock:
            if self._state not in (TTSState.SHUTDOWN, TTSState.ERROR):
                self._set_state(TTSState.READY)

    def speak(self, text: str, on_chunk_start: Optional[Callable[[str], None]] = None):
        """
        Compatibility method for ConversationController.

        This wraps synthesize() to match the TTSEngine.speak() interface.
        """
        # Extract turn_id from current request if available
        turn_id = 0
        with self._turn_lock:
            turn_id = self._active_turn_id

        return self.synthesize(
            text=text,
            turn_id=turn_id,
            on_chunk_start=on_chunk_start,
        )

    def shutdown(self):
        """Full shutdown - release all resources."""
        logger.info("Shutting down TTS service")

        self._set_state(TTSState.SHUTDOWN)
        self.interrupt()

        # Release engine reference
        self._engine = None

        # Clear singleton
        with TTSService._lock:
            if TTSService._instance is self:
                TTSService._instance = None


# Singleton accessor
_tts_service: Optional[TTSService] = None
_tts_service_lock = threading.Lock()


def get_tts_service() -> TTSService:
    """Get the TTS service singleton."""
    global _tts_service
    with _tts_service_lock:
        if _tts_service is None:
            _tts_service = TTSService()
        return _tts_service


def reset_tts_service():
    """Reset the TTS service singleton (for testing)."""
    global _tts_service
    with _tts_service_lock:
        if _tts_service is not None:
            _tts_service.shutdown()
        _tts_service = None

"""
modules/voice/voice_state.py

Voice State Machine - Single authoritative voice pipeline state.

This module provides explicit state management for the voice pipeline:
- Prevents race conditions between listening, thinking, speaking
- Debounces interrupt events
- Manages turn lifecycle with explicit IDs
- Coordinates between VoiceModule, ConversationController, and TTS

State Machine:
  IDLE -> LISTENING -> THINKING -> SPEAKING -> LISTENING
                |          |           |
                v          v           v
           INTERRUPTING <-+-----------+
                |
                v
           LISTENING

Only ONE state can be active at a time.
State transitions are atomic and logged.
"""

import threading
import time
from enum import Enum, auto
from typing import Optional, Callable, Dict, Any
from dataclasses import dataclass, field
import logging

from core.events import event_bus, EventType

logger = logging.getLogger("saint.voice_state")


class VoiceState(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()
    INTERRUPTING = auto()
    STOPPING = auto()


@dataclass
class VoiceTurn:
    """Represents a single voice conversation turn."""
    turn_id: int
    session_id: int = 0
    user_text: str = ""
    assistant_text: str = ""
    start_time: float = field(default_factory=time.perf_counter)
    end_time: float = 0.0
    was_interrupted: bool = False
    stt_latency_ms: float = 0.0
    ai_first_token_ms: float = 0.0
    ai_total_ms: float = 0.0
    tts_latency_ms: float = 0.0


class VoiceStateMachine:
    """
    Singleton state machine for voice pipeline.

    Ensures only one activity can happen at a time and provides
    safe state transitions with logging.
    """

    _instance: Optional['VoiceStateMachine'] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, '_initialized') and self._initialized:
            return
        self._initialized = True

        # Current state
        self._state = VoiceState.IDLE
        self._state_lock = threading.RLock()
        self._state_changed_at = time.perf_counter()

        # Turn tracking
        self._turn_counter = 0
        self._turn_lock = threading.Lock()
        self._active_turn: Optional[VoiceTurn] = None
        self._turn_history: list = []  # Last N turns for debugging

        # Interrupt debouncing
        self._last_interrupt_time = 0.0
        self._interrupt_debounce_ms = 500  # Minimum time between interrupts
        self._interrupt_count = 0
        self._interrupt_processed = threading.Event()
        self._interrupt_processed.set()  # Start as "no pending interrupt"

        # Listening state
        self._listening_session_id = 0
        self._listening_session_active = False

        # Callbacks for state transitions
        self._on_state_change_callbacks: list = []
        self._on_interrupt_callbacks: list = []

        logger.info("VoiceStateMachine initialized")

    @property
    def state(self) -> VoiceState:
        with self._state_lock:
            return self._state

    @property
    def state_name(self) -> str:
        return self.state.name

    @property
    def active_turn(self) -> Optional[VoiceTurn]:
        with self._turn_lock:
            return self._active_turn

    @property
    def active_turn_id(self) -> int:
        with self._turn_lock:
            return self._active_turn.turn_id if self._active_turn else -1

    @property
    def is_speaking(self) -> bool:
        return self.state == VoiceState.SPEAKING

    @property
    def is_listening(self) -> bool:
        return self.state == VoiceState.LISTENING

    @property
    def is_busy(self) -> bool:
        return self.state in (VoiceState.THINKING, VoiceState.SPEAKING, VoiceState.INTERRUPTING)

    def get_diagnostics(self) -> Dict[str, Any]:
        """Get voice state diagnostics."""
        with self._state_lock:
            state_duration_ms = (time.perf_counter() - self._state_changed_at) * 1000

        return {
            "state": self.state_name,
            "state_duration_ms": round(state_duration_ms, 1),
            "active_turn_id": self.active_turn_id,
            "listening_session_id": self._listening_session_id,
            "interrupt_count": self._interrupt_count,
            "is_busy": self.is_busy,
        }

    # ------------------------------------------------------------------ #
    # State Transitions
    # ------------------------------------------------------------------ #

    def transition_to(self, new_state: VoiceState, reason: str = "") -> bool:
        """
        Attempt to transition to a new state.

        Returns True if transition was allowed, False otherwise.
        """
        with self._state_lock:
            old_state = self._state

            # Validate transition
            if not self._is_valid_transition(old_state, new_state):
                logger.warning(
                    f"Invalid state transition: {old_state.name} -> {new_state.name} ({reason})"
                )
                return False

            # Perform transition
            self._state = new_state
            self._state_changed_at = time.perf_counter()

            logger.info(f"Voice state: {old_state.name} -> {new_state.name} ({reason})")

            # Emit event
            event_bus.emit_event(EventType.VOICE_STATE_CHANGE, {
                "old_state": old_state.name,
                "new_state": new_state.name,
                "reason": reason,
                "turn_id": self.active_turn_id,
            })

        # Call callbacks outside lock
        self._call_state_change_callbacks(old_state, new_state, reason)

        return True

    def _is_valid_transition(self, from_state: VoiceState, to_state: VoiceState) -> bool:
        """Check if a state transition is valid."""
        # Define valid transitions
        valid_transitions = {
            VoiceState.IDLE: {VoiceState.LISTENING, VoiceState.STOPPING},
            VoiceState.LISTENING: {VoiceState.THINKING, VoiceState.IDLE, VoiceState.STOPPING},
            VoiceState.THINKING: {VoiceState.SPEAKING, VoiceState.INTERRUPTING, VoiceState.IDLE, VoiceState.STOPPING},
            VoiceState.SPEAKING: {VoiceState.LISTENING, VoiceState.INTERRUPTING, VoiceState.IDLE, VoiceState.STOPPING},
            VoiceState.INTERRUPTING: {VoiceState.LISTENING, VoiceState.IDLE, VoiceState.STOPPING},
            VoiceState.STOPPING: {VoiceState.IDLE},
        }

        return to_state in valid_transitions.get(from_state, set())

    def _call_state_change_callbacks(self, old_state, new_state, reason):
        """Call registered state change callbacks."""
        for callback in self._on_state_change_callbacks:
            try:
                callback(old_state, new_state, reason)
            except Exception as e:
                logger.error(f"State change callback error: {e}")

    def register_state_callback(self, callback: Callable):
        """Register a callback for state changes."""
        self._on_state_change_callbacks.append(callback)

    # ------------------------------------------------------------------ #
    # Turn Management
    # ------------------------------------------------------------------ #

    def start_turn(self, user_text: str = "", session_id: int = 0) -> int:
        """
        Start a new conversation turn.

        Returns the turn_id.
        """
        with self._turn_lock:
            self._turn_counter += 1
            turn_id = self._turn_counter

            turn = VoiceTurn(
                turn_id=turn_id,
                session_id=session_id,
                user_text=user_text,
            )

            self._active_turn = turn

            # Add to history (keep last 10)
            self._turn_history.append(turn)
            if len(self._turn_history) > 10:
                self._turn_history = self._turn_history[-10:]

        logger.debug(f"Started turn {turn_id} (session={session_id})")

        event_bus.emit_event(EventType.VOICE_TURN_START, {
            "turn_id": turn_id,
            "session_id": session_id,
            "user_text": user_text[:50],
        })

        return turn_id

    def end_turn(self, assistant_text: str = "", was_interrupted: bool = False):
        """End the current turn."""
        with self._turn_lock:
            if self._active_turn is None:
                return

            self._active_turn.assistant_text = assistant_text
            self._active_turn.end_time = time.perf_counter()
            self._active_turn.was_interrupted = was_interrupted

            turn_id = self._active_turn.turn_id
            self._active_turn = None

        logger.debug(f"Ended turn {turn_id} (interrupted={was_interrupted})")

        event_bus.emit_event(EventType.VOICE_TURN_END, {
            "turn_id": turn_id,
            "was_interrupted": was_interrupted,
        })

    def invalidate_turn(self, turn_id: int):
        """Invalidate a specific turn (used for cancellation)."""
        with self._turn_lock:
            if self._active_turn and self._active_turn.turn_id == turn_id:
                self._active_turn.was_interrupted = True
                self._active_turn = None

        logger.debug(f"Invalidated turn {turn_id}")

    # ------------------------------------------------------------------ #
    # Interrupt Handling
    # ------------------------------------------------------------------ #

    def request_interrupt(self, source: str = "unknown") -> bool:
        """
        Request an interrupt.

        Implements debouncing to prevent multiple rapid interrupts.
        Returns True if interrupt was accepted, False if debounced.
        """
        now = time.perf_counter()
        debounce_sec = self._interrupt_debounce_ms / 1000.0

        # Check if we're in a state that can be interrupted
        current_state = self.state
        if current_state not in (VoiceState.THINKING, VoiceState.SPEAKING):
            logger.debug(f"Ignoring interrupt request in state {current_state.name}")
            return False

        # Debounce check
        time_since_last = now - self._last_interrupt_time
        if time_since_last < debounce_sec:
            logger.debug(
                f"Debounced interrupt ({time_since_last*1000:.0f}ms < {self._interrupt_debounce_ms}ms)"
            )
            return False

        # Check if previous interrupt is still being processed
        if not self._interrupt_processed.is_set():
            logger.debug("Previous interrupt still processing, skipping")
            return False

        # Accept interrupt
        self._last_interrupt_time = now
        self._interrupt_count += 1
        self._interrupt_processed.clear()

        logger.info(f"Interrupt requested (source={source}, count={self._interrupt_count})")

        # Transition to INTERRUPTING state
        self.transition_to(VoiceState.INTERRUPTING, f"interrupt:{source}")

        # Call interrupt callbacks
        for callback in self._on_interrupt_callbacks:
            try:
                callback(source)
            except Exception as e:
                logger.error(f"Interrupt callback error: {e}")

        return True

    def complete_interrupt(self):
        """Mark interrupt processing as complete."""
        self._interrupt_processed.set()

        # Transition to LISTENING
        self.transition_to(VoiceState.LISTENING, "interrupt_complete")

    def register_interrupt_callback(self, callback: Callable):
        """Register a callback for interrupts."""
        self._on_interrupt_callbacks.append(callback)

    # ------------------------------------------------------------------ #
    # Listening Management
    # ------------------------------------------------------------------ #

    def start_listening(self) -> int:
        """Start a listening session. Returns session_id."""
        if not self.transition_to(VoiceState.LISTENING, "user_request"):
            return -1

        self._listening_session_id += 1
        self._listening_session_active = True

        return self._listening_session_id

    def stop_listening(self):
        """Stop listening."""
        self._listening_session_active = False
        self.transition_to(VoiceState.IDLE, "stop_listening")

    def is_valid_listening_session(self, session_id: int) -> bool:
        """Check if a session ID is still valid."""
        return session_id == self._listening_session_id and self._listening_session_active

    # ------------------------------------------------------------------ #
    # High-level Operations
    # ------------------------------------------------------------------ #

    def begin_thinking(self, user_text: str, session_id: int = 0) -> int:
        """
        Begin thinking state. Returns turn_id or -1 if invalid state.
        """
        if not self.transition_to(VoiceState.THINKING, "stt_complete"):
            return -1

        return self.start_turn(user_text, session_id)

    def begin_speaking(self) -> bool:
        """Transition from THINKING to SPEAKING."""
        return self.transition_to(VoiceState.SPEAKING, "ai_response_ready")

    def end_speaking(self):
        """End speaking and return to LISTENING."""
        self.end_turn()
        self.transition_to(VoiceState.LISTENING, "tts_complete")

    def reset(self):
        """Reset to IDLE state."""
        with self._state_lock:
            self._state = VoiceState.IDLE
            self._state_changed_at = time.perf_counter()

        with self._turn_lock:
            self._active_turn = None

        self._interrupt_processed.set()
        self._listening_session_active = False

        logger.info("VoiceStateMachine reset to IDLE")


# Singleton accessor
_voice_state_machine: Optional[VoiceStateMachine] = None
_voice_state_lock = threading.Lock()


def get_voice_state() -> VoiceStateMachine:
    """Get the voice state machine singleton."""
    global _voice_state_machine
    with _voice_state_lock:
        if _voice_state_machine is None:
            _voice_state_machine = VoiceStateMachine()
        return _voice_state_machine

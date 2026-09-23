"""
core/assistant_state.py

The single, authoritative "what is SAINT doing right now" state.

    OFFLINE            microphone not running (text input still works)
    IDLE               running, not listening for anything
    WAKE_LISTENING     waiting for "SAINT" (only the wake-word model runs)
    WAKE_DETECTED      the wake word was just heard
    COMMAND_LISTENING  capturing the user's command
    LISTENING          always-on mode (wake word disabled): capturing speech
    PROCESSING         transcribing / routing / thinking
    EXECUTING          a tool is running (Spotify, desktop, automation...)
    SPEAKING           TTS is playing
    ERROR              a subsystem the voice loop depends on failed

The voice module, conversation controller and tool registry drive it; the UI
only renders it (via EventType.ASSISTANT_STATE). It never depends on Qt
widgets, so background operation works with the window hidden.
"""

import logging
import threading
import time
from enum import Enum

from core.events import event_bus, EventType

log = logging.getLogger("saint.state")


class AssistantState(str, Enum):
    OFFLINE = "offline"
    IDLE = "idle"
    WAKE_LISTENING = "wake_listening"
    WAKE_DETECTED = "wake_detected"
    COMMAND_LISTENING = "command_listening"
    LISTENING = "listening"
    PROCESSING = "processing"
    EXECUTING = "executing"
    SPEAKING = "speaking"
    ERROR = "error"


LABELS = {
    AssistantState.OFFLINE: "Microphone off",
    AssistantState.IDLE: "Idle",
    AssistantState.WAKE_LISTENING: "Listening for “Hey SAINT”",
    AssistantState.WAKE_DETECTED: "Wake word detected",
    AssistantState.COMMAND_LISTENING: "Listening",
    AssistantState.LISTENING: "Listening",
    AssistantState.PROCESSING: "Processing",
    AssistantState.EXECUTING: "Executing task",
    AssistantState.SPEAKING: "Speaking",
    AssistantState.ERROR: "Error",
}


class AssistantStateTracker:
    def __init__(self):
        self._lock = threading.RLock()
        self._state = AssistantState.OFFLINE
        self._detail = ""
        self._since = time.time()
        # What "at rest" means right now; set by the voice module.
        self._resting = AssistantState.OFFLINE
        self._active_tools = 0
        self._turn_active = False
        event_bus.subscribe(self._on_event)

    # ------------------------------------------------------------------ #
    @property
    def state(self) -> AssistantState:
        with self._lock:
            return self._state

    @property
    def detail(self) -> str:
        with self._lock:
            return self._detail

    @property
    def resting_state(self) -> AssistantState:
        with self._lock:
            return self._resting

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state.value,
                "label": LABELS[self._state],
                "detail": self._detail,
                "since": self._since,
                "resting": self._resting.value,
            }

    # ------------------------------------------------------------------ #
    def set(self, state: AssistantState, detail: str = "", **info):
        with self._lock:
            if state == self._state and detail == self._detail and not info:
                return
            previous = self._state
            self._state = state
            self._detail = detail
            self._since = time.time()
        log.debug("state %s -> %s %s", previous.value, state.value, detail)
        payload = {
            "state": state.value,
            "previous": previous.value,
            "label": LABELS[state],
            "detail": detail,
        }
        payload.update(info)
        event_bus.emit_event(EventType.ASSISTANT_STATE, payload)

    def set_resting(self, state: AssistantState, apply: bool = True, detail: str = ""):
        """Declare the state SAINT returns to when a turn finishes."""
        with self._lock:
            self._resting = state
            busy = self._state in (AssistantState.PROCESSING, AssistantState.EXECUTING,
                                   AssistantState.SPEAKING)
        if apply and not busy:
            self.set(state, detail)

    def begin_turn(self):
        with self._lock:
            self._turn_active = True
        self.set(AssistantState.PROCESSING)

    def end_turn(self):
        with self._lock:
            self._turn_active = False
        self.return_to_rest()

    def return_to_rest(self):
        with self._lock:
            resting = self._resting
        self.set(resting)

    # ------------------------------------------------------------------ #
    def _on_event(self, ev):
        # Tool execution is reported by the registry; reflect it so the UI
        # can show "Executing task" only while a tool is actually running.
        if ev.type == EventType.TOOL_STARTED:
            with self._lock:
                self._active_tools += 1
            self.set(AssistantState.EXECUTING, ev.payload.get("tool", ""))
        elif ev.type in (EventType.TOOL_COMPLETED, EventType.TOOL_FAILED):
            with self._lock:
                self._active_tools = max(0, self._active_tools - 1)
                still_running = self._active_tools > 0
                executing = self._state == AssistantState.EXECUTING
                in_turn = self._turn_active
            if executing and not still_running:
                if in_turn:
                    self.set(AssistantState.PROCESSING)
                else:
                    # Background work (e.g. a scheduled automation) finished.
                    self.return_to_rest()


assistant_state = AssistantStateTracker()

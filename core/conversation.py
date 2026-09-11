"""
core/conversation.py

ConversationController — the orchestrator for real-time conversation.

State machine:
    IDLE
      ↓  (VOICE_STT_FINAL received)
    THINKING  ← AI streaming starts
      ↓  (first token received → TTS starts)
    SPEAKING
      ↓  (TTS done)
    IDLE

Interruption path (VOICE_INTERRUPT while THINKING or SPEAKING):
    THINKING/SPEAKING
      ↓  cancel AI stream + interrupt TTS
    INTERRUPTED
      ↓  next VOICE_STT_FINAL
    THINKING  (with interruption context)

The controller lives as a singleton. It subscribes to the event bus and
drives the AI + TTS modules from those events, so the UI never needs to
coordinate them.
"""

import threading
import time
from enum import Enum, auto
from typing import Optional

from core.events import event_bus, EventType
from core.config import config


class ConvState(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()
    INTERRUPTED = auto()


class ConversationController:
    """
    Singleton that ties VoiceModule → AIModule → TTSEngine together.

    The controller is created lazily when voice is first enabled, so it
    doesn't import heavy GPU libraries at app startup.
    """

    def __init__(self, ai_module, voice_module, tts_engine):
        self._ai = ai_module
        self._voice = voice_module
        self._tts = tts_engine

        self._state = ConvState.IDLE
        self._state_lock = threading.Lock()

        self._pending_interrupt = False
        self._interrupt_text: str = ""
        self._current_turn_start: float = 0.0

        # Wire up event bus
        event_bus.event_occurred.connect(self._on_event)

    # ------------------------------------------------------------------ #
    # State helpers
    # ------------------------------------------------------------------ #
    def _set_state(self, new_state: ConvState):
        with self._state_lock:
            self._state = new_state

    def _get_state(self) -> ConvState:
        with self._state_lock:
            return self._state

    @property
    def state(self) -> ConvState:
        return self._get_state()

    # ------------------------------------------------------------------ #
    # Event dispatcher (runs on Qt main thread via Signal)
    # ------------------------------------------------------------------ #
    def _on_event(self, ev):
        t = ev.type

        if t == EventType.VOICE_STT_FINAL:
            self._handle_user_speech(ev.payload["text"])

        elif t == EventType.VOICE_INTERRUPT:
            self._handle_interrupt()

    # ------------------------------------------------------------------ #
    # Speech handling
    # ------------------------------------------------------------------ #
    def _handle_user_speech(self, text: str):
        """Called when a complete user utterance is transcribed."""
        state = self._get_state()

        if state in (ConvState.THINKING, ConvState.SPEAKING):
            # This is an interruption — cancel ongoing work
            self._cancel_current()
            self._pending_interrupt = True
            self._interrupt_text = text
            return

        if state == ConvState.INTERRUPTED:
            # Already cancelled; this is the new redirected intent
            is_interruption = True
        else:
            is_interruption = False

        self._start_thinking(text, is_interruption=is_interruption)

    def _handle_interrupt(self):
        """VOICE_INTERRUPT — user spoke while SAINT was speaking."""
        state = self._get_state()
        if state in (ConvState.THINKING, ConvState.SPEAKING):
            self._cancel_current()
            self._set_state(ConvState.INTERRUPTED)
            event_bus.emit_event(EventType.CONVERSATION_INTERRUPTED, {})

    # ------------------------------------------------------------------ #
    # Cancel in-flight work
    # ------------------------------------------------------------------ #
    def _cancel_current(self):
        """Stop AI stream and TTS immediately."""
        self._ai.cancel()
        if self._tts:
            self._tts.interrupt()
        self._voice.set_saint_speaking(False)

    # ------------------------------------------------------------------ #
    # AI + TTS pipeline
    # ------------------------------------------------------------------ #
    def _start_thinking(self, text: str, is_interruption: bool = False):
        """Start streaming AI response in a background thread."""
        self._current_turn_start = time.perf_counter()
        self._set_state(ConvState.THINKING)
        self._voice.set_saint_speaking(False)
        event_bus.emit_event(EventType.CONVERSATION_TURN_START, {
            "text": text,
            "is_interruption": is_interruption,
        })

        thread = threading.Thread(
            target=self._thinking_thread,
            args=(text, is_interruption),
            daemon=True,
            name="conv-thinking",
        )
        thread.start()

    def _thinking_thread(self, text: str, is_interruption: bool):
        """Runs in background thread: stream AI tokens → feed to TTS."""
        tts_started = threading.Event()
        sentence_buf = []
        full_response = []
        tts_start_time = [0.0]

        def on_token(token: str):
            full_response.append(token)
            sentence_buf.append(token)
            combined = "".join(sentence_buf)

            # Start TTS as soon as we have the first complete sentence
            if not tts_started.is_set():
                if any(c in combined for c in ".!?,;") or len(combined) > 60:
                    tts_start_time[0] = time.perf_counter()
                    tts_started.set()
                    self._speak_chunk(combined)
                    sentence_buf.clear()
            else:
                # Stream subsequent sentences
                if any(c in combined for c in ".!?") and len(combined) > 10:
                    self._speak_chunk(combined)
                    sentence_buf.clear()

        try:
            self._ai.stream_prompt(
                prompt=text,
                on_token=on_token,
                is_interruption=is_interruption,
            )
        except Exception as e:
            event_bus.emit_event(EventType.AI_ERROR, {"error": str(e)})
            self._set_state(ConvState.IDLE)
            self._voice.set_saint_speaking(False)
            return

        # Speak any remaining buffer
        remainder = "".join(sentence_buf).strip()
        if remainder:
            self._speak_chunk(remainder)

        # Wait for TTS to finish
        if self._tts:
            while self._tts.is_speaking():
                time.sleep(0.05)
                if self._get_state() == ConvState.INTERRUPTED:
                    break

        # Emit overall latency
        if self._current_turn_start:
            overall_ms = (time.perf_counter() - self._current_turn_start) * 1000
            event_bus.emit_event(EventType.LATENCY_OVERALL, {"ms": round(overall_ms, 1)})

        if self._get_state() != ConvState.INTERRUPTED:
            self._set_state(ConvState.IDLE)
            self._voice.set_saint_speaking(False)
            event_bus.emit_event(EventType.CONVERSATION_TURN_END, {
                "response": "".join(full_response),
            })

    def _speak_chunk(self, text: str):
        """Hand a chunk of text to TTS. Runs in thinking thread."""
        if not text.strip():
            return
        if not self._tts:
            return
        if self._get_state() == ConvState.INTERRUPTED:
            return

        self._set_state(ConvState.SPEAKING)
        self._voice.set_saint_speaking(True)

        t0 = time.perf_counter()

        def on_chunk_start(sentence: str):
            latency_ms = (time.perf_counter() - t0) * 1000
            event_bus.emit_event(EventType.TTS_SPEAK_CHUNK, {"chunk": sentence})
            event_bus.emit_event(EventType.LATENCY_TTS, {"ms": round(latency_ms, 1)})

        try:
            self._tts.speak(text, on_chunk_start=on_chunk_start)
        except Exception as e:
            event_bus.emit_event(EventType.ERROR, {"error": f"TTS error: {e}"})

        if not self._tts.is_speaking():
            self._voice.set_saint_speaking(False)


# ---------------------------------------------------------------------------
# Module-level singleton (created by VoiceModule.enable() when ready)
# ---------------------------------------------------------------------------
_controller: Optional[ConversationController] = None


def get_controller() -> Optional[ConversationController]:
    return _controller


def init_controller(ai_module, voice_module, tts_engine) -> ConversationController:
    global _controller
    _controller = ConversationController(ai_module, voice_module, tts_engine)
    return _controller

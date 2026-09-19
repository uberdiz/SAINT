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

import queue
import threading
import time
import uuid
import re
from enum import Enum, auto
from typing import Optional

from core.events import event_bus, EventType
from core.config import config


# Control tokens that should never be sent to TTS
CONTROL_TOKENS = {
    "[silence]", "[SILENCE]", "<silence>", "[SILENT]", "<SILENT>",
    "[pause]", "[PAUSE]", "<pause>",
    "[interrupted]", "[INTERRUPTED]",
    "[thinking]", "[THINKING]",
    "[action]", "[ACTION]",
    "[error]", "[ERROR]",
}


def clean_text_for_tts(text: str) -> str:
    """
    Clean text before sending to TTS.
    Removes control tokens and other non-speakable content.
    """
    if not text:
        return ""

    # Remove control tokens
    cleaned = text
    for token in CONTROL_TOKENS:
        cleaned = cleaned.replace(token, "")

    # Remove bracket patterns like [anything]
    cleaned = re.sub(r'\[[\w\s]+\]', '', cleaned)

    # Remove angle bracket patterns like <anything>
    cleaned = re.sub(r'<[\w\s]+>', '', cleaned)

    # Collapse multiple spaces
    cleaned = re.sub(r'\s+', ' ', cleaned)

    return cleaned.strip()


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
        self._thinking_lock = threading.Lock()
        self._turn_id: int = 0
        self._active_turn_id: int = -1
        self._turn_id_lock = threading.Lock()

        self._stt_session_submitted: set = set()
        self._stt_session_lock = threading.Lock()

        # TTS queue: decouple TTS playback from the AI streaming thread so
        # audio plays in a background thread while AI tokens keep flowing.
        self._tts_queue: queue.Queue = queue.Queue()
        self._tts_worker: Optional[threading.Thread] = None
        self._tts_stop_event = threading.Event()

        event_bus.event_occurred.connect(self._on_event)

        # Start the TTS worker thread (lives for the lifetime of the controller)
        self._tts_stop_event.clear()
        self._tts_worker = threading.Thread(
            target=self._tts_worker_loop, daemon=True, name="tts-worker"
        )
        self._tts_worker.start()

    def shutdown(self):
        try:
            event_bus.event_occurred.disconnect(self._on_event)
        except (TypeError, RuntimeError):
            pass
        # Stop TTS worker
        self._tts_stop_event.set()
        self._tts_queue.put(None)  # sentinel to unblock worker
        if self._tts_worker and self._tts_worker.is_alive():
            self._tts_worker.join(timeout=2.0)
        if self._tts:
            self._tts.interrupt()

    def _set_state(self, new_state: ConvState):
        with self._state_lock:
            self._state = new_state

    def _get_state(self) -> ConvState:
        with self._state_lock:
            return self._state

    @property
    def state(self) -> ConvState:
        return self._get_state()

    def _on_event(self, ev):
        t = ev.type

        if t == EventType.VOICE_STT_FINAL:
            session_id = ev.payload.get("session_id", 0)
            self._handle_user_speech(ev.payload["text"], session_id)

        elif t == EventType.VOICE_INTERRUPT:
            self._handle_interrupt()

        elif t == EventType.AI_CANCELLED:
            pass  # already handled by _cancel_current and _handle_interrupt

    # ------------------------------------------------------------------ #
    # Speech handling
    # ------------------------------------------------------------------ #
    def _handle_user_speech(self, text: str, session_id: int = 0):
        state = self._get_state()

        if state in (ConvState.THINKING, ConvState.SPEAKING):
            self._cancel_current()
            self._pending_interrupt = True
            self._interrupt_text = text
            event_bus.emit_event(EventType.AI_CANCELLED, {"reason": "user_cancel"})
            with self._turn_id_lock:
                self._active_turn_id = -1
            return

        with self._stt_session_lock:
            if session_id in self._stt_session_submitted:
                return
            self._stt_session_submitted.add(session_id)

        if state == ConvState.INTERRUPTED:
            is_interruption = True
        else:
            is_interruption = False

        self._start_thinking(text, session_id=session_id, is_interruption=is_interruption)

    def _handle_interrupt(self):
        """VOICE_INTERRUPT — user spoke while SAINT was speaking."""
        state = self._get_state()
        if state in (ConvState.THINKING, ConvState.SPEAKING):
            self._cancel_current()
            self._set_state(ConvState.INTERRUPTED)
            with self._turn_id_lock:
                self._active_turn_id = -1
            event_bus.emit_event(EventType.CONVERSATION_INTERRUPTED, {})

    # ------------------------------------------------------------------ #
    # Cancel in-flight work
    # ------------------------------------------------------------------ #
    def _cancel_current(self):
        """Stop AI stream and TTS immediately."""
        self._ai.cancel()
        if self._tts:
            self._tts.interrupt()
        # Drain pending TTS items so stale chunks aren't played after interrupt
        while not self._tts_queue.empty():
            try:
                self._tts_queue.get_nowait()
                self._tts_queue.task_done()
            except queue.Empty:
                break
        self._voice.set_saint_speaking(False)

    # ------------------------------------------------------------------ #
    # AI + TTS pipeline
    # ------------------------------------------------------------------ #
    def _start_thinking(self, text: str, session_id: int = 0, is_interruption: bool = False):
        with self._turn_id_lock:
            self._turn_id += 1
            turn_id = self._turn_id
        self._active_turn_id = turn_id
        self._current_turn_start = time.perf_counter()
        self._set_state(ConvState.THINKING)
        self._voice.set_saint_speaking(False)

        request_id = uuid.uuid4().hex[:12]
        stream_id = f"stream_{turn_id}_{uuid.uuid4().hex[:8]}"

        event_bus.emit_event(EventType.CONVERSATION_TURN_START, {
            "text": text,
            "is_interruption": is_interruption,
            "turn_id": turn_id,
            "session_id": session_id,
            "request_id": request_id,
        })

        event_bus.emit_event(EventType.AI_STREAM_START, {
            "turn_id": turn_id,
            "session_id": session_id,
            "request_id": request_id,
            "stream_id": stream_id,
        })

        thread = threading.Thread(
            target=self._thinking_thread,
            args=(text, is_interruption, turn_id, session_id, request_id, stream_id),
            daemon=True,
            name="conv-thinking",
        )
        thread.start()

    def _thinking_thread(self, text: str, is_interruption: bool, turn_id: int, session_id: int, request_id: str, stream_id: str):
        """Runs in background thread: stream AI tokens → feed to TTS."""
        if not self._thinking_lock.acquire(blocking=False):
            return
        try:
            # Apply variable agent response delay
            delay_ms = config.get("voice.agent_response_delay_ms", 0)
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)
                if self._get_state() == ConvState.INTERRUPTED:
                    return  # aborted during delay

            # Chat message start
            event_bus.emit_event(EventType.CHAT_MESSAGE_START, {
                "turn_id": turn_id,
                "request_id": request_id,
                "role": "assistant",
            })

            tts_started = threading.Event()
            sentence_buf = []
            full_response = []
            tts_start_time = [0.0]
            first_token_time = [0.0]
            first_token_emitted = [False]
            chat_length = [0]

            def on_token(token: str):
                if not first_token_emitted[0]:
                    first_token_time[0] = time.perf_counter()
                    first_token_emitted[0] = True
                    ai_first_token_ms = (first_token_time[0] - self._current_turn_start) * 1000
                    event_bus.emit_event(EventType.LATENCY_AI_FIRST_TOKEN, {"ms": round(ai_first_token_ms, 1)})

                full_response.append(token)
                sentence_buf.append(token)
                combined = "".join(sentence_buf)

                # Chat message update
                chat_length[0] += len(token)
                event_bus.emit_event(EventType.CHAT_MESSAGE_UPDATE, {
                    "turn_id": turn_id,
                    "length": chat_length[0],
                    "delta_length": len(token),
                })

                # Start TTS as soon as we have the first complete sentence
                if not tts_started.is_set():
                    if any(c in combined for c in ".!?,;") or len(combined) > 60:
                        tts_start_time[0] = time.perf_counter()
                        tts_started.set()
                        self._speak_chunk(combined, stream_id, turn_id)
                        sentence_buf.clear()
                else:
                    # Stream subsequent sentences
                    if any(c in combined for c in ".!?") and len(combined) > 10:
                        self._speak_chunk(combined, stream_id, turn_id)
                        sentence_buf.clear()

            try:
                self._ai.stream_prompt(
                    prompt=text,
                    on_token=on_token,
                    is_interruption=is_interruption,
                    turn_id=turn_id,
                    request_id=request_id,
                )
            except Exception as e:
                event_bus.emit_event(EventType.AI_ERROR, {"error": str(e)})
                self._set_state(ConvState.IDLE)
                self._voice.set_saint_speaking(False)
                return

            event_bus.emit_event(EventType.AI_STREAM_END, {
                "turn_id": turn_id,
                "session_id": session_id,
                "request_id": request_id,
                "stream_id": stream_id,
            })

            # Speak any remaining buffer
            remainder = "".join(sentence_buf).strip()
            if remainder:
                self._speak_chunk(remainder, stream_id, turn_id)

            # Wait for TTS to finish playing all queued chunks
            tts_playback_start = time.perf_counter()
            if self._tts:
                while self._tts.is_speaking() or not self._tts_queue.empty():
                    time.sleep(0.01)
                    if self._get_state() == ConvState.INTERRUPTED:
                        break
            tts_playback_ms = (time.perf_counter() - tts_playback_start) * 1000
            if tts_playback_ms > 0:
                event_bus.emit_event(EventType.LATENCY_TTS_PLAYBACK, {"ms": round(tts_playback_ms, 1)})

            # Emit AI total latency
            if self._current_turn_start:
                ai_total_ms = (time.perf_counter() - self._current_turn_start) * 1000
                event_bus.emit_event(EventType.LATENCY_AI_TOTAL, {"ms": round(ai_total_ms, 1)})

            # Emit overall latency
            if self._current_turn_start:
                overall_ms = (time.perf_counter() - self._current_turn_start) * 1000
                event_bus.emit_event(EventType.LATENCY_OVERALL, {"ms": round(overall_ms, 1)})

            final_response = "".join(full_response)
            
            # Chat message final
            event_bus.emit_event(EventType.CHAT_MESSAGE_FINAL, {
                "turn_id": turn_id,
                "length": len(final_response),
                "text": final_response,
            })
            event_bus.emit_event(EventType.UI_CHAT_RENDER, {
                "turn_id": turn_id,
                "role": "assistant",
                "text": final_response,
            })

            if self._get_state() != ConvState.INTERRUPTED:
                self._set_state(ConvState.IDLE)
            self._voice.set_saint_speaking(False)
            event_bus.emit_event(EventType.CONVERSATION_TURN_END, {
                "response": final_response,
            })
        finally:
            self._thinking_lock.release()

    def _speak_chunk(self, text: str, stream_id: str = "", turn_id: int = 0):
        """Enqueue text for TTS. Non-blocking — the calling (thinking) thread
        returns immediately so AI streaming continues while TTS audio plays in
        a dedicated background worker thread."""
        if not text.strip():
            return

        # Clean text before TTS - remove control tokens
        cleaned_text = clean_text_for_tts(text)
        if not cleaned_text or not cleaned_text.strip():
            # Text was all control tokens, skip TTS
            return

        if not self._tts:
            return
        if self._get_state() == ConvState.INTERRUPTED:
            return
        self._set_state(ConvState.SPEAKING)
        self._voice.set_saint_speaking(True)

        # Log TTS request
        event_bus.emit_event(EventType.TTS_REQUEST, {
            "turn_id": turn_id,
            "stream_id": stream_id,
            "text_length": len(cleaned_text),
            "text_preview": cleaned_text[:100],
        })

        self._tts_queue.put((cleaned_text, stream_id, turn_id))

    # ------------------------------------------------------------------ #
    # TTS worker — runs in its own thread, processes queue items serially
    # ------------------------------------------------------------------ #
    def _tts_worker_loop(self):
        """Background thread: pulls text chunks from the queue and plays them
        one at a time via ``self._tts.speak()``.  This decouples audio playback
        from the AI streaming thread, enabling true overlap."""
        while True:
            try:
                item = self._tts_queue.get(timeout=0.5)
            except queue.Empty:
                if self._tts_stop_event.is_set():
                    break
                continue

            if item is None:  # shutdown sentinel
                break

            text, stream_id, turn_id = item

            try:
                if self._get_state() == ConvState.INTERRUPTED:
                    self._tts_queue.task_done()
                    continue

                # TTS generation start
                event_bus.emit_event(EventType.TTS_GENERATION_START, {
                    "turn_id": turn_id,
                    "stream_id": stream_id,
                    "text_length": len(text),
                })
                tts_gen_start = time.perf_counter()

                def on_chunk_start(chunk: str, _start=tts_gen_start):
                    tts_gen_ms = (time.perf_counter() - _start) * 1000
                    event_bus.emit_event(EventType.TTS_SPEAK_CHUNK, {
                        "chunk": chunk,
                        "stream_id": stream_id,
                        "turn_id": turn_id,
                    })
                    event_bus.emit_event(EventType.LATENCY_TTS_INFERENCE, {"ms": round(tts_gen_ms, 1)})

                try:
                    self._voice.set_tts_playback_active(True)
                    self._tts.speak(text, turn_id=turn_id, on_chunk_start=on_chunk_start)
                except Exception as e:
                    event_bus.emit_event(EventType.TTS_ERROR, {"error": str(e), "turn_id": turn_id})
                finally:
                    # Wait for actual audio playback to finish before
                    # clearing the speaking flag.  TTSService.is_speaking()
                    # now delegates to the underlying engine (e.g. KokoroTTS)
                    # which tracks the background playback thread, so this
                    # loop blocks until audio has truly stopped — keeping
                    # _speaking=True for the voice module's barge-in logic.
                    while self._tts.is_speaking():
                        time.sleep(0.01)
                    self._voice.set_tts_playback_active(False)
                    self._last_tts_end_time = time.perf_counter()
                    self._voice.set_saint_speaking(False)
                        
                # TTS generation end
                tts_gen_ms = (time.perf_counter() - tts_gen_start) * 1000
                event_bus.emit_event(EventType.TTS_GENERATION_END, {
                    "turn_id": turn_id,
                    "stream_id": stream_id,
                    "generation_ms": round(tts_gen_ms, 1),
                })
            finally:
                self._tts_queue.task_done()


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

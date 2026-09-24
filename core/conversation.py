"""
core/conversation.py

ConversationController — the orchestrator for real-time conversation.

State machine (conversation level):
    IDLE
      ↓  (VOICE_STT_FINAL / submit_text)
    THINKING  ← agent routing + AI streaming
      ↓  (first sentence ready → TTS starts)
    SPEAKING
      ↓  (TTS done)
    IDLE

Interruption path (VOICE_INTERRUPT or new speech while THINKING/SPEAKING):
    THINKING/SPEAKING
      ↓  cancel AI stream + interrupt TTS
    INTERRUPTED
      ↓  next VOICE_STT_FINAL
    THINKING  (with interruption context)

The controller also drives the global AssistantState (PROCESSING / SPEAKING /
back to rest) that the UI renders. It lives in core and is started by
core.runtime, so voice, reminders and background work run whether or not a
window is open or focused.
"""

import logging
import queue
import re
import threading
import time
import uuid
from enum import Enum, auto
from typing import Optional

from core.assistant_state import assistant_state, AssistantState
from core.events import event_bus, EventType
from core.config import config

log = logging.getLogger("saint.conversation")


# Stop/override phrases that tell SAINT to cease speaking without starting a new turn
STOP_PHRASES = {
    "stop talking", "stop saying", "be quiet", "shut up", "hold on",
    "wait wait", "that's enough", "that is enough", "never mind",
    "nevermind", "forget it", "forget about it", "im done",
    "i m done", "im finished", "i m finished", "i am done",
    "not now", "not yet", "cancel that", "leave it",
    "leave it alone", "i m done talking", "i am done talking",
}

STOP_WORDS = {"stop", "quit", "halt", "silence", "enough", "cancel", "done", "finished"}
CONTROL_TOKENS = {
    "[silence]", "[SILENCE]", "<silence>", "[SILENT]", "<SILENT>",
    "[pause]", "[PAUSE]", "<pause>",
    "[interrupted]", "[INTERRUPTED]",
    "[thinking]", "[THINKING]",
    "[action]", "[ACTION]",
    "[error]", "[ERROR]",
}


def clean_text_for_tts(text: str) -> str:
    """Remove control tokens, markdown and other non-speakable content."""
    if not text:
        return ""
    cleaned = text
    for token in CONTROL_TOKENS:
        cleaned = cleaned.replace(token, "")
    cleaned = re.sub(r'\[[\w\s]+\]', '', cleaned)
    cleaned = re.sub(r'<[\w\s]+>', '', cleaned)
    cleaned = re.sub(r'[*_`#]+', '', cleaned)           # markdown emphasis/headers
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip()


class ConvState(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()
    INTERRUPTED = auto()


class ConversationController:
    """Ties VoiceModule → AIModule (agent) → TTS together."""

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

        # Echo suppression state
        self._current_ai_response = ""
        self._last_ai_response = ""
        self._last_tts_end_time = 0.0
        self._echo_lock = threading.Lock()

        # TTS queue: decouple playback from the AI streaming thread.
        self._tts_queue: queue.Queue = queue.Queue()
        self._tts_stop_event = threading.Event()

        event_bus.subscribe(self._on_event)

        self._tts_worker = threading.Thread(target=self._tts_worker_loop, daemon=True,
                                            name="tts-worker")
        self._tts_worker.start()

    def shutdown(self):
        try:
            event_bus.unsubscribe(self._on_event)
        except (TypeError, RuntimeError):
            pass
        self._tts_stop_event.set()
        self._tts_queue.put(None)
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

    @property
    def busy(self) -> bool:
        return self._get_state() in (ConvState.THINKING, ConvState.SPEAKING)

    # ------------------------------------------------------------------ #
    # Event handling
    # ------------------------------------------------------------------ #
    def _on_event(self, ev):
        t = ev.type
        if t == EventType.VOICE_STT_FINAL:
            p = ev.payload
            self._handle_user_speech(
                p["text"], p.get("session_id", 0), p.get("confidence", 0.0),
                addressed=bool(p.get("wake")) or p.get("source") in ("text", "inject_text"),
            )
        elif t == EventType.VOICE_INTERRUPT:
            self._handle_interrupt(ev.payload.get("source", ""))

    # ------------------------------------------------------------------ #
    # Public API (UI / runtime)
    # ------------------------------------------------------------------ #
    def submit_text(self, text: str):
        """Typed input: goes through exactly the same agent path as speech."""
        text = (text or "").strip()
        if not text:
            return
        import random
        event_bus.emit_event(EventType.VOICE_STT_FINAL, {
            "text": text, "confidence": 1.0, "latency_ms": 0.0,
            "session_id": random.randint(1, 1_000_000_000), "source": "text",
        })

    def interrupt(self):
        """Explicit stop (UI button)."""
        self._handle_interrupt("button")

    def announce(self, text: str, source: str = "system"):
        """Speak a message that did not come from a user turn (e.g. a reminder)."""
        text = (text or "").strip()
        if not text:
            return

        def run():
            deadline = time.monotonic() + 30
            while self.busy and time.monotonic() < deadline:
                time.sleep(0.2)   # don't talk over an active answer
            with self._turn_id_lock:
                self._turn_id += 1
                turn_id = self._turn_id
            self._active_turn_id = turn_id
            event_bus.emit_event(EventType.UI_CHAT_RENDER, {
                "turn_id": turn_id, "role": "assistant", "text": text, "source": source})
            if not self._tts:
                return
            assistant_state.begin_turn()
            self._speak_chunk(text, f"announce_{turn_id}", turn_id)
            time.sleep(0.05)
            while self._tts.is_speaking() or not self._tts_queue.empty():
                time.sleep(0.05)
                if self._active_turn_id != turn_id:
                    break
            if self._get_state() == ConvState.SPEAKING:
                self._set_state(ConvState.IDLE)
            assistant_state.end_turn()

        threading.Thread(target=run, daemon=True, name="announce").start()

    # ------------------------------------------------------------------ #
    # Speech handling
    # ------------------------------------------------------------------ #
    def _handle_user_speech(self, text: str, session_id: int = 0, confidence: float = 0.0,
                            addressed: bool = False):
        state = self._get_state()
        tts_recent = time.perf_counter() - self._last_tts_end_time < 5.0

        # Stop/override commands — never suppressed as echo.
        if self._is_stop_command(text) and state in (ConvState.THINKING, ConvState.SPEAKING,
                                                      ConvState.INTERRUPTED):
            log.info("voice.stop.detected text=%r → stopping", text)
            self._cancel_current()
            self._set_state(ConvState.IDLE)
            with self._turn_id_lock:
                self._active_turn_id = -1
            self._voice.set_saint_speaking(False)
            assistant_state.end_turn()
            return

        # The microphone is never muted during TTS, so the transcript may be
        # SAINT hearing itself. Addressed speech (wake word / barge-in / typed)
        # skips the text heuristics because the audio-level echo gate already
        # vetted it.
        if not addressed and (state in (ConvState.THINKING, ConvState.SPEAKING) or tts_recent):
            if self._looks_like_echo(text, confidence, state):
                return

        if state in (ConvState.THINKING, ConvState.SPEAKING):
            # New speech while SAINT is busy = an interruption with a new
            # request: cancel the current answer and handle the new one.
            log.info("conversation.interrupted_by_speech text=%r", text)
            self._cancel_current()
            self._set_state(ConvState.INTERRUPTED)
            with self._turn_id_lock:
                self._active_turn_id = -1
            event_bus.emit_event(EventType.CONVERSATION_INTERRUPTED, {"text": text})
            state = ConvState.INTERRUPTED

        with self._stt_session_lock:
            if session_id in self._stt_session_submitted:
                return
            self._stt_session_submitted.add(session_id)
            if len(self._stt_session_submitted) > 500:
                self._stt_session_submitted = set(list(self._stt_session_submitted)[-100:])

        self._start_thinking(text, session_id=session_id,
                             is_interruption=(state == ConvState.INTERRUPTED))

    def _looks_like_echo(self, text: str, confidence: float, state: ConvState) -> bool:
        during = state in (ConvState.THINKING, ConvState.SPEAKING)
        if 0 < confidence < 0.25:
            log.info("voice.echo.suppressed low_confidence=%.2f text=%r", confidence, text)
            return True
        with self._echo_lock:
            ai_text = self._current_ai_response or self._last_ai_response
        t_clean = re.sub(r'[^\w\s]', '', text.lower()).strip()
        ai_clean = re.sub(r'[^\w\s]', '', ai_text.lower()).strip()
        if not t_clean or not ai_clean:
            return False
        # Short replies ("yes", "no") legitimately share words with SAINT's
        # question, so substring matching needs a few words to be meaningful.
        if len(t_clean.split()) >= 3 and (t_clean in ai_clean or (during and ai_clean in t_clean)):
            log.info("voice.echo.suppressed matched_output text=%r", text)
            return True
        overlap = self._word_overlap(t_clean, ai_clean)
        if overlap > 0.60 and confidence < 0.55:
            log.info("voice.echo.suppressed overlap=%.0f%% text=%r", overlap * 100, text)
            return True
        return False

    def _handle_interrupt(self, source: str = ""):
        state = self._get_state()
        if state in (ConvState.THINKING, ConvState.SPEAKING):
            log.info("conversation.interrupt source=%s", source or "voice")
            self._cancel_current()
            self._set_state(ConvState.INTERRUPTED)
            with self._turn_id_lock:
                self._active_turn_id = -1
            event_bus.emit_event(EventType.CONVERSATION_INTERRUPTED, {"source": source})
            if source == "button":
                assistant_state.end_turn()

    def _is_stop_command(self, text: str) -> bool:
        cleaned = re.sub(r'[^\w\s]', '', text.lower()).strip()
        if not cleaned:
            return False
        for phrase in STOP_PHRASES:
            if phrase in cleaned:
                return True
        words = cleaned.split()
        # Only short utterances count as a bare stop word ("stop", "okay stop"),
        # so "stop the music" still reaches the agent.
        return len(words) <= 2 and bool(set(words) & STOP_WORDS)

    def _word_overlap(self, text1: str, text2: str) -> float:
        words1, words2 = set(text1.split()), set(text2.split())
        if not words1 or not words2:
            return 0.0
        return len(words1 & words2) / len(words1 | words2)

    # ------------------------------------------------------------------ #
    # Cancel in-flight work
    # ------------------------------------------------------------------ #
    def _cancel_current(self):
        """Stop AI stream and TTS immediately."""
        self._ai.cancel()
        if self._tts:
            self._tts.interrupt()
        cleared = 0
        while not self._tts_queue.empty():
            try:
                self._tts_queue.get_nowait()
                self._tts_queue.task_done()
                cleared += 1
            except queue.Empty:
                break
        if cleared:
            log.info("tts.queue.clear turn_id=%s cleared=%d", self._active_turn_id, cleared)
        event_bus.emit_event(EventType.TTS_INTERRUPTED, {"turn_id": self._active_turn_id,
                                                         "cleared": cleared})
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
        with self._echo_lock:
            self._current_ai_response = ""
        assistant_state.begin_turn()

        request_id = uuid.uuid4().hex[:12]
        stream_id = f"stream_{turn_id}_{uuid.uuid4().hex[:8]}"
        log.info("conversation.turn.start turn=%d interruption=%s text=%r",
                 turn_id, is_interruption, text)

        event_bus.emit_event(EventType.CONVERSATION_TURN_START, {
            "text": text, "is_interruption": is_interruption, "turn_id": turn_id,
            "session_id": session_id, "request_id": request_id,
        })
        event_bus.emit_event(EventType.AI_STREAM_START, {
            "turn_id": turn_id, "session_id": session_id,
            "request_id": request_id, "stream_id": stream_id,
        })
        threading.Thread(
            target=self._thinking_thread,
            args=(text, is_interruption, turn_id, session_id, request_id, stream_id),
            daemon=True, name="conv-thinking",
        ).start()

    def _thinking_thread(self, text, is_interruption, turn_id, session_id, request_id, stream_id):
        """Runs in background thread: agent/AI tokens → TTS."""
        # A previous (cancelled) turn may still be unwinding; wait for it
        # rather than silently dropping this request.
        if not self._thinking_lock.acquire(timeout=5.0):
            log.error("conversation.turn.dropped turn=%d (previous turn stuck)", turn_id)
            event_bus.emit_event(EventType.ERROR, {"error": "Previous request is still running."})
            return
        try:
            if self._active_turn_id != turn_id:
                return  # superseded while waiting
            delay_ms = config.get("voice.agent_response_delay_ms", 0)
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)
                if self._active_turn_id != turn_id:
                    return

            event_bus.emit_event(EventType.CHAT_MESSAGE_START, {
                "turn_id": turn_id, "request_id": request_id, "role": "assistant"})

            tts_started = threading.Event()
            sentence_buf, full_response = [], []
            first_token_emitted = [False]
            chat_length = [0]

            def on_token(token: str):
                if self._active_turn_id != turn_id:
                    return
                if not first_token_emitted[0]:
                    first_token_emitted[0] = True
                    event_bus.emit_event(EventType.LATENCY_AI_FIRST_TOKEN, {
                        "ms": round((time.perf_counter() - self._current_turn_start) * 1000, 1)})
                full_response.append(token)
                sentence_buf.append(token)
                combined = "".join(sentence_buf)
                with self._echo_lock:
                    self._current_ai_response = "".join(full_response)
                    self._last_ai_response = self._current_ai_response
                chat_length[0] += len(token)
                event_bus.emit_event(EventType.CHAT_MESSAGE_UPDATE, {
                    "turn_id": turn_id, "length": chat_length[0], "delta_length": len(token)})
                # Start TTS on the first complete sentence (not on commas).
                if not tts_started.is_set():
                    if any(c in combined for c in ".!?") or len(combined) > 60:
                        tts_started.set()
                        self._speak_chunk(combined, stream_id, turn_id)
                        sentence_buf.clear()
                elif any(c in combined for c in ".!?") and len(combined) > 10:
                    self._speak_chunk(combined, stream_id, turn_id)
                    sentence_buf.clear()

            try:
                self._ai.stream_prompt(prompt=text, on_token=on_token,
                                       is_interruption=is_interruption,
                                       turn_id=turn_id, request_id=request_id)
            except Exception as e:
                log.exception("conversation.ai_failed")
                event_bus.emit_event(EventType.AI_ERROR, {"error": str(e), "turn_id": turn_id})
                if self._active_turn_id == turn_id:
                    self._set_state(ConvState.IDLE)
                    assistant_state.end_turn()
                self._voice.set_saint_speaking(False)
                return

            # A cancelled generation must never publish or speak its partial
            # response after a newer user turn has taken over.
            if self._get_state() == ConvState.INTERRUPTED or self._active_turn_id != turn_id:
                return

            event_bus.emit_event(EventType.AI_STREAM_END, {
                "turn_id": turn_id, "session_id": session_id,
                "request_id": request_id, "stream_id": stream_id})

            remainder = "".join(sentence_buf).strip()
            if remainder:
                self._speak_chunk(remainder, stream_id, turn_id)

            tts_playback_start = time.perf_counter()
            if self._tts:
                while self._tts.is_speaking() or not self._tts_queue.empty():
                    time.sleep(0.01)
                    if self._get_state() == ConvState.INTERRUPTED or self._active_turn_id != turn_id:
                        break
            event_bus.emit_event(EventType.LATENCY_TTS_PLAYBACK, {
                "ms": round((time.perf_counter() - tts_playback_start) * 1000, 1)})
            if self._current_turn_start:
                total_ms = round((time.perf_counter() - self._current_turn_start) * 1000, 1)
                event_bus.emit_event(EventType.LATENCY_AI_TOTAL, {"ms": total_ms})
                event_bus.emit_event(EventType.LATENCY_OVERALL, {"ms": total_ms})

            if self._get_state() == ConvState.INTERRUPTED or self._active_turn_id != turn_id:
                return

            final_response = "".join(full_response)
            event_bus.emit_event(EventType.CHAT_MESSAGE_FINAL, {
                "turn_id": turn_id, "length": len(final_response), "text": final_response})
            event_bus.emit_event(EventType.UI_CHAT_RENDER, {
                "turn_id": turn_id, "role": "assistant", "text": final_response})

            self._set_state(ConvState.IDLE)
            self._voice.set_saint_speaking(False)
            expects_reply = bool(getattr(self._ai, "expects_reply", False))
            assistant_state.end_turn()
            log.info("conversation.turn.end turn=%d chars=%d", turn_id, len(final_response))
            # Hint for the voice module's adaptive follow-up window: an action
            # (open, play, skip, etc.) tends to be followed by another action;
            # a bare Q&A rarely is. Kept as a signal — voice module decides.
            was_action = bool(final_response) and any(w in final_response.lower().split()[:3]
                                                     for w in ("opened", "playing", "paused", "skipped",
                                                                "closed", "moved", "clicked", "focused",
                                                                "minimized", "maximized", "queued"))
            event_bus.emit_event(EventType.CONVERSATION_TURN_END, {
                "turn_id": turn_id, "user_text": text, "response": final_response,
                "expects_reply": expects_reply, "was_action": was_action})
        finally:
            self._thinking_lock.release()

    def _speak_chunk(self, text: str, stream_id: str = "", turn_id: int = 0):
        """Enqueue text for TTS (non-blocking)."""
        cleaned_text = clean_text_for_tts(text)
        if not cleaned_text or not self._tts:
            return
        if self._get_state() == ConvState.INTERRUPTED:
            return
        self._set_state(ConvState.SPEAKING)
        self._voice.set_saint_speaking(True)
        assistant_state.set(AssistantState.SPEAKING)
        event_bus.emit_event(EventType.TTS_REQUEST, {
            "turn_id": turn_id, "stream_id": stream_id,
            "text_length": len(cleaned_text), "text_preview": cleaned_text[:100]})
        self._tts_queue.put((cleaned_text, stream_id, turn_id))

    # ------------------------------------------------------------------ #
    # TTS worker — runs in its own thread, processes queue items serially
    # ------------------------------------------------------------------ #
    def _tts_worker_loop(self):
        while True:
            try:
                item = self._tts_queue.get(timeout=0.5)
            except queue.Empty:
                if self._tts_stop_event.is_set():
                    break
                continue
            if item is None:
                break
            text, stream_id, turn_id = item
            try:
                if self._get_state() == ConvState.INTERRUPTED or self._active_turn_id != turn_id:
                    continue
                event_bus.emit_event(EventType.TTS_GENERATION_START, {
                    "turn_id": turn_id, "stream_id": stream_id, "text_length": len(text)})
                tts_gen_start = time.perf_counter()

                def on_chunk_start(chunk: str, _start=tts_gen_start):
                    event_bus.emit_event(EventType.TTS_SPEAK_CHUNK, {
                        "chunk": chunk, "stream_id": stream_id, "turn_id": turn_id})
                    event_bus.emit_event(EventType.LATENCY_TTS_INFERENCE, {
                        "ms": round((time.perf_counter() - _start) * 1000, 1)})

                try:
                    self._voice.set_tts_playback_active(True)
                    ok = self._tts.speak(text, turn_id=turn_id, on_chunk_start=on_chunk_start)
                    # One controlled retry for a failed (not interrupted) synthesis
                    # so a sentence is never silently dropped.
                    if ok is False and self._get_state() != ConvState.INTERRUPTED \
                            and not self._tts_stop_event.is_set():
                        log.warning("tts.retry turn_id=%s — resynthesizing failed chunk", turn_id)
                        ok = self._tts.speak(text, turn_id=turn_id, on_chunk_start=on_chunk_start)
                    if ok is False:
                        log.error("tts.chunk.failed turn_id=%s text=%r", turn_id, text[:60])
                except Exception as e:
                    log.exception("tts.speak_failed")
                    event_bus.emit_event(EventType.TTS_ERROR, {
                        "error": f"Text-to-speech failed: {e}", "turn_id": turn_id})
                finally:
                    while self._tts.is_speaking() or getattr(self._tts, 'playback_active', False):
                        time.sleep(0.01)
                    self._voice.set_tts_playback_active(False)
                    self._last_tts_end_time = time.perf_counter()
                    self._voice.set_saint_speaking(False)
                event_bus.emit_event(EventType.TTS_GENERATION_END, {
                    "turn_id": turn_id, "stream_id": stream_id,
                    "generation_ms": round((time.perf_counter() - tts_gen_start) * 1000, 1)})
            finally:
                self._tts_queue.task_done()


# ---------------------------------------------------------------------------
# Module-level singleton (created by core.runtime)
# ---------------------------------------------------------------------------
_controller: Optional[ConversationController] = None


def get_controller() -> Optional[ConversationController]:
    return _controller


def init_controller(ai_module, voice_module, tts_engine) -> ConversationController:
    global _controller
    if _controller is None:
        _controller = ConversationController(ai_module, voice_module, tts_engine)
    return _controller

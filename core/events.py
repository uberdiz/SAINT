"""Event bus and event type constants used throughout SAINT.

Two kinds of subscribers:

* Core services (conversation controller, voice, assistant state, logger,
  runtime, ...) use ``event_bus.subscribe(fn)``. They are called directly on
  the thread that emitted the event, so the voice/agent pipeline never waits
  on — or depends on — the Qt GUI thread.
* UI widgets use ``event_bus.event_occurred.connect(slot)``. Qt queues those
  deliveries onto the GUI thread, which is the only thread allowed to touch
  widgets.
"""
import itertools
import logging
import threading
import time

from PySide6.QtCore import QObject, Signal

_log = logging.getLogger("saint.events")


class Event:
    __slots__ = ("id", "type", "payload", "timestamp")
    _counter = itertools.count(1)

    def __init__(self, type_, payload=None):
        self.id = next(Event._counter)
        self.type = type_
        self.payload = payload or {}
        self.timestamp = time.time()

    def formatted_time(self):
        return time.strftime("%H:%M:%S", time.localtime(self.timestamp))


class EventBus(QObject):
    event_occurred = Signal(object)

    def __init__(self):
        super().__init__()
        self._subscribers = []
        self._sub_lock = threading.Lock()

    def subscribe(self, fn):
        """Register a core (non-UI) handler, called synchronously on emit."""
        with self._sub_lock:
            if fn not in self._subscribers:
                self._subscribers.append(fn)

    def unsubscribe(self, fn):
        with self._sub_lock:
            try:
                self._subscribers.remove(fn)
            except ValueError:
                pass

    def emit_event(self, type_, payload=None):
        ev = Event(type_, payload)
        with self._sub_lock:
            subs = list(self._subscribers)
        for fn in subs:
            try:
                fn(ev)
            except Exception:
                # One faulty subscriber must never break the pipeline.
                _log.exception("event subscriber failed for %s", type_)
        self.event_occurred.emit(ev)
        return ev


class EventType:
    APP_STARTED="app.started"
    MODULE_LOADED="module.loaded"; MODULE_UNLOADED="module.unloaded"
    MODULE_ENABLED="module.enabled"; MODULE_DISABLED="module.disabled"; MODULE_CRASH="module.crash"
    AI_REQUEST="ai.request"; AI_RESPONSE="ai.response"; AI_ERROR="ai.error"
    AI_STREAM_TOKEN="ai.stream.token"; AI_STREAM_START="ai.stream.start"; AI_STREAM_END="ai.stream.end"
    AI_STREAM_DONE="ai.stream.done"; AI_CANCELLED="ai.cancelled"; AI_PROVIDER_TEST="ai.provider.test"
    VOICE_LISTENING_START="voice.listening.start"; VOICE_LISTENING_STOP="voice.listening.stop"
    VOICE_AUDIO_LEVEL="voice.audio.level"; VOICE_SPEECH_START="voice.speech.start"; VOICE_SPEECH_END="voice.speech.end"
    VOICE_STATE_CHANGE="voice.state.change"; VOICE_TURN_START="voice.turn.start"; VOICE_TURN_END="voice.turn.end"
    VOICE_WAKE_WORD="voice.wake_word"; VOICE_FALSE_WAKE_WORD="voice.false_wake_word"
    VOICE_STT_PARTIAL="voice.stt.partial"; VOICE_STT_FINAL="voice.stt.final"; VOICE_STT_ERROR="voice.stt.error"
    VOICE_STT_SKIP="voice.stt.skip"; VOICE_STT_DEBUG="voice.stt.debug"
    TTS_SPEAK_START="tts.speak.start"; TTS_INFERENCE_START="tts.inference.start"; TTS_INFERENCE_END="tts.inference.end"
    TTS_AUDIO_READY="tts.audio.ready"; TTS_PLAYBACK_START="tts.playback.start"; TTS_PLAYBACK_END="tts.playback.end"
    TTS_SPEAK_CHUNK="tts.speak.chunk"; TTS_SPEAK_DONE="tts.speak.done"; TTS_INTERRUPTED="tts.interrupted"; TTS_ERROR="tts.error"
    TTS_REQUEST="tts.request"; TTS_GENERATION_START="tts.generation.start"; TTS_GENERATION_END="tts.generation.end"
    TTS_STATE_CHANGE="tts.state.change"; TTS_WARMUP_COMPLETE="tts.warmup.complete"; TTS_SKIPPED="tts.skipped"; TTS_DUPLICATE="tts.duplicate"
    TTS_DEVICE_INFO="tts.device.info"; TTS_FALLBACK="tts.fallback"; TTS_SPEAK_ERROR="tts.speak.error"
    VOICE_INTERRUPT="voice.interrupt"; CONVERSATION_TURN_START="conversation.turn.start"; CONVERSATION_TURN_END="conversation.turn.end"
    CONVERSATION_INTERRUPTED="conversation.interrupted"
    CHAT_MESSAGE_START="chat.message.start"; CHAT_MESSAGE_UPDATE="chat.message.update"; CHAT_MESSAGE_FINAL="chat.message.final"; UI_CHAT_RENDER="ui.chat.render"
    LATENCY_STT="latency.stt"; LATENCY_INTENT="latency.intent"; LATENCY_AI_FIRST_TOKEN="latency.ai.first_token"
    LATENCY_AI_TOTAL="latency.ai.total"; LATENCY_MODEL="latency.model"; LATENCY_TTS_INFERENCE="latency.tts.inference"
    LATENCY_TTS_PLAYBACK="latency.tts.playback"; LATENCY_OVERALL="latency.overall"; LATENCY_WAKE_WORD="latency.wake_word"
    UI_UPDATED="ui.updated"; SETTINGS_CHANGED="settings.changed"
    SPOTIFY_CONNECTED="spotify.connected"; SPOTIFY_DISCONNECTED="spotify.disconnected"
    SPOTIFY_PLAYBACK_CHANGED="spotify.playback.changed"; SPOTIFY_ERROR="spotify.error"
    TOOL_REQUESTED="tool.requested"; TOOL_PERMISSION_REQUIRED="tool.permission.required"
    TOOL_PERMISSION_GRANTED="tool.permission.granted"; TOOL_PERMISSION_DENIED="tool.permission.denied"
    TOOL_STARTED="tool.started"; TOOL_COMPLETED="tool.completed"; TOOL_FAILED="tool.failed"
    # Authoritative assistant state (core/assistant_state.py) — the UI renders
    # this rather than inferring state from individual pipeline events.
    ASSISTANT_STATE="assistant.state"
    # Wake word
    WAKE_STATUS="wake.status"; WAKE_ERROR="wake.error"; VOICE_WAKE_SCORE="voice.wake.score"
    VOICE_COMMAND_TIMEOUT="voice.command.timeout"; VOICE_BARGE_IN="voice.barge_in"
    # Agent
    AGENT_INTENT="agent.intent"; AGENT_CONFIRM_REQUIRED="agent.confirm.required"
    AGENT_CONFIRM_RESOLVED="agent.confirm.resolved"
    # Memory
    MEMORY_STORED="memory.stored"; MEMORY_UPDATED="memory.updated"; MEMORY_DELETED="memory.deleted"
    MEMORY_RECALLED="memory.recalled"
    # Automations / scheduler
    AUTOMATION_CREATED="automation.created"; AUTOMATION_UPDATED="automation.updated"
    AUTOMATION_CANCELLED="automation.cancelled"; AUTOMATION_TRIGGERED="automation.triggered"
    AUTOMATION_FAILED="automation.failed"
    # Desktop control / screen
    DESKTOP_ACTION="desktop.action"; SCREEN_CAPTURED="screen.captured"
    # User-facing notification (tray balloon / toast)
    NOTIFY="notify"
    ERROR="error"; WARNING="warning"


# Singleton instance
event_bus = EventBus()

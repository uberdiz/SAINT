"""
core/events.py

The Event Bus is the spine of SAINT. No module talks to another module
directly -- everything flows through here as an Event. The UI, logger,
and analytics engine all subscribe to this single stream.

Built on a QObject + Signal so that events emitted from worker threads
(e.g. an in-flight AI request) are safely marshalled back onto the Qt
main thread for any UI subscribers.
"""

import time
import itertools
from PySide6.QtCore import QObject, Signal


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

    def __repr__(self):
        return f"<Event #{self.id} {self.type} {self.payload}>"


class EventBus(QObject):
    """Global event bus. Import `event_bus` (singleton) elsewhere."""

    event_occurred = Signal(object)  # emits an Event instance

    def emit_event(self, type_, payload=None):
        ev = Event(type_, payload)
        self.event_occurred.emit(ev)
        return ev


# ---------------------------------------------------------------------------
# Common event type constants
# ---------------------------------------------------------------------------
class EventType:
    # App / module lifecycle
    APP_STARTED = "app.started"
    MODULE_LOADED = "module.loaded"
    MODULE_UNLOADED = "module.unloaded"
    MODULE_ENABLED = "module.enabled"
    MODULE_DISABLED = "module.disabled"
    MODULE_CRASH = "module.crash"

    # AI (text) pipeline
    AI_REQUEST = "ai.request"
    AI_RESPONSE = "ai.response"
    AI_ERROR = "ai.error"
    AI_STREAM_TOKEN = "ai.stream.token"     # payload: {token: str}
    AI_STREAM_DONE = "ai.stream.done"       # payload: {full_text: str, elapsed_seconds: float}
    AI_CANCELLED = "ai.cancelled"           # payload: {reason: str}

    # Voice — audio input
    VOICE_LISTENING_START = "voice.listening.start"
    VOICE_LISTENING_STOP = "voice.listening.stop"
    VOICE_AUDIO_LEVEL = "voice.audio.level"         # payload: {level: float 0-1}
    VOICE_SPEECH_START = "voice.speech.start"       # VAD detected voice onset
    VOICE_SPEECH_END = "voice.speech.end"           # VAD detected voice offset

    # Voice — wake word
    VOICE_WAKE_WORD = "voice.wake_word"             # payload: {confidence: float}
    VOICE_FALSE_WAKE_WORD = "voice.false_wake_word"

    # Voice — STT
    VOICE_STT_PARTIAL = "voice.stt.partial"         # payload: {text: str, confidence: float}
    VOICE_STT_FINAL = "voice.stt.final"             # payload: {text: str, confidence: float, latency_ms: float}
    VOICE_STT_ERROR = "voice.stt.error"

    # Voice — TTS
    TTS_SPEAK_START = "tts.speak.start"             # payload: {text: str}
    TTS_SPEAK_CHUNK = "tts.speak.chunk"             # payload: {chunk: str} (sentence emitted to audio)
    TTS_SPEAK_DONE = "tts.speak.done"               # payload: {latency_ms: float}
    TTS_INTERRUPTED = "tts.interrupted"             # payload: {}

    # Conversation
    VOICE_INTERRUPT = "voice.interrupt"             # user spoke while SAINT was speaking
    CONVERSATION_TURN_START = "conversation.turn.start"
    CONVERSATION_TURN_END = "conversation.turn.end"
    CONVERSATION_INTERRUPTED = "conversation.interrupted"

    # Latency analytics (emitted by ConversationController)
    LATENCY_STT = "latency.stt"                     # payload: {ms: float}
    LATENCY_INTENT = "latency.intent"               # payload: {ms: float}
    LATENCY_MODEL = "latency.model"                 # payload: {ms: float}
    LATENCY_TTS = "latency.tts"                     # payload: {ms: float}
    LATENCY_OVERALL = "latency.overall"             # payload: {ms: float}
    LATENCY_WAKE_WORD = "latency.wake_word"         # payload: {ms: float}

    # UI
    UI_UPDATED = "ui.updated"
    SETTINGS_CHANGED = "settings.changed"

    ERROR = "error"
    WARNING = "warning"


# Singleton instance
event_bus = EventBus()

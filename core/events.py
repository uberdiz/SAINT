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
    AI_STREAM_TOKEN = "ai.stream.token"     # payload: {token: str, turn_id: int, stream_id: str}
    AI_STREAM_START = "ai.stream.start"     # payload: {turn_id: int, session_id: int, request_id: str}
    AI_STREAM_END = "ai.stream.end"         # payload: {turn_id: int, session_id: int, request_id: str}
    AI_STREAM_DONE = "ai.stream.done"
    AI_CANCELLED = "ai.cancelled"           # payload: {reason: str}
    AI_PROVIDER_TEST = "ai.provider.test"   # payload: {provider: str, model: str, connected: bool, details: dict}

    # Voice — audio input
    VOICE_LISTENING_START = "voice.listening.start"
    VOICE_LISTENING_STOP = "voice.listening.stop"
    VOICE_AUDIO_LEVEL = "voice.audio.level"         # payload: {level: float 0-1}
    VOICE_SPEECH_START = "voice.speech.start"       # VAD detected voice onset
    VOICE_SPEECH_END = "voice.speech.end"           # VAD detected voice offset
    VOICE_STATE_CHANGE = "voice.state.change"       # payload: {old_state, new_state, reason}
    VOICE_TURN_START = "voice.turn.start"           # payload: {turn_id, session_id, user_text}
    VOICE_TURN_END = "voice.turn.end"               # payload: {turn_id, was_interrupted}

    # Voice — wake word
    VOICE_WAKE_WORD = "voice.wake_word"             # payload: {confidence: float}
    VOICE_FALSE_WAKE_WORD = "voice.false_wake_word"

    # Voice — STT
    VOICE_STT_PARTIAL = "voice.stt.partial"         # payload: {text: str, confidence: float}
    VOICE_STT_FINAL = "voice.stt.final"             # payload: {text: str, confidence: float, latency_ms: float}
    VOICE_STT_ERROR = "voice.stt.error"
    VOICE_STT_SKIP = "voice.stt.skip"               # payload: {session_id: int, reason: str, ...}

    VOICE_STT_DEBUG = "voice.stt.debug"

# Voice — TTS
    TTS_SPEAK_START = "tts.speak.start"             # payload: {text: str, word_count: int}
    TTS_INFERENCE_START = "tts.inference.start"     # payload: {word_index: int, word: str}
    TTS_INFERENCE_END = "tts.inference.end"         # payload: {word_index: int, inference_ms: float, sample_rate: int, samples: int, duration_ms: float}
    TTS_AUDIO_READY = "tts.audio.ready"             # payload: {word_index: int, samples: int, sample_rate: int, channels: int, duration_ms: float}
    TTS_PLAYBACK_START = "tts.playback.start"       # payload: {word_index: int}
    TTS_PLAYBACK_END = "tts.playback.end"           # payload: {word_index: int, playback_ms: float}
    TTS_SPEAK_CHUNK = "tts.speak.chunk"             # payload: {chunk: str, stream_id: str} (word emitted to audio)
    TTS_SPEAK_DONE = "tts.speak.done"               # payload: {text: str}
    TTS_INTERRUPTED = "tts.interrupted"             # payload: {}
    TTS_ERROR = "tts.error"                         # payload: {error: str, turn_id: int}
    TTS_REQUEST = "tts.request"                     # payload: {turn_id: int, stream_id: str, text_length: int, text_preview: str}
    TTS_GENERATION_START = "tts.generation.start"   # payload: {turn_id: int, stream_id: str, text_length: int}
    TTS_GENERATION_END = "tts.generation.end"       # payload: {turn_id: int, stream_id: str, generation_ms: float}
    TTS_STATE_CHANGE = "tts.state.change"           # payload: {old_state: str, new_state: str}
    TTS_WARMUP_COMPLETE = "tts.warmup.complete"     # payload: {warmup_ms: float, engine: str}
    TTS_SKIPPED = "tts.skipped"                     # payload: {reason: str, text_preview: str, turn_id: int}
    TTS_DUPLICATE = "tts.duplicate"                 # payload: {request_id: str, turn_id: int}

    # Conversation
    VOICE_INTERRUPT = "voice.interrupt"             # user spoke while SAINT was speaking
    CONVERSATION_TURN_START = "conversation.turn.start"
    CONVERSATION_TURN_END = "conversation.turn.end"
    CONVERSATION_INTERRUPTED = "conversation.interrupted"

    # Chat message lifecycle
    CHAT_MESSAGE_START = "chat.message.start"       # payload: {turn_id, request_id, role="assistant"}
    CHAT_MESSAGE_UPDATE = "chat.message.update"     # payload: {turn_id, length, delta_length}
    CHAT_MESSAGE_FINAL = "chat.message.final"       # payload: {turn_id, length, text}
    UI_CHAT_RENDER = "ui.chat.render"               # payload: {turn_id, role, text}

    # Latency analytics (emitted by ConversationController)
    LATENCY_STT = "latency.stt"                     # payload: {ms: float}
    LATENCY_INTENT = "latency.intent"               # payload: {ms: float}
    LATENCY_AI_FIRST_TOKEN = "latency.ai.first_token"  # payload: {ms: float}
    LATENCY_AI_TOTAL = "latency.ai.total"           # payload: {ms: float}
    LATENCY_MODEL = "latency.model"                 # payload: {ms: float}
    LATENCY_TTS_INFERENCE = "latency.tts.inference" # payload: {ms: float}
    LATENCY_TTS_PLAYBACK = "latency.tts.playback"   # payload: {ms: float}
    LATENCY_OVERALL = "latency.overall"             # payload: {ms: float}
    LATENCY_WAKE_WORD = "latency.wake_word"         # payload: {ms: float}

    # UI
    UI_UPDATED = "ui.updated"
    SETTINGS_CHANGED = "settings.changed"

    ERROR = "error"
    WARNING = "warning"


# Singleton instance
event_bus = EventBus()

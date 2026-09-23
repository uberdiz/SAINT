"""Regressions for bugs found during the live end-to-end run."""

import time

from core.audio_echo import playback_monitor
from modules.agent.router import route
from modules.automation.timeparse import extract_reminder
from modules.automation.tools import Tool, ToolRegistry, PermissionLevel, P
from modules.voice.module import VoiceModule


def test_tool_parameter_called_name_is_allowed():
    # ToolRegistry.execute used to take the tool name as a keyword too, so
    # desktop.open_app(name=...) crashed with "multiple values for 'name'".
    reg = ToolRegistry()
    reg.register(Tool("t.open", "x", {}, PermissionLevel.LOW, lambda name: {"opened": name},
                      parameters={"name": P("string")}))
    r = reg.execute("t.open", name="notepad")
    assert r.success and r.result == {"opened": "notepad"}


def test_whisper_wake_variants_are_stripped():
    v = VoiceModule.__new__(VoiceModule)
    assert v._strip_wake_prefix("Hey, St.") == ""
    assert v._strip_wake_prefix("Hey, St. what time is it?") == "what time is it?"
    assert v._strip_wake_prefix("St. Louis weather") == "St. Louis weather"


def test_leading_fillers_do_not_break_routing():
    # "Actually, what time is it?" went to the LLM, which invented a time.
    assert route("Actually, what time is it?").name == "time"
    assert route("um, okay, pause the music").name == "spotify.pause"


def test_from_now_durations():
    r = extract_reminder("set a reminder twenty minutes from now to drink water")
    assert r["schedule"]["type"] == "once" and r["message"] == "Drink water"
    assert 19 * 60 < r["schedule"]["at"] - time.time() <= 20 * 60


def test_tts_counts_as_speaking_while_audio_plays():
    # The controller used to consider TTS finished as soon as synthesis
    # returned, while Kokoro was still playing — so SAINT went back to
    # "listening", barge-in was disabled and the next command was dropped.
    from modules.voice.tts_service import TTSService
    svc = TTSService.__new__(TTSService)
    import threading
    svc._speaking_lock = threading.Lock()
    svc._is_speaking = False

    class Engine:
        _playback_active = True

        def is_speaking(self):
            return False
    svc._engine = Engine()
    assert svc.is_speaking() is True
    svc._engine._playback_active = False
    playback_monitor.clear()
    playback_monitor.note_block(0.1, 0.5)
    assert svc.is_speaking() is True      # audio still in flight
    playback_monitor.clear()
    assert svc.is_speaking() is False


def test_llm_gets_real_clock(monkeypatch):
    from modules.ai.module import AIModule
    from modules.ai import providers

    captured = {}

    class Fake(providers.MockProvider):
        def stream_send(self, messages, *a, **k):
            captured["messages"] = messages
            return "ok"
    monkeypatch.setattr(providers, "get_provider", lambda name: Fake())
    import modules.ai.module as aimod
    monkeypatch.setattr(aimod, "get_provider", lambda name: Fake())
    ai = AIModule()
    ai.stream_prompt("why is the sky blue", on_token=lambda t: None)
    sys_text = " ".join(m["content"] for m in captured["messages"] if m["role"] == "system")
    assert "Current local date and time" in sys_text

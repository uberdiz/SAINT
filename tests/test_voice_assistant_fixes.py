"""
tests/test_voice_assistant_fixes.py

Regression tests for the SAINT voice-assistant bug fixes:
  * deterministic Spotify intent routing (no "spotify" keyword needed)
  * Spotify client robustness (empty 204 body, structured errors)
  * STT activation gating (false-activation rejection)
  * Ollama model resolution + no silent mock fallback

All tests are network-free (HTTP and providers are faked).
"""

import types

import pytest

from core.config import config
from modules.ai.module import AIModule
from modules.voice.module import VoiceModule
from modules.spotify.client import SpotifyClient, SpotifyAPIError, friendly_error


# --- deterministic Spotify routing ---------------------------------------------
@pytest.fixture
def ai():
    return AIModule.__new__(AIModule)  # bypass __init__ (no provider setup)


@pytest.mark.parametrize("text,tool", [
    ("skip the song", "spotify.next"),
    ("skip this", "spotify.next"),
    ("next song", "spotify.next"),
    ("play the next track", "spotify.next"),
    ("previous song", "spotify.previous"),
    ("go back", "spotify.previous"),
    ("pause the music", "spotify.pause"),
    ("resume", "spotify.play"),
    ("what am i listening to", "spotify.current"),
    ("what song is this", "spotify.current"),
    ("set volume to 40", "spotify.volume"),
    ("shuffle on", "spotify.shuffle"),
    ("repeat this track", "spotify.repeat"),
])
def test_spotify_intent_routes(ai, text, tool):
    from modules.agent.context import desktop_context
    desktop_context.note_domain("spotify")        # "go back" means the song after a music command
    try:
        intent = ai._spotify_intent(text)
    finally:
        desktop_context.clear()
    assert intent is not None, f"{text!r} should route"
    assert intent[1] == tool


@pytest.mark.parametrize("text", [
    "what is the capital of France",
    "tell me a joke",
    "how are you today",
])
def test_non_music_falls_through_to_llm(ai, text):
    assert ai._spotify_intent(text) is None


# --- Spotify client robustness -------------------------------------------------
class _FakeResp:
    def __init__(self, status, content=b"", json_data=None):
        self.status_code = status
        self.content = content
        self._json = json_data
        self.text = content.decode() if content else ""
        self.headers = {}

    def json(self):
        import json
        if self._json is not None:
            return self._json
        return json.loads(self.text)  # raises on empty -> exercises our guard


class _FakeAuth:
    def get_access_token(self):
        return "TESTTOKEN"

    def clear(self):
        pass


def _client(monkeypatch, resp):
    import modules.spotify.client as cl
    monkeypatch.setattr(cl.requests, "request", lambda *a, **k: resp)
    return SpotifyClient(_FakeAuth())


def test_204_no_content_does_not_crash(monkeypatch):
    c = _client(monkeypatch, _FakeResp(204, b""))
    data, _ = c.request("POST", "/me/player/next")
    assert data is None  # no "Expecting value" JSONDecodeError


def test_empty_200_body_does_not_crash(monkeypatch):
    c = _client(monkeypatch, _FakeResp(200, b""))
    data, _ = c.request("GET", "/me/player")
    assert data is None


def test_no_active_device_is_structured(monkeypatch):
    body = {"error": {"status": 404, "message": "No active device", "reason": "NO_ACTIVE_DEVICE"}}
    import json
    c = _client(monkeypatch, _FakeResp(404, json.dumps(body).encode(), json_data=body))
    with pytest.raises(SpotifyAPIError) as ei:
        c.request("POST", "/me/player/next")
    assert ei.value.code == "NO_ACTIVE_DEVICE"
    assert "device" in friendly_error(ei.value).lower()
    # Never leaks a raw JSONDecodeError message.
    assert "Expecting value" not in friendly_error(ei.value)


def test_auth_expired_is_structured(monkeypatch):
    c = _client(monkeypatch, _FakeResp(401, b"{}", json_data={}))
    with pytest.raises(SpotifyAPIError) as ei:
        c.request("POST", "/me/player/pause")
    assert ei.value.code == "AUTH_EXPIRED"


# --- STT activation gate -------------------------------------------------------
@pytest.fixture
def voice():
    return VoiceModule.__new__(VoiceModule)


def test_gate_rejects_foreign_wake_word(voice):
    ok, reason = voice._passes_activation_gate("Alexa skip.", 0.29)
    assert ok is False and reason == "foreign_wake_word"


def test_gate_rejects_low_confidence(voice):
    ok, reason = voice._passes_activation_gate("There's nothing to skip right now.", 0.48)
    assert ok is False and reason.startswith("low_confidence")


def test_gate_rejects_hallucination(voice):
    assert voice._passes_activation_gate("thank you", 0.4)[0] is False
    assert voice._passes_activation_gate("you", 0.3)[0] is False


def test_gate_accepts_clear_command(voice):
    assert voice._passes_activation_gate("skip the song", 0.72)[0] is True
    assert voice._passes_activation_gate("what is the weather today", 0.65)[0] is True


# --- model resolution ----------------------------------------------------------
def test_resolve_model_substitutes_missing(ai, monkeypatch):
    import modules.ai.module as aim

    class P:
        def list_models(self, *a, **k):
            return [{"name": "smollm2:360m"}, {"name": "llama3.2:1b"}, {"name": "llama3.1:latest"}]

    monkeypatch.setattr(aim, "get_provider", lambda name: P())
    model, info = ai._resolve_model("ollama", "http://x", "llama3")
    assert model in {"llama3.2:1b", "llama3.1:latest"}  # a real installed model
    assert info is not None and info["configured"] == "llama3"


def test_resolve_model_keeps_installed(ai, monkeypatch):
    import modules.ai.module as aim

    class P:
        def list_models(self, *a, **k):
            return [{"name": "llama3.1:latest"}]

    monkeypatch.setattr(aim, "get_provider", lambda name: P())
    model, info = ai._resolve_model("ollama", "http://x", "llama3.1:latest")
    assert model == "llama3.1:latest"
    assert info is None


# --- wake word inside a follow-up window --------------------------------------
def _followup_voice(text):
    import time as _t
    from tests.test_wake_and_voice import _voice, _speech
    config.set("voice.wake_word_chime", False, persist=False)
    v = _voice(text)
    v._music_playing = True
    v._enter_command(15.0, reason="follow-up")
    return v, _speech(30), _t.perf_counter()


def test_bare_wake_word_during_follow_up_opens_a_fresh_wake_window():
    v, frames, t0 = _followup_voice("Hey SAINT!")
    assert v._stt_worker_func(frames, 1, t0, wake_initiated=True, follow_up=True) == ""
    assert v._command_reason == "wake word"
    # ...so the next command is wake-initiated and skips the music guard.
    v._stt.text = "what time is it"
    follow_up = v._command_reason == "follow-up"
    assert v._stt_worker_func(frames, 2, t0, wake_initiated=True, follow_up=follow_up)


def test_wake_word_prefix_during_follow_up_bypasses_music_guard():
    v, frames, t0 = _followup_voice("Hey SAINT, that song was actually amazing")
    assert v._stt_worker_func(frames, 1, t0, wake_initiated=True, follow_up=True) == \
        "that song was actually amazing"


def test_they_saint_is_heard_as_the_wake_word(voice):
    assert voice._starts_with_wake("They SAINT!")
    assert voice._strip_wake_prefix("They SAINT, click the first link.") == "click the first link."


def test_low_confidence_hotword_still_fires():
    from core.events import Event, EventType
    v = VoiceModule.__new__(VoiceModule)
    v._on_bus_event(Event(EventType.SPOTIFY_PLAYBACK_CHANGED, {"is_playing": True, "track": "Song"}))
    assert v._is_music_hotword("Skip.", 0.25)
    assert v._is_music_hotword("next", 0.3)
    assert v._is_music_hotword("skip that one", 0.3)


def test_resolve_model_prefers_the_bigger_equal_match(ai, monkeypatch):
    import modules.ai.module as aim

    class P:
        def list_models(self, *a, **k):
            return [{"name": "smollm2:360m", "size": 270_000_000}, {"name": "llama3.2:1b", "size": 1_300_000_000},
                    {"name": "llama3.1:latest", "size": 4_900_000_000}]

    monkeypatch.setattr(aim, "get_provider", lambda name: P())
    assert ai._resolve_model("ollama", "http://x", "llama3")[0] == "llama3.1:latest"
    P.list_models = lambda self, *a, **k: [{"name": "llama3.2:1b"}, {"name": "llama3.1:latest"}]
    assert ai._resolve_model("ollama", "http://x", "llama3")[0] == "llama3.1:latest"


# --- second live run (2026-09-24) -------------------------------------------------
def test_one_word_follow_up_command_passes_at_zero_confidence(voice):
    voice._music_playing = True
    # live: "Pause." came back at 0.00 twice and was dropped
    assert voice._passes_activation_gate("Pause.", 0.0, follow_up=True)[0]
    assert voice._passes_activation_gate("pause", 0.0, follow_up=True)[0]
    assert not voice._passes_activation_gate("wow", 0.0, follow_up=True)[0]


def test_fragment_after_wake_keeps_listening_for_the_rest():
    v, frames, t0 = _followup_voice("and")
    v._enter_command(6.0, reason="wake word")
    assert v._stt_worker_func(frames, 1, t0, wake_initiated=True) == ""
    assert v._command_reason == "wake word"          # still listening, nothing sent to the agent


def test_speech_that_started_during_a_command_stays_a_command():
    """Live: the wake word fired mid-sentence, a fragment was handled, SAINT went
    back to wake-word listening and dropped the rest of the request as
    'background speech'. A segment that began in the command window is still
    the user's command."""
    from modules.voice.module import ListenPhase
    from tests.test_wake_and_voice import _finals, _wait
    finals, done = _finals()
    try:
        v, frames, _ = _followup_voice("what time is it and what is the date today")
        with v._phase_lock:
            v._phase = ListenPhase.WAKE                 # the early fragment already ended the window
        v._end_segment(frames, len(frames), 7, 5, 0.001, False, started_reason="wake word")
        _wait(lambda: finals)
        assert finals and finals[0]["text"].startswith("what time is it")
    finally:
        done()

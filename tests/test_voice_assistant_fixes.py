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
    intent = ai._spotify_intent(text)
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

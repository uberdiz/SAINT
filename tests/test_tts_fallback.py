"""
tests/test_tts_fallback.py

Tests for the TTSService state machine's FALLBACK handling and controlled
reinitialize(), using a lightweight fake engine so no real model is loaded and
no GPU is required.
"""

import threading

import pytest

import modules.voice.tts as tts_mod
from modules.voice.tts_service import (
    TTSState,
    reset_tts_service,
    get_tts_service,
)


class _FakeEngine:
    """Minimal TTS engine that reports a CPU fallback."""

    def __init__(self, fell_back=True, **kwargs):
        self._fell_back = fell_back
        self._interrupt_event = threading.Event()
        self.spoken = []

    def _load(self):
        pass

    @property
    def device_info(self):
        return {
            "requested_device": "cuda",
            "resolved_device": "cpu" if self._fell_back else "cuda",
            "cuda_available": not self._fell_back,
            "fell_back": self._fell_back,
            "reason": "CUDA unavailable: CPU-only build" if self._fell_back else "CUDA available",
        }

    def warm_up(self):
        pass

    def speak(self, text, turn_id=0, on_chunk_start=None):
        self.spoken.append(text)

    def interrupt(self, turn_id=None):
        self._interrupt_event.set()

    def is_speaking(self):
        return False


@pytest.fixture(autouse=True)
def _fresh_service():
    reset_tts_service()
    yield
    reset_tts_service()


def test_fallback_engine_reaches_fallback_state(monkeypatch):
    monkeypatch.setattr(tts_mod, "KokoroTTS", lambda **kw: _FakeEngine(fell_back=True, **kw))
    svc = get_tts_service()
    ok = svc.initialize(backend="kokoro", blocking=True, device="cuda")
    assert ok is True
    assert svc.state == TTSState.FALLBACK
    assert svc.is_ready is True            # FALLBACK is operational
    diag = svc.get_diagnostics()
    assert diag["is_fallback"] is True
    assert diag["device"]["resolved_device"] == "cpu"


def test_non_fallback_engine_reaches_ready_state(monkeypatch):
    monkeypatch.setattr(tts_mod, "KokoroTTS", lambda **kw: _FakeEngine(fell_back=False, **kw))
    svc = get_tts_service()
    assert svc.initialize(backend="kokoro", blocking=True, device="cuda") is True
    assert svc.state == TTSState.READY
    assert svc.get_diagnostics()["is_fallback"] is False


def test_synthesis_returns_to_fallback_state(monkeypatch):
    monkeypatch.setattr(tts_mod, "KokoroTTS", lambda **kw: _FakeEngine(fell_back=True, **kw))
    svc = get_tts_service()
    svc.initialize(backend="kokoro", blocking=True, device="cuda")
    assert svc.synthesize("hello world", turn_id=1) is True
    # After synthesis it must return to the FALLBACK idle state, not plain READY.
    assert svc.state == TTSState.FALLBACK


def test_reinitialize_before_initialize_is_noop():
    svc = get_tts_service()
    assert svc.reinitialize(blocking=True) is False


def test_reinitialize_recreates_engine(monkeypatch):
    monkeypatch.setattr(tts_mod, "KokoroTTS", lambda **kw: _FakeEngine(fell_back=True, **kw))
    svc = get_tts_service()
    svc.initialize(backend="kokoro", blocking=True, device="cuda")
    first = svc._engine
    assert svc.reinitialize(blocking=True) is True
    assert svc._engine is not None
    assert svc._engine is not first        # a fresh engine instance
    assert svc.state == TTSState.FALLBACK

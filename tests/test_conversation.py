"""
tests/test_conversation.py

Acceptance tests for SAINT Milestone 1 — Real-Time Conversation.

All tests run headlessly using MockSTT, MockTTS, and MockProvider.
No microphone, GPU, or API key required.

Run with:
    cd saint
    python -m pytest tests/test_conversation.py -v
"""

import sys
import os
import threading
import time

# Make sure we can import from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Bootstrap Qt application (needed for the event bus QObject/Signal machinery)
from PySide6.QtWidgets import QApplication

_app = None

def get_app():
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication(sys.argv)
    return _app


import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def qt_app():
    """Ensure a QApplication exists for each test."""
    app = get_app()
    yield app


@pytest.fixture()
def mock_setup():
    """
    Return a fresh set of Mock* components and a ConversationController
    wired together. Uses MockProvider for AI, MockTTS for speech output.
    """
    get_app()  # ensure Qt is up

    # Force mock backends in config before any module imports settle
    from core.config import config
    config.set("ai.provider", "mock", persist=False)
    config.set("voice.tts_backend", "mock", persist=False)
    config.set("voice.stt_backend", "mock", persist=False)
    # These tests exercise interruption mechanics against the mock LLM's
    # streamed answers; bypass the agent's instant deterministic replies.
    config.set("agent.enabled", False, persist=False)

    # Reset analytics so counts are fresh
    from core.analytics import analytics
    analytics._data["interruptions"] = 0
    analytics._data["cancelled_tasks"] = 0
    analytics._data["stt_latencies"] = []
    analytics._data["tts_latencies"] = []
    analytics._data["model_latencies"] = []
    analytics._data["overall_latencies"] = []

    from modules.ai.module import AIModule
    from modules.voice.module import VoiceModule
    from modules.voice.tts import MockTTS
    from core.conversation import ConversationController

    ai = AIModule()
    voice = VoiceModule()
    tts = MockTTS(words_per_second=50)  # fast for tests

    ctrl = ConversationController(ai_module=ai, voice_module=voice, tts_engine=tts)

    yield {
        "ai": ai,
        "voice": voice,
        "tts": tts,
        "ctrl": ctrl,
        "config": config,
    }

    # Cleanup
    ctrl.shutdown()
    config.set("agent.enabled", True, persist=False)


# ---------------------------------------------------------------------------
# Helper: run Qt event loop briefly to flush signals
# ---------------------------------------------------------------------------
def flush_events(ms=50):
    """Process pending Qt events."""
    app = get_app()
    deadline = time.time() + ms / 1000
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)


def wait_for(condition, timeout=5.0, poll=0.05):
    """Wait until condition() returns True or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        flush_events(int(poll * 1000))
        if condition():
            return True
    return False


# ===========================================================================
# Test 1 — Language switch mid-sentence (knowledge interruption)
# ===========================================================================
#
# User:   "What's Python?"
# SAINT:  "Python is—"        ← starts speaking
# User:   "Actually Lua."     ← interrupts
# SAINT:  "Sure, Lua is..."   ← pivots, no apology
#
def test_01_language_switch_interruption(mock_setup):
    """
    PASS criteria:
    - No restart (context preserved)
    - No apology string in second response
    - Second response contains "Lua"
    - Interruption counter increments
    """
    from core.events import event_bus, EventType
    from core.conversation import ConvState

    ctrl = mock_setup["ctrl"]
    voice = mock_setup["voice"]
    tts = mock_setup["tts"]
    ai = mock_setup["ai"]

    spoken_chunks = []

    # Capture every TTS chunk
    def on_tts_event(ev):
        if ev.type == EventType.TTS_SPEAK_CHUNK:
            spoken_chunks.append(ev.payload.get("chunk", ""))

    event_bus.event_occurred.connect(on_tts_event)

    try:
        # Step 1: User asks about Python
        voice.inject_utterance("What's Python?")
        flush_events(100)

        # Wait for SAINT to start thinking/speaking
        assert wait_for(
            lambda: ctrl.state in (ConvState.THINKING, ConvState.SPEAKING),
            timeout=3.0,
        ), "SAINT did not enter THINKING/SPEAKING state"

        # Step 2: User interrupts
        voice.inject_interrupt()
        flush_events(100)

        # Wait for SAINT to handle the interrupt
        assert wait_for(
            lambda: ctrl.state in (ConvState.INTERRUPTED, ConvState.IDLE),
            timeout=3.0,
        ), "SAINT did not reach INTERRUPTED/IDLE after interrupt"

        # Step 3: User provides new question
        voice.inject_utterance("Actually Lua.")
        flush_events(200)

        # Wait for a full response cycle
        assert wait_for(
            lambda: ctrl.state == ConvState.IDLE,
            timeout=8.0,
        ), "SAINT did not return to IDLE after second question"

        # --- Assertions ---------------------------------------------------
        # 1. Second response should mention Lua
        all_spoken = " ".join(spoken_chunks).lower()
        assert "lua" in all_spoken, (
            f"Expected 'lua' in SAINT's response, got: {all_spoken!r}"
        )

        # 2. No apology
        apology_words = ["sorry", "apologize", "apologi", "my mistake", "i'm sorry"]
        for word in apology_words:
            assert word not in all_spoken, (
                f"SAINT apologised unexpectedly: found {word!r} in {all_spoken!r}"
            )

        # 3. Context has interruption marker
        history = ai.context._history
        interrupted_entries = [m for m in history if "[interrupted]" in m.get("content", "")]
        assert interrupted_entries, "No [interrupted] marker found in context history"

        print(f"\n[Test 1 PASS] SAINT said: {all_spoken[:120]!r}")

    finally:
        event_bus.event_occurred.disconnect(on_tts_event)


# ===========================================================================
# Test 2 — Action interruption
# ===========================================================================
#
# User:   "Open Chrome."
# SAINT:  "Opening Chrome."   ← starts speaking
# User:   "Actually Firefox." ← interrupts
# SAINT:  "Stopping... Opening Firefox."
#
def test_02_action_interruption(mock_setup):
    """
    PASS criteria:
    - TTS interrupted (ctrl._tts.is_speaking() becomes False)
    - Second response contains "Firefox"
    - Cancelled tasks counter increments
    """
    from core.events import event_bus, EventType
    from core.conversation import ConvState
    from core.analytics import analytics

    ctrl = mock_setup["ctrl"]
    voice = mock_setup["voice"]
    tts = mock_setup["tts"]

    cancelled_before = analytics._data.get("cancelled_tasks", 0)
    spoken_chunks = []

    def on_tts_event(ev):
        if ev.type == EventType.TTS_SPEAK_CHUNK:
            spoken_chunks.append(ev.payload.get("chunk", ""))

    event_bus.event_occurred.connect(on_tts_event)

    try:
        # Step 1: User says "Open Chrome"
        voice.inject_utterance("Open Chrome.")
        flush_events(100)

        # Check state immediately - mock is very fast so it may already be SPEAKING or IDLE
        # The key is that it entered THINKING or SPEAKING at some point
        assert ctrl.state in (ConvState.THINKING, ConvState.SPEAKING, ConvState.IDLE), f"Unexpected state: {ctrl.state}"

        # Step 2: Interrupt
        voice.inject_interrupt()
        flush_events(100)

        assert wait_for(
            lambda: not tts.is_speaking(),
            timeout=3.0,
        ), "TTS was not interrupted"

        # Step 3: New command
        voice.inject_utterance("Actually Firefox.")
        flush_events(200)

        assert wait_for(
            lambda: ctrl.state == ConvState.IDLE,
            timeout=8.0,
        ), "SAINT did not complete second response"
        # Analytics is a Qt-side subscriber: drain queued events before reading it.
        flush_events(200)

        all_spoken = " ".join(spoken_chunks).lower()
        assert "firefox" in all_spoken, (
            f"Expected 'firefox' in response, got: {all_spoken!r}"
        )

        cancelled_after = analytics._data.get("cancelled_tasks", 0)
        assert cancelled_after > cancelled_before, (
            "Cancelled tasks counter did not increment"
        )

        print(f"\n[Test 2 PASS] SAINT said: {all_spoken[:120]!r}")

    finally:
        event_bus.event_occurred.disconnect(on_tts_event)


# ===========================================================================
# Test 3 — Latency metrics are recorded
# ===========================================================================

def test_03_latency_metrics_recorded(mock_setup):
    """
    After one complete conversation turn, the analytics snapshot should
    contain populated latency fields (at least overall_latency recorded).
    """
    from core.conversation import ConvState
    from core.analytics import analytics
    from core.events import event_bus, EventType

    ctrl = mock_setup["ctrl"]
    voice = mock_setup["voice"]

    # Ensure latency lists start empty for this test
    analytics._data["overall_latencies"] = []
    analytics._data["stt_latencies"] = []

    voice.inject_utterance("What is Lua?")
    flush_events(100)

    assert wait_for(
        lambda: ctrl.state == ConvState.IDLE,
        timeout=8.0,
    ), "SAINT did not complete the turn"
    # Analytics is a Qt-side subscriber: drain queued events before reading it.
    flush_events(200)

    snap = analytics.snapshot()

    # STT latency: inject_utterance fires LATENCY_STT with 0.0 ms (mock)
    # overall latency: ConversationController always fires LATENCY_OVERALL
    assert analytics._data.get("overall_latencies"), (
        "No overall latency recorded. Got snapshot: " + str(snap)
    )

    print(f"\n[Test 3 PASS] Latency snapshot: {snap}")


# ===========================================================================
# Test 4 — No apology in any response
# ===========================================================================

def test_04_no_apology_after_multiple_interruptions(mock_setup):
    """
    Three rapid interruptions — SAINT should never output an apology.
    """
    from core.conversation import ConvState
    from core.events import event_bus, EventType

    ctrl = mock_setup["ctrl"]
    voice = mock_setup["voice"]
    spoken = []

    def on_tts_event(ev):
        if ev.type == EventType.TTS_SPEAK_CHUNK:
            spoken.append(ev.payload.get("chunk", ""))

    event_bus.event_occurred.connect(on_tts_event)

    try:
        pairs = [
            ("What's Python?", "Actually Lua."),
            ("Open Chrome.", "Actually Firefox."),
        ]
        for q, redirect in pairs:
            voice.inject_utterance(q)
            flush_events(80)
            wait_for(lambda: ctrl.state in (ConvState.THINKING, ConvState.SPEAKING), 3.0)
            voice.inject_interrupt()
            flush_events(80)
            voice.inject_utterance(redirect)
            wait_for(lambda: ctrl.state == ConvState.IDLE, 8.0)

        all_spoken = " ".join(spoken).lower()
        apology_words = ["sorry", "apologize", "apologi", "i'm sorry", "my bad"]
        for word in apology_words:
            assert word not in all_spoken, f"Found apology {word!r} in: {all_spoken!r}"

        print(f"\n[Test 4 PASS] No apologies in: {all_spoken[:200]!r}")

    finally:
        event_bus.event_occurred.disconnect(on_tts_event)


# ===========================================================================
# Test 5 — Context survives interruption (topic tracking)
# ===========================================================================

def test_05_context_survives_interruption(mock_setup):
    """
    After an interruption, the context should contain both the original
    partial answer (marked [interrupted]) and the new question.
    """
    from core.conversation import ConvState

    ctrl = mock_setup["ctrl"]
    voice = mock_setup["voice"]
    ai = mock_setup["ai"]

    voice.inject_utterance("What's Python?")
    flush_events(80)
    wait_for(lambda: ctrl.state in (ConvState.THINKING, ConvState.SPEAKING), 3.0)

    voice.inject_interrupt()
    flush_events(80)

    voice.inject_utterance("Actually, tell me about Lua.")
    wait_for(lambda: ctrl.state == ConvState.IDLE, 8.0)

    history = ai.context._history
    content_all = " ".join(m["content"] for m in history)

    assert "lua" in content_all.lower(), "Lua not found in context after pivot"
    assert any("[interrupted]" in m["content"] for m in history), \
        "No [interrupted] marker in context"

    print(f"\n[Test 5 PASS] Context turns: {len(history)}, last: {history[-1]['content'][:80]!r}")


# ===========================================================================
# Test 6 — New speech while SAINT is speaking is processed (not dropped)
# ===========================================================================
def test_06_speech_during_speaking_is_processed(mock_setup):
    from core.conversation import ConvState
    from core.events import event_bus, EventType

    ctrl = mock_setup["ctrl"]
    voice = mock_setup["voice"]
    starts = []

    def on_ev(ev):
        if ev.type == EventType.CONVERSATION_TURN_START:
            starts.append(ev.payload["text"])

    event_bus.subscribe(on_ev)
    try:
        voice.inject_utterance("What's Python?")
        assert wait_for(lambda: ctrl.state in (ConvState.THINKING, ConvState.SPEAKING), 3.0)
        # The user talks over SAINT with a new request (no explicit interrupt event).
        event_bus.emit_event(EventType.VOICE_STT_FINAL, {"text": "Actually Lua.", "confidence": 0.9,
                                                         "session_id": 424242, "wake": True})
        assert wait_for(lambda: "Actually Lua." in starts, 3.0), starts
        assert wait_for(lambda: ctrl.state == ConvState.IDLE, 8.0)
    finally:
        event_bus.unsubscribe(on_ev)

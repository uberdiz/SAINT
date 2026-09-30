"""One-word answers ("Yes.", "Stop.") reach SAINT when it's listening for them.

Logged 2026-09-25/28: the reply to "Do you want me to close Disk Cleanup?"
was dropped before speech recognition as ``insufficient_speech`` with 4–8
voiced frames at normal speaking volume.
"""

import time

import pytest

from core import dialog
from core.config import config
from modules.agent.confirm import PendingAction, classify_reply, confirmations
from modules.voice.module import ListenPhase
from tests.test_wake_and_voice import _finals, _run, _silence, _speech, _voice, _wait


@pytest.fixture(autouse=True)
def _quiet():
    config.set("voice.wake_word_chime", False, persist=False)
    yield
    confirmations.clear("test")


def _command(v, reason):
    v._enter_command(20.0, reason=reason)


def test_a_one_word_reply_to_a_question_is_transcribed():
    finals, done = _finals()
    try:
        confirmations.ask(PendingAction("close Disk Cleanup", lambda: "Closed."))
        v = _voice("Yes.")
        _command(v, "awaiting reply")
        _run(v, _silence(5) + _speech(5) + _silence(30))     # 150 ms "yes"
        _wait(lambda: finals, 2.0)
        assert finals and finals[0]["text"] == "Yes."
    finally:
        done()


def test_a_short_command_in_a_follow_up_window_is_transcribed():
    v = _voice("Pause.")
    _command(v, "wake word")
    _run(v, _silence(5) + _speech(5) + _silence(40))
    _wait(lambda: v._stt.calls >= 1, 2.0)
    assert v._stt.calls == 1


def test_a_click_is_still_dropped():
    v = _voice("Yes.")
    _command(v, "awaiting reply")
    _run(v, _silence(5) + _speech(1) + _silence(40))       # 30 ms: a click, not a word
    time.sleep(0.3)
    assert v._stt.calls == 0


def test_short_speech_while_idle_still_needs_the_wake_word():
    finals, done = _finals()
    try:
        v = _voice("Yes.")
        _run(v, _silence(5) + _speech(12) + _silence(40))
        time.sleep(0.3)
        assert not finals and v.phase == ListenPhase.WAKE
    finally:
        done()


def test_voiced_ratio_ignores_preroll_and_trailing_silence():
    v = _voice("")
    frames = _silence(20) + _speech(5) + _silence(33)
    assert not v._validate_speech_session(frames, 5, 4, 0.005)            # the old check
    assert v._validate_speech_session(frames, 5, 4, 0.005, pad=53)


def test_reply_window_lasts_as_long_as_the_question():
    confirmations.ask(PendingAction("close Discord", lambda: "Closed."))
    assert dialog.expects_short_answer()
    assert dialog.reply_window(8.0) > 8.0
    confirmations.clear("test")
    assert dialog.current() is None or dialog.current().kind == "reply"


@pytest.mark.parametrize("text,answer", [
    ("Yes.", True), ("Uh, yeah.", True), ("Um yes please", True), ("Alright.", True), ("Mm-hmm.", True),
    ("Go ahead", True), ("No.", False), ("Not yet.", False), ("Hold on", False), ("Uh no", False),
    ("Right click the button", None), ("Please close Discord", None), ("Let's go to settings", None),
])
def test_classify_reply(text, answer):
    assert classify_reply(text) is answer


def _spotify_available(monkeypatch):
    from core.module_manager import module_manager
    sp = module_manager.get("spotify")
    monkeypatch.setattr(sp, "availability", lambda: (True, ""))


def test_spotify_search_results_can_be_picked_by_position(monkeypatch):
    from modules.agent import router
    from modules.agent.agent import agent
    from modules.agent.confirm import choices

    played = []

    class R:
        def __init__(self, result=None):
            self.success, self.result, self.error, self.error_code = True, result, "", None

    def fake_call(tool, **kw):
        if tool == "spotify.search":
            return R({"query": kw.get("query"), "tracks": {"items": [
                {"name": "HUMBLE.", "uri": "spotify:track:1", "artists": [{"name": "Kendrick Lamar"}]},
                {"name": "DNA.", "uri": "spotify:track:2", "artists": [{"name": "Kendrick Lamar"}]},
                {"name": "Alright", "uri": "spotify:track:3", "artists": [{"name": "Kendrick Lamar"}]}]},
                "artists": {"items": []}, "albums": {"items": []}, "playlists": {"items": []}})
        if tool == "spotify.play":
            played.append(kw.get("uri"))
            return R({})
        return R({})

    _spotify_available(monkeypatch)
    monkeypatch.setattr(router, "call", fake_call)
    reply = router.route("search spotify for Kendrick Lamar").run()
    assert reply.expects_reply and choices.pending is not None
    res = agent.handle("play the second one")
    assert played == ["spotify:track:2"] and "DNA." in res.text
    assert choices.pending is None and confirmations.pending is None


def test_no_to_a_search_offer_drops_the_list_too(monkeypatch):
    from modules.agent import router
    from modules.agent.agent import agent
    from modules.agent.confirm import choices

    class R:
        success, error, error_code = True, "", None
        result = {"query": "x", "tracks": {"items": [{"name": "A", "uri": "u:a"}, {"name": "B", "uri": "u:b"}]},
                  "artists": {"items": []}, "albums": {"items": []}, "playlists": {"items": []}}

    _spotify_available(monkeypatch)
    monkeypatch.setattr(router, "call", lambda tool, **kw: R())
    router.route("search spotify for x").run()
    agent.handle("no")
    assert choices.pending is None and confirmations.pending is None


def test_addressed_speech_that_is_dropped_says_so():
    from core.assistant_state import assistant_state
    v = _voice("Yes.")
    _command(v, "wake word")
    v._end_segment(_silence(20) + _speech(1) + _silence(20), 2, 1, 10, 0.5, False, "wake word", trailing=20)
    snap = assistant_state.snapshot()
    assert snap["detail"] == "didn't catch that" and snap["label"].startswith("Didn't catch that")


def test_room_chatter_in_a_follow_up_window_stays_silent():
    from core.assistant_state import assistant_state
    v = _voice("")
    _command(v, "follow-up")
    assistant_state.set(assistant_state.state, "follow-up")
    v._end_segment(_silence(20) + _speech(1) + _silence(20), 2, 1, 10, 0.5, False, "follow-up", trailing=20)
    assert assistant_state.snapshot()["detail"] != "didn't catch that"

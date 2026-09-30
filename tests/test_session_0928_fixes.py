"""Regressions from the 2026-09-28 evening session (Rocket League / YouTube / Spotify)."""

import pytest

from core.audio_echo import EchoGate
from modules.agent.confirm import PendingAction, confirmations
from modules.agent.router import route


@pytest.fixture(autouse=True)
def _clean():
    yield
    confirmations.clear("test")


@pytest.mark.parametrize("text,intent", [
    ("Open YouTube on my main screen.", "composite:browser.open_url+desktop.move_window"),
    ("and also open youtube on the right screen", "composite:browser.open_url+desktop.move_window"),
    ("press play on Spotify.", "spotify.resume"),
    ("hit play", "spotify.resume"),
    ("play my Spotify", "spotify.resume"),
    ("play my playlist", "spotify.choose_playlist"),
    ("play my gym playlist", "spotify.play_playlist"),
    ("Close that notification.", "notifications.clear"),
    ("close notepad", "desktop.close_app"),
])
def test_routing(text, intent):
    i = route(text)
    assert i is not None and i.name == intent


def test_planner_never_closes_something_that_was_not_named():
    from modules.learning.planner import grounded
    assert not grounded("close Claude", "Close that notification.")
    assert grounded("close THE FINALS", "close the finers")          # a mishearing is still fixed


def test_a_question_talked_past_still_takes_a_plain_yes():
    ran = []
    confirmations.ask(PendingAction("close Rocket League", lambda: ran.append(1) or "Closed."))
    assert confirmations.resolve("and also open youtube on the right screen") is None
    assert confirmations.pending is None and confirmations.can_answer("Yes.")
    assert not confirmations.can_answer("that was fun")
    assert confirmations.resolve("Yes.") == "Closed." and ran == [1]
    assert not confirmations.can_answer("yes")                        # answered: gone


def test_a_set_aside_question_expires(monkeypatch):
    confirmations.ask(PendingAction("close Discord", lambda: "Closed."))
    confirmations.resolve("play some music")
    monkeypatch.setattr(confirmations, "SET_ASIDE_SEC", 0.0)
    assert confirmations.resolve("yes") is None


def test_quiet_noise_while_saint_is_thinking_does_not_interrupt():
    gate = EchoGate(margin=2.5, min_ms=240, frame_ms=30, initial_coupling=0.3)
    assert not any(gate.update(0.012, 0.0) for _ in range(40))       # logged phantom level
    gate.reset_run()
    assert any(gate.update(0.12, 0.0) for _ in range(20))            # really talking over it


def test_follow_up_requests_the_router_does_not_know_still_reach_saint():
    from modules.voice.module import VoiceModule
    v = VoiceModule()
    ok, _ = v._passes_activation_gate("Turn on auto scroll.", 0.5, follow_up=True)
    assert ok
    ok, reason = v._passes_activation_gate("That was a crazy round, bro.", 0.6, follow_up=True)
    assert not ok and reason == "followup_no_intent"


def test_a_short_yes_to_a_set_aside_question_passes_the_gate():
    from modules.voice.module import VoiceModule
    confirmations.ask(PendingAction("close Rocket League", lambda: "Closed."))
    confirmations.resolve("open youtube")
    v = VoiceModule()
    assert v._passes_activation_gate("Yes.", 0.05, follow_up=True)[0]


def test_no_chime_after_saint_asks_by_default():
    from core.config import config
    assert config.get("voice.reply_chime", False) is False


def test_media_keys_work_during_game_mode_without_switching_windows(monkeypatch):
    import pyautogui
    from core import game_mode as gm
    from modules.desktop import controller
    pressed = []
    monkeypatch.setattr(controller, "_require", lambda *a: None)
    monkeypatch.setattr(gm.game_mode, "refuse_input", lambda: "the game is in front")
    monkeypatch.setattr(controller.desktop, "_input_target", lambda: pytest.fail("switched windows"))
    monkeypatch.setattr(pyautogui, "press", lambda k: pressed.append(k))
    assert controller.desktop.press_keys("playpause") == {"keys": "playpause"} and pressed == ["playpause"]


def test_window_question_is_short():
    from modules.agent.desktop_intents import _short_title
    assert _short_title("when a police movie is too realistic - YouTube") == "YouTube: when a police movie"
    assert _short_title("uberdiz/SAINT: SAINT") == "uberdiz/SAINT"


def test_open_youtube_switches_to_the_youtube_window(monkeypatch):
    from modules.desktop import browser
    from modules.desktop.controller import desktop

    class W:
        def __init__(self, hwnd, title):
            self.hwnd, self.title, self.minimized, self.foreground, self.monitor = hwnd, title, False, False, 2

    wins = [W(1, "uberdiz/SAINT: SAINT - Opera"), W(2, "when a police movie is too realistic - YouTube - Opera")]
    monkeypatch.setattr(desktop, "app_windows", lambda kind: wins)
    monkeypatch.setattr(desktop, "_activate", lambda w: w)
    monkeypatch.setattr(desktop, "_note", lambda w: None)
    monkeypatch.setattr(desktop, "press_keys", lambda k: pytest.fail("navigated away from the video"))
    monkeypatch.setattr("modules.desktop.controller._require", lambda *a: None)
    r = browser.open_url("https://www.youtube.com")
    assert r["already_open"] and r["window"].startswith("when a police movie")


def test_llm_tool_narration_is_not_spoken():
    from modules.agent.output import clean_reply
    out = clean_reply("Based on the provided functions, it seems like you want to open YouTube. "
                      "However, there is no direct function to open an application on a specific screen.")
    assert out == ""

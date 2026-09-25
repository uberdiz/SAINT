"""Requests that went wrong in the live logs (2026-09-23/24), and voice
control of SAINT's own window."""

import pytest

from modules.agent.router import route, _is_key_combo


@pytest.fixture(autouse=True)
def _no_youtube(monkeypatch):
    from modules.desktop import youtube
    monkeypatch.setattr(youtube, "is_watching", lambda *a, **k: False)
    from modules.agent.context import desktop_context
    desktop_context.note_domain("")


def _name(text):
    it = route(text)
    return it and it.name


# ---------------------------------------------------------------- SAINT's own window
@pytest.mark.parametrize("text,intent", [
    ("No, wait, turn off the SAINT mini player.", "ui.mini_player"),
    ("turn off the mini player", "ui.mini_player"),
    ("show the mini player", "ui.mini_player"),
    ("mini player", "ui.mini_player"),
    ("open the dashboard", "ui.navigate"),
    ("go to history", "ui.navigate"),
    ("go to settings", "ui.navigate"),
    ("open SAINT settings", "ui.navigate"),
    ("show me the history page", "ui.navigate"),
    ("take me to automations", "ui.navigate"),
    ("hide the halo", "ui.halo"),
    ("halo always on", "ui.halo"),
    ("open the overlay", "ui.overlay"),
    ("turn off action notices", "ui.action_notices"),
    ("dark mode", "ui.theme"),
    ("switch to light theme", "ui.theme"),
    ("minimize yourself", "ui.window"),
    ("Minimize SAINT.", "ui.window"),
])
def test_saint_ui_phrases(text, intent):
    assert _name(text) == intent, (text, _name(text))


def test_youtube_keeps_its_miniplayer(monkeypatch):
    from modules.desktop import youtube
    assert _name("put the youtube video in the mini player") == "youtube.miniplayer"
    monkeypatch.setattr(youtube, "is_watching", lambda *a, **k: True)
    assert _name("mini player") == "youtube.miniplayer"
    assert _name("turn off your mini player") == "ui.mini_player"


def test_open_settings_is_still_windows_settings():
    assert _name("open settings") == "desktop.open_app"


def test_ui_tool_reports_missing_window():
    from core.ui_link import ui_link
    from modules.automation.tools import get_tool_registry
    was = ui_link.attached
    ui_link.attached = False
    try:
        res = get_tool_registry().execute("ui.navigate", page="dashboard")
        assert not res.success and res.error_code == "UNAVAILABLE"
    finally:
        ui_link.attached = was


def test_ui_link_round_trip():
    import threading
    from core.events import event_bus, EventType
    from core.ui_link import UILink
    link = UILink()
    link.attached = True
    seen = []

    def handler(ev):
        if ev.type == EventType.UI_COMMAND:
            seen.append(ev.payload)
            threading.Timer(0.05, lambda: link.ack(ev.payload["id"], True, "done")).start()
    event_bus.subscribe(handler)
    try:
        assert link.send("navigate", page="History") == (True, "done")
    finally:
        event_bus.unsubscribe(handler)
    assert seen[0]["cmd"] == "navigate" and seen[0]["args"] == {"page": "History"}


def test_page_names():
    from modules.ui_control.tools import page_for
    assert page_for("dashboard") == "Home"
    assert page_for("stats") == "History"
    assert page_for("Settings") == "Settings"


# ---------------------------------------------------------------- desktop phrasing from the logs
def test_press_key_with_a_purpose():
    assert _name("Press F to full screen.") == "desktop.press_keys"
    assert _is_key_combo("f") and _is_key_combo("ctrl+shift+t") and _is_key_combo("alt f4")
    assert _is_key_combo("page down") and _is_key_combo("f11")
    assert not _is_key_combo("f to full screen") and not _is_key_combo("to")


def test_a_whole_request_is_never_a_window_name():
    it = route("Go to my Downloads folder, click the first download, and extract it using WinRAR to my games folder.")
    assert it is None or it.name != "desktop.focus_window"


def test_focus_on_a_monitor():
    assert _name("Go to my browser on my right screen") == "desktop.focus_window"


@pytest.mark.parametrize("text,intent", [
    ("clear everything off the screen but you too.", "desktop.minimize_others"),
    ("Clear everything off the screen but YouTube", "desktop.minimize_others"),
    ("clear everything off the screen", "desktop.show_desktop"),
    ("clear my screen", "desktop.show_desktop"),
])
def test_clear_the_screen(text, intent):
    assert _name(text) == intent


def test_normal_speed_wording():
    from modules.desktop import youtube
    assert youtube.parse("put it back at one time speed", youtube_context=True) == ("speed", 1.0)
    assert youtube.parse("back to normal speed", youtube_context=True) == ("speed", 1.0)


# ---------------------------------------------------------------- replies name the right thing
@pytest.mark.parametrize("result,label", [
    ({"title": "BREAK PIGGY BANK = MAKE MONEY - YouTube - Opera", "process": "opera.exe"}, "YouTube"),
    ({"title": "Otis", "process": "Spotify.exe"}, "Spotify"),
    ({"title": "Speed Dial - Opera", "process": "opera.exe"}, "Speed Dial"),
    ({"title": "Monkeytype | A minimalistic typing test - Opera", "process": "opera.exe"}, "Monkeytype"),
    ({"title": "MANUAL_TESTING.md - SAINT-v02 - Visual Studio Code", "process": "Code.exe"}, "VS Code"),
    ({"window": "Steam", "app": "Steam"}, "Steam"),
])
def test_app_label(result, label):
    from modules.vision.screen import app_label
    assert app_label(result) == label


# ---------------------------------------------------------------- choosing among browser windows
def test_smart_policy_picks_the_window_you_used_last(monkeypatch):
    import time
    from modules.desktop.focus_history import focus_history, smart_pick
    W = type("W", (), {})
    a, b, c = W(), W(), W()
    a.hwnd, b.hwnd, c.hwnd = 101, 102, 103
    now = time.time()
    focus_history.note(101, now - 3000)      # long ago
    focus_history.note(102, now - 20)
    focus_history.note(103, now - 5)         # most recent, but minimized
    assert smart_pick([a, b, c], visible=[a, b]) is b
    assert smart_pick([a, c], visible=[a]) is c
    assert smart_pick([a], visible=[a]) is None


# ---------------------------------------------------------------- AI model tags
def test_untagged_model_matches_latest(monkeypatch):
    from modules.ai import module as ai_mod

    class P:
        def list_models(self, base_url):
            return [{"name": "llama3.1:latest", "size": 4}, {"name": "llama3.2:1b", "size": 1}]
    monkeypatch.setattr(ai_mod, "get_provider", lambda name: P())
    m = ai_mod.AIModule.__new__(ai_mod.AIModule)
    assert m._resolve_model("ollama", "", "llama3.1") == ("llama3.1:latest", None)
    model, info = m._resolve_model("ollama", "", "llama3")
    assert model == "llama3.1:latest" and info["configured"] == "llama3"


def test_config_v7_migration():
    from core.config import _migrate
    data = _migrate({"config_version": 6, "ai": {"model": "llama3"}, "desktop": {"multi_window_policy": "ask"}})
    assert data["config_version"] == 7 and data["ai"]["model"] == "llama3.1"
    assert data["desktop"]["multi_window_policy"] == "smart"
    kept = _migrate({"config_version": 6, "ai": {"model": "mistral"}, "desktop": {"multi_window_policy": "recent"}})
    assert kept["ai"]["model"] == "mistral" and kept["desktop"]["multi_window_policy"] == "recent"


def test_phonemizer_noise_is_filtered():
    import logging
    from core.logger import _DropKnownNoise
    rec = logging.LogRecord("phonemizer", logging.WARNING, "", 0, "words count mismatch on 100.0%% of the lines (1/1)",
                            None, None)
    assert not _DropKnownNoise().filter(rec)

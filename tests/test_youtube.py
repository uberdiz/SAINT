"""YouTube control: requests map to the player's own shortcuts and menus."""

import pytest

from modules.desktop import youtube


@pytest.mark.parametrize("text,expected", [
    ("theater mode", ("theater", None)),
    ("turn on captions", ("captions_on", None)),
    ("turn the subtitles off", ("captions_off", None)),
    ("set the quality to 1080p", ("quality", "1080p")),
    ("watch in 4k", ("quality", "4k")),
    ("lower the quality", ("quality", "lowest")),
    ("turn off autoplay", ("autoplay_off", None)),
    ("loop this video", ("loop_on", None)),
    ("stop looping", ("loop_off", None)),
    ("next video", ("next_video", None)),
    ("skip the ad", ("skip_ad", None)),
    ("sleep timer 30 minutes", ("sleep_timer", "30")),
    ("next chapter", ("next_chapter", None)),
    ("mini player", ("miniplayer", None)),
])
def test_unambiguous_requests(text, expected):
    assert youtube.parse(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("pause", ("pause", None)),
    ("fullscreen", ("fullscreen", None)),
    ("faster", ("speed_up", None)),
    ("slow down", ("speed_down", None)),
    ("play at 2x", ("speed", 2.0)),
    ("normal speed", ("speed", 1.0)),
    ("set speed to 1.25", ("speed", 1.25)),
    ("skip ahead 30 seconds", ("seek_seconds", 30.0)),
    ("go back 10 seconds", ("seek_seconds", -10.0)),
    ("fast forward a minute", ("seek_seconds", 60)),
    ("skip to 50 percent", ("seek_percent", 50.0)),
    ("mute", ("mute", None)),
    ("restart the video", ("restart", None)),
])
def test_bare_commands_mean_the_video_while_watching(text, expected):
    assert youtube.parse(text, youtube_context=True) == expected


@pytest.mark.parametrize("text", ["pause", "faster", "mute", "can you repeat that", "what time is it",
                                  "play some music", "what does the caption say"])
def test_not_hijacked_without_youtube(text):
    assert youtube.parse(text) is None


def test_repeat_that_is_not_loop_even_while_watching():
    assert youtube.parse("can you repeat that", youtube_context=True) is None


@pytest.fixture
def pressed(monkeypatch):
    keys = []
    win = type("W", (), {"title": "Some video - YouTube - Opera", "hwnd": 7})()
    monkeypatch.setattr(youtube, "_focus_page", lambda w: w)
    monkeypatch.setattr(youtube, "youtube_window", lambda required=True: win)
    monkeypatch.setattr(youtube, "_press", lambda k, times=1, interval=0.06: keys.extend([k] * times))
    from modules.desktop import controller
    monkeypatch.setattr(controller, "_require", lambda *a: None)
    return keys


def test_shortcut_keys(pressed):
    assert youtube.run("fullscreen")["keys"] == "f"
    assert youtube.run("theater")["keys"] == "t"
    assert youtube.run("speed_up")["keys"] == "shift+."
    assert pressed == ["f", "t", "shift+."]


def test_exact_speed_steps_down_then_up(pressed):
    r = youtube.run("speed", 1.5)
    assert r["speed"] == 1.5
    assert pressed.count("shift+,") == 12 and pressed.count("shift+.") == youtube.SPEEDS.index(1.5)


def test_seek_uses_ten_and_five_second_jumps(pressed):
    youtube.run("seek_seconds", 35)
    assert pressed == ["l", "l", "l", "right"]
    pressed.clear()
    youtube.run("seek_seconds", -20)
    assert pressed == ["j", "j"]
    pressed.clear()
    youtube.run("seek_percent", 47)
    assert pressed == ["4"]


def test_router_sends_youtube_requests_to_the_tool():
    from modules.agent.router import route
    assert route("theater mode").name == "youtube.theater"
    assert route("set the video quality to 720p").name == "youtube.quality"
    assert route("play some lofi").name.startswith("spotify.")

"""Natural-language understanding: many phrasings -> the right action, and
action phrases are never mistaken for song titles."""

import pytest

from modules.agent.context import desktop_context
from modules.agent.router import route, spotify_intent


@pytest.fixture(autouse=True)
def _fresh_context():
    desktop_context.clear()
    yield
    desktop_context.clear()


# ---------------------------------------------------------------- Spotify
@pytest.mark.parametrize("text", [
    "Play something else.", "Play a different song.", "Give me another song.", "Next.", "Skip this.",
    "Change the song.", "Put on something different.", "Skip.", "Skip the song.", "Skip this song.",
    "Switch songs.", "Play something different.", "Next track please", "SAINT, skip that one",
    "play the next song", "switch it up", "can you change the track",
])
def test_change_track_phrasings_skip(text):
    si = spotify_intent(text)
    assert si is not None and si.tool == "spotify.next", (text, si)


@pytest.mark.parametrize("text", ["I'm not feeling this one.", "I don't want to hear this song",
                                  "this song is boring", "nah, skip it"])
def test_rejections_skip_and_learn(text):
    si = spotify_intent(text)
    assert si is not None and si.tool == "spotify.next" and si.kind == "next_reject", (text, si)


def test_action_phrases_are_never_titles():
    for text in ("Play a different song.", "Play something else", "play another one"):
        si = spotify_intent(text)
        assert si.tool != "spotify.play_query", text


@pytest.mark.parametrize("text,seed", [
    ("Play something like Damn by Kendrick Lamar.", "Damn by Kendrick Lamar"),
    ("Place something like damn by Kendrick Lamar.", "damn by Kendrick Lamar"),     # STT slip
    ("put on songs similar to Blinding Lights", "Blinding Lights"),
    ("give me music like Daft Punk", "Daft Punk"),
])
def test_something_like_seed(text, seed):
    si = spotify_intent(text)
    assert si is not None and si.tool == "spotify.play_recommended", (text, si)
    assert si.kwargs["seed"].lower() == seed.lower()


def test_something_like_this_uses_current():
    si = spotify_intent("play something like this")
    assert si.kwargs == {"similar_to_current": True}


@pytest.mark.parametrize("text,tool,kw", [
    ("play something by Kendrick Lamar", "spotify.play_query", {"query": "kendrick lamar", "kind": "artist"}),
    ("Turn on shuffle.", "spotify.shuffle", {"state": True}),
    ("Turn on Smart Shuffle.", "spotify.smart_shuffle", {"state": True}),
    ("turn smart shuffle off", "spotify.smart_shuffle", {"state": False}),
    ("Pause.", "spotify.pause", {}),
    ("Resume.", "spotify.play", {}),
    ("replay this song", "spotify.replay", {}),
    ("start this song over", "spotify.replay", {}),
    ("skip ahead 30 seconds", "spotify.seek", {"seconds": 30, "relative": True}),
    ("go to 1:30", "spotify.seek", {"seconds": 90}),
    ("who is this", "spotify.current", {}),
    ("what album is this from", "spotify.current", {}),
    ("search spotify for Kendrick Lamar", "spotify.search", {"query": "kendrick lamar"}),
    ("play Kendrick Lamar", "spotify.play_query", {"query": "kendrick lamar", "kind": "auto"}),
    ("put on Kendrick Lamar", "spotify.play_query", {"query": "kendrick lamar", "kind": "auto"}),
    ("turn the music down", "spotify.volume_step", {"direction": "down"}),
])
def test_spotify_operations(text, tool, kw):
    si = spotify_intent(text)
    assert si is not None and si.tool == tool, (text, si)
    for k, v in kw.items():
        got = si.kwargs.get(k)
        assert (got.lower() if isinstance(got, str) else got) == v, (text, k, got)


def test_queue_removal_is_honest():
    si = spotify_intent("remove this song from the queue")
    assert si is not None and si.kind == "queue_remove"


# ---------------------------------------------------------------- context
def test_volume_follows_context():
    assert route("Turn the volume down.").name == "desktop.volume"          # no music context
    desktop_context.note_domain("spotify")
    assert route("Turn the volume down.").name == "spotify.volume_down"     # after a music command
    desktop_context.note_domain("browser")
    assert route("Turn the volume down.").name == "desktop.volume"          # after a video


def test_go_back_follows_context():
    assert route("Go back.").name == "desktop.keys"                          # browser back
    desktop_context.note_domain("spotify")
    assert route("go back").name == "spotify.previous"


def test_pause_while_watching_a_video_is_the_video():
    desktop_context.note_domain("browser")
    assert route("Pause.").name == "desktop.media"


def test_video_commands_are_not_spotify():
    for text in ("play the first video", "pause the video", "Click the first video."):
        assert spotify_intent(text) is None, text


# ---------------------------------------------------------------- desktop / screen / browser
@pytest.mark.parametrize("text,intent", [
    ("What's on my second screen?", "screen.monitor"),
    ("what's open on my other screen", "screen.monitor"),
    ("What's on my screen?", "screen.context"),
    ("What am I looking at?", "screen.context"),
    ("What's currently open?", "screen.open_windows"),
    ("What is this error?", "screen.explain"),
    ("Read what's on my screen.", "screen.read"),
    ("Find the settings button.", "screen.locate"),
    ("Where is the YouTube search box?", "screen.locate"),
    ("Click it.", "desktop.click_last"),
    ("Click the first video.", "desktop.click_result"),
    ("In that window, click the first video.", "desktop.click_in_window"),
    ("Click the button in the bottom right.", "desktop.click_region"),
    ("double click the recycle bin", "desktop.click_element"),
    ("right click the desktop icon", "desktop.click_element"),
    ("Scroll down.", "desktop.scroll"),
    ("scroll up a bit", "desktop.scroll"),
    ("Go back.", "desktop.keys"),
    ("refresh the page", "desktop.keys"),
    ("copy that", "desktop.keys"),
    ("select all", "desktop.keys"),
    ("put it in fullscreen", "desktop.fullscreen"),
    ("Move Spotify to my second monitor.", "desktop.move_window"),
    ("Move the browser to my second monitor.", "desktop.move_window"),
    ("Move this window to my other monitor.", "desktop.move_window"),
    ("Put Chrome on the left side.", "desktop.arrange_window"),
    ("Put it on the left.", "desktop.arrange_window"),
    ("Make this window bigger.", "desktop.scale_window"),
    ("Make it bigger.", "desktop.scale_window"),
    ("Put this window next to Spotify.", "desktop.place_beside"),
    ("Move that window over there.", "desktop.move_window"),
    ("Close the window I'm looking at.", "desktop.close_app"),
    ("Close that window.", "desktop.close_app"),
    ("Close it.", "desktop.close_app"),
    ("Close all the browser windows.", "desktop.close_windows"),
    ("Open Discord.", "desktop.open_app"),
    ("Open YouTube.", "browser.open_url"),
    ("go to youtube", "browser.open_url"),
    ("Search YouTube for Kendrick Lamar.", "browser.search"),
    ("look up Kendrick Lamar on youtube", "browser.search"),
    ("Open my browser.", "desktop.open_browser"),
    ("open a new browser window", "desktop.open_browser"),
    ("Turn the volume down.", "desktop.volume"),
])
def test_desktop_phrases(text, intent):
    it = route(text)
    assert it is not None and it.name == intent, (text, it and it.name)


@pytest.mark.parametrize("text,parts", [
    ("Open my browser and search YouTube.", ["desktop.open_browser", "browser.open_url"]),
    ("Open my browser, search YouTube for Kendrick Lamar, click the first video, and turn the volume down.",
     ["desktop.open_browser", "browser.search", "desktop.click_result", "desktop.volume"]),
    ("Open my browser, go to YouTube, search for Kendrick Lamar, click the first video, put it in fullscreen, "
     "and turn the volume down.",
     ["desktop.open_browser", "browser.open_url", "browser.search", "desktop.click_result", "desktop.fullscreen",
      "desktop.volume"]),
])
def test_multi_step_plans(text, parts):
    it = route(text)
    assert it is not None and it.name == "composite:" + "+".join(parts), (text, it and it.name)


@pytest.mark.parametrize("text", ["How tall is the Burj Khalifa?", "tell me a joke",
                                  "what is the capital of France", "explain how rainbows form"])
def test_questions_go_to_the_llm(text):
    assert route(text) is None

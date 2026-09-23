"""Intent routing: natural phrases map to the right real tool, and nothing else."""

import pytest

from modules.agent.router import spotify_intent, route, route_single, parse_desktop, parse_memory


@pytest.mark.parametrize("text,tool,kwargs", [
    ("SAINT, play Blinding Lights by The Weeknd", "spotify.play_query", {"query": "Blinding Lights by The Weeknd", "kind": "auto"}),
    ("play Daft Punk", "spotify.play_query", {"query": "Daft Punk", "kind": "auto"}),
    ("play some jazz", "spotify.play_query", {"query": "jazz", "kind": "genre"}),
    ("play some lofi music", "spotify.play_query", {"query": "lofi", "kind": "genre"}),
    ("play my gym playlist", "spotify.play_query", {"query": "gym", "kind": "playlist"}),
    ("play the album Discovery", "spotify.play_query", {"query": "Discovery", "kind": "album"}),
    ("play songs by Radiohead", "spotify.play_query", {"query": "Radiohead", "kind": "artist"}),
    ("pause", "spotify.pause", {}),
    ("resume", "spotify.play", {}),
    ("skip", "spotify.next", {}),
    ("go back", "spotify.previous", {}),
    ("turn it up", "spotify.volume_step", {"direction": "up"}),
    ("turn it down", "spotify.volume_step", {"direction": "down"}),
    ("set the volume to 30", "spotify.volume", {"percent": 30}),
    ("what am I listening to?", "spotify.current", {}),
    ("recommend something similar", "spotify.recommend", {"similar_to_current": True}),
    ("play something similar", "spotify.play_recommended", {"similar_to_current": True}),
    ("play something I like", "spotify.play_recommended", {"context": ""}),
    ("play my liked songs", "spotify.play_liked", {}),
    ("add this to my chill playlist", "spotify.add_current_to_playlist", {"playlist": "chill"}),
    ("queue Harder Better Faster Stronger", "spotify.queue", {"query": "harder better faster stronger"}),
    ("what have I listened to today", "spotify.history", {"period": "today"}),
])
def test_spotify_phrases(text, tool, kwargs):
    si = spotify_intent(text)
    assert si is not None, text
    assert si.tool == tool
    for k, v in kwargs.items():
        got = si.kwargs.get(k)
        assert (got.lower() if isinstance(got, str) else got) == (v.lower() if isinstance(v, str) else v), (k, got)


@pytest.mark.parametrize("text", [
    "what is the capital of France", "tell me a joke", "how are you today",
    "explain how rainbows form", "remind me at 5 PM to work on AIDE",
])
def test_non_music_not_spotify(text):
    assert spotify_intent(text) is None


@pytest.mark.parametrize("text,name", [
    ("open Discord", "desktop.open_app"),
    ("SAINT, open Chrome", "desktop.open_app"),
    ("close Notepad", "desktop.close_app"),
    ("switch to Spotify", "desktop.focus_window"),
    ("move this window to my second monitor", "desktop.move_window"),
    ("move Discord to the other screen", "desktop.move_window"),
    ("maximize this window", "desktop.arrange_window"),
    ("snap chrome to the left", "desktop.arrange_window"),
    ("type hello world into the search box", "desktop.type_text"),
    ("press ctrl+t", "desktop.press_keys"),
    ("click the send button", "desktop.click_element"),
    ("take a screenshot", "screen.capture"),
    ("what's on my screen", "screen.context"),
])
def test_desktop_phrases(text, name):
    it = parse_desktop(text)
    assert it is not None and it.name == name, (text, it and it.name)


def test_open_timer_is_not_an_app():
    assert parse_desktop("start a timer for 5 minutes") is None


def test_composite_split():
    it = route("open chrome and search for cats")
    assert it is not None and it.name == "composite:desktop.open_app+desktop.web_search"


def test_reminder_with_and_is_not_split():
    it = route("remind me in 10 minutes to buy milk and eggs")
    assert it is not None and it.name == "automation.create_reminder"


@pytest.mark.parametrize("text,name", [
    ("My favorite programming language is Python.", "memory.remember"),
    ("remember that my sister's name is Ana", "memory.remember"),
    ("What programming language do I like?", "memory.recall"),
    ("what's my favorite programming language", "memory.recall"),
    ("what do you know about me", "memory.list"),
    ("forget my favorite programming language", "memory.forget"),
])
def test_memory_phrases(text, name):
    it = parse_memory(text)
    assert it is not None and it.name == name, (text, it and it.name)


def test_generic_statement_not_memorised():
    assert parse_memory("my internet is down") is None


def test_unknown_goes_to_llm():
    assert route("why is the sky blue") is None

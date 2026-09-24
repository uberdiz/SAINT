"""Which browser window SAINT uses: no question when it's obvious, ask once when it isn't, then remember."""

import pytest

from core.config import config
from modules.agent.context import desktop_context
from modules.desktop import browser, controller
from modules.desktop.controller import AmbiguousWindow, WindowInfo


def _w(hwnd, title, fg=False, minimized=False, proc="opera.exe", monitor=1):
    return WindowInfo(hwnd=hwnd, title=title, process=proc, left=0, top=0, width=800, height=600,
                      monitor=monitor, minimized=minimized, maximized=False, foreground=fg)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    browser.forget_preference()
    monkeypatch.setattr(desktop_context, "window", lambda max_age=None: None)
    config.set("desktop.multi_window_policy", "ask", persist=False)
    yield
    browser.forget_preference()
    controller.last_ambiguity = None


def test_one_browser_on_screen_is_used_without_asking():
    wins = [_w(1, "Speed Dial - Opera"), _w(2, "Old tab - Opera", minimized=True)]
    assert browser.choose(wins=wins).hwnd == 1


def test_the_browser_in_front_wins():
    wins = [_w(1, "Docs - Opera"), _w(2, "YouTube - Opera", fg=True), _w(3, "Mail - Opera")]
    assert browser.choose(wins=wins).hwnd == 2


def test_several_on_screen_asks_and_the_answer_is_remembered():
    wins = [_w(1, "Docs - Opera"), _w(2, "YouTube - Opera", monitor=2), _w(3, "Mail - Opera", minimized=True)]
    with pytest.raises(AmbiguousWindow) as e:
        browser.choose(wins=wins)
    assert [w.hwnd for w in e.value.candidates] == [1, 2]            # only what's on screen
    assert controller.last_ambiguity[3]["remember"] is True
    browser.remember(wins[1])                                         # the user said "the second one"
    assert browser.choose(wins=wins).hwnd == 2
    assert browser.choose(wins=wins).hwnd == 2                        # ... and every time after


def test_preference_is_dropped_when_that_window_closes():
    browser.remember(_w(9, "Gone - Opera"))
    wins = [_w(1, "Docs - Opera"), _w(2, "YouTube - Opera")]
    with pytest.raises(AmbiguousWindow):
        browser.choose(wins=wins)


def test_nothing_on_screen_offers_the_minimized_ones():
    wins = [_w(1, "Docs - Opera", minimized=True), _w(2, "YouTube - Opera", minimized=True)]
    with pytest.raises(AmbiguousWindow) as e:
        browser.choose(wins=wins)
    assert controller.last_ambiguity[3]["offscreen"] is True
    assert "None of your browser windows are on screen" in str(e.value)


def test_no_browser_at_all_raises_no_browser():
    with pytest.raises(browser.NoBrowser):
        browser.choose(wins=[])


def test_a_named_window_is_used_and_remembered():
    wins = [_w(1, "Monkeytype | typing test - Opera"), _w(2, "YouTube - Opera")]
    assert browser.choose("monkey type", wins=wins).hwnd == 1
    assert (config.get(browser.PREF_KEY) or {}).get("hwnd") == 1
    # a site hint only steers one request
    browser.forget_preference()
    assert browser.choose("youtube", wins=wins, remember_hint=False).hwnd == 2
    assert not config.get(browser.PREF_KEY)


def test_recent_policy_never_asks():
    config.set("desktop.multi_window_policy", "recent", persist=False)
    wins = [_w(1, "Docs - Opera"), _w(2, "YouTube - Opera")]
    assert browser.choose(wins=wins).hwnd == 1


def test_window_name_folded_into_the_app_name():
    assert controller._browser_request("monkey type browser")[0] == "browser"
    assert controller._browser_request("monkey type browser")[1] == "monkey type"
    assert controller._browser_request("my youtube browser window") == ("browser", "youtube")
    assert controller._browser_request("discord") == ("discord", "")


def test_asking_remembers_the_answer(monkeypatch):
    from modules.agent import desktop_intents
    from modules.agent.confirm import choices
    from modules.agent.router import Reply
    wins = [_w(11, "Docs - Opera"), _w(12, "YouTube - Opera", monitor=2)]
    d = controller.desktop
    monkeypatch.setattr(d, "monitors", lambda: [])
    monkeypatch.setattr(d, "_info", lambda hwnd: next(w for w in wins if w.hwnd == hwnd))
    monkeypatch.setattr(d, "_activate", lambda w: w)
    AmbiguousWindow("browser", wins, remember=True)
    asked = desktop_intents._ask_which(lambda: Reply("Searched."))
    assert asked is not None and "Which one" in asked.text and "keep using it" in asked.text
    assert choices.resolve("the youtube one") == "Searched."
    assert (config.get(browser.PREF_KEY) or {}).get("hwnd") == 12

"""Which window a command acts on: SAINT's working window unless the user switched away."""

import pytest

from modules.agent.context import desktop_context
from modules.desktop.controller import DesktopController, WindowInfo


def _w(hwnd, title, fg=False, proc="opera.exe"):
    return WindowInfo(hwnd=hwnd, title=title, process=proc, left=0, top=0, width=800, height=600,
                      monitor=1, minimized=False, maximized=False, foreground=fg)


@pytest.fixture
def ctl(monkeypatch):
    c = DesktopController()
    monkeypatch.setattr(DesktopController, "_is_own", staticmethod(lambda w: w.process == "saint.exe"))
    monkeypatch.setattr(desktop_context, "window", lambda: desktop_context._test_ref)
    monkeypatch.setattr(desktop_context, "foreground_at_note", lambda: desktop_context._test_fg)
    yield c
    desktop_context.clear()


def _ctx(ref, fg_then):
    desktop_context._test_ref, desktop_context._test_fg = ref, fg_then


def test_keeps_working_window_when_user_has_not_switched(ctl):
    # SAINT searched in window 2 while window 1 was in front; window 1 is still in front.
    _ctx(2, 1)
    wins = [_w(1, "Your video", fg=True), _w(2, "Kendrick Lamar - YouTube")]
    assert ctl.target_window(wins).hwnd == 2


def test_follows_the_user_after_they_switch(ctl):
    _ctx(2, 2)                                     # SAINT's window was in front ...
    wins = [_w(3, "Something else", fg=True), _w(2, "Kendrick Lamar - YouTube")]
    assert ctl.target_window(wins).hwnd == 3      # ... then the user switched to 3


def test_saint_window_focus_is_not_a_switch(ctl):
    _ctx(2, 2)
    wins = [_w(9, "SAINT", fg=True, proc="saint.exe"), _w(2, "Kendrick Lamar - YouTube")]
    assert ctl.target_window(wins).hwnd == 2


def test_no_context_uses_foreground(ctl):
    _ctx(None, None)
    wins = [_w(1, "A"), _w(4, "B", fg=True)]
    assert ctl.target_window(wins).hwnd == 4


def test_no_context_and_saint_focused_uses_previous_window(ctl):
    _ctx(None, None)
    wins = [_w(9, "SAINT", fg=True, proc="saint.exe"), _w(5, "Previous app")]
    assert ctl.target_window(wins).hwnd == 5


def test_choice_matching():
    from modules.agent.confirm import ChoiceManager, ChoiceOption
    opts = [ChoiceOption("Speed Dial on your second screen", "Speed Dial Opera monitor 2 second other"),
            ChoiceOption("BLOODHOUNDS - YouTube", "BLOODHOUNDS YouTube Opera monitor 1 main primary"),
            ChoiceOption("Monkeytype", "Monkeytype Opera monitor 1 main primary")]
    m = ChoiceManager()
    assert m.match("the second one", opts) == 1
    assert m.match("the YouTube one", opts) == 1
    assert m.match("the one on my second screen", opts) == 0
    assert m.match("monkeytype", opts) == 2
    assert m.match("number three", opts) == 2
    assert m.match("the last one", opts) == 2

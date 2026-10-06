"""Manual-test round 2026-10-05: mini player resize / album-art mode / volume / live repaint,
placing it by voice, paths read in a French accent, spoken e-mail addresses, "stop speaking",
"hey SAINT" kept in a command, albums, "not the kind of song", lessons swallowing asides."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ---------------------------------------------------------------------- #
# Speech and language
# ---------------------------------------------------------------------- #
def test_paths_and_links_are_said_short():
    from modules.voice.speech_text import for_speech
    said = for_speech(r"Opened C:\Users\sam\Downloads. Made New Text Document.txt in C:\Users\sam\Downloads.")
    assert "\\" not in said and said == "Opened Downloads. Made New Text Document.txt in Downloads."
    assert for_speech(r"Opening C:\Program Files\Antigravity\Antigravity IDE.lnk now") == \
        "Opening Antigravity IDE now"
    assert for_speech("Go to https://mail.google.com/mail/u/0/#inbox") == "Go to mail.google.com"
    assert for_speech("It's j.norton@essextech.net") == "It's j dot norton at essextech dot net"


def test_a_path_never_makes_a_request_french():
    from modules.lang.detect import detect
    d = detect(r'open this "C:\Users\sam\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Antigravity'
               r'\Antigravity IDE.lnk" for antigravity')
    assert d.primary == "en" and not d.mixed


def test_stop_speaking_is_a_stop_not_a_mute():
    from modules.agent.meta import match_meta
    for t in ("STOP SPEAKING FRIEND!", "stop speaking", "stop saying that", "stop talking bro"):
        assert match_meta(t) is not None and match_meta(t).kind == "stop", t
    assert match_meta("stop the music") is None


def test_a_greeting_is_never_part_of_the_command():
    from modules.agent.agent import Agent, _GREETED_WAKE
    assert _GREETED_WAKE.sub("", "Hey SAINT, write an email to Mr. Norton about Friday.", count=1) == \
        "write an email to Mr. Norton about Friday."
    assert _GREETED_WAKE.sub("", "Saint Louis weather", count=1) == "Saint Louis weather"
    r = Agent().handle("Hey, SAINT!")
    assert r is not None and r.expects_reply and "what do you need" in r.text


# ---------------------------------------------------------------------- #
# E-mail addresses said aloud
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("said, addr", [
    ("J. Norton at EssexTech.net", "j.norton@essextech.net"),
    ("sam dot lee at gmail dot com", "sam.lee@gmail.com"),
    ("it's jo_b@x.io", "jo_b@x.io"),
    ("first underscore last at school dot org", "first_last@school.org"),
    ("It is", ""),
    ("at the store", ""),
])
def test_spoken_email(said, addr):
    from modules.learning.lesson import spoken_email
    assert spoken_email(said) == addr


# ---------------------------------------------------------------------- #
# Routing
# ---------------------------------------------------------------------- #
def _name(text):
    from modules.agent.router import route
    it = route(text)
    return it.name if it else None


def test_moving_the_mini_player_never_toggles_it():
    for t in ("Move the mini player to the top left of my screen.",
              "move the mini player to the top left of my second screen",
              "put the mini player in the bottom right corner", "move the mini player to the center"):
        assert _name(t) == "ui.place", t
    assert _name("nudge the mini player up a little") == "ui.nudge"
    assert _name("move the mini player over there") != "ui.mini_player"
    assert _name("turn off the mini player") == "ui.mini_player"
    assert _name("make the mini player smaller") == "ui.mini_size"
    assert _name("just show the album art") == "ui.mini_size"


def test_a_bare_nudge_follows_a_move():
    from modules.agent import task_intents
    task_intents._last_placed.update(what="", at=0.0)
    assert _name("right a bit") is None                        # nothing was just moved
    import time
    task_intents._last_placed.update(what="mini_player", at=time.time())
    assert _name("right a bit") == "ui.nudge"
    assert _name("down a couple pixels") == "ui.nudge"
    task_intents._last_placed.update(what="", at=0.0)


def test_corner_and_amount_words():
    from modules.agent.task_intents import _amount, _corner
    assert _corner("top left") == "top-left" and _corner("upper right corner") == "top-right"
    assert _corner("bottom") == "bottom" and _corner("middle") == "center"
    assert _amount("a couple pixels") == 6 and _amount("20 pixels") == 20 and _amount("a bit") == 40


def test_albums_and_not_this_kind_of_song():
    assert _name("Play the album, fancy that.") == "spotify.play_album"
    assert _name("play the album called DAMN") == "spotify.play_album"
    # Off-vibe, not disliked (2026-10-06): skip it and steer the mix, don't mark the song down.
    assert _name("This is not the kind of song I was talking about.") == "spotify.not_mood"
    assert _name("this is not hype at all") == "spotify.not_mood"
    assert _name("thats not the type of music i said") == "spotify.not_mood"
    assert _name("that's not the vibe I asked for") == "spotify.not_mood"
    assert _name("I'm not feeling this song") == "spotify.next_reject"
    assert _name("this is not the kind of game i like") != "spotify.next_reject"


def test_music_is_an_aside_during_an_email_lesson():
    from modules.learning.lesson import LessonManager
    assert LessonManager._aside("write an email", "play the album fancy that")
    assert LessonManager._aside("write an email", "move the mini player to the top left")
    assert not LessonManager._aside("write an email", "click compose")
    assert not LessonManager._aside("make a playlist", "play the album fancy that")


def test_long_text_has_a_limit_but_no_small_one():
    from core.config import config
    from modules.automation.tools import ToolError
    from modules.desktop.controller import MAX_INPUT_TEXT, DesktopController
    config.set("desktop.enabled", True, persist=False)
    try:
        with pytest.raises(ToolError):
            DesktopController().type_text("x" * (MAX_INPUT_TEXT + 1))
    finally:
        config.set("desktop.enabled", False, persist=False)


# ---------------------------------------------------------------------- #
# The mini player
# ---------------------------------------------------------------------- #
@pytest.fixture
def mini(monkeypatch):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ui import actions
    monkeypatch.setattr(actions, "refresh_spotify", lambda *a, **k: None)
    monkeypatch.setattr(actions, "spotify_status", lambda: (True, ""))
    vols = []
    monkeypatch.setattr(actions, "app_volume", lambda app, percent=None, **kw: vols.append((app, percent)))
    from ui.spotify_widget import SpotifyWidget

    class Shell:
        def set_widget(self, on): pass
        def toggle_overlay(self): pass
        def show_normal(self): pass
        def navigate(self, page): pass
    w = SpotifyWidget(Shell())
    w.set_lyrics(False)
    yield w
    w.hide()
    w.deleteLater()
    app.processEvents()


def test_mini_player_resizes_both_ways_and_becomes_album_art(mini):
    from PySide6.QtCore import QRect, Qt
    from ui import spotify_widget as sw
    mini.set_card(400, 138)
    assert mini._mode == "full" and mini.player.isVisibleTo(mini) and not mini.art.isVisibleTo(mini)
    mini.set_card(520, 260)                                   # taller: bigger art
    assert mini.height() == 260 + 2 * sw.SHADOW and mini.player.cover.width() > 150
    mini.set_card(150, 150)                                   # small / square: just the art
    assert mini._mode == "art" and mini.art.isVisibleTo(mini) and not mini.player.isVisibleTo(mini)
    mini.set_card(10, 5000)
    assert mini._card == (sw.MIN_W, sw.MAX_H)
    # Dragging the top-left corner: the bottom-right corner stays put.
    mini.set_card(300, 140)
    mini.move(500, 500)
    geo = QRect(mini.geometry())
    mini.resize_by(Qt.LeftEdge | Qt.TopEdge, -100, -60, geo, mini._card)
    assert mini._card == (400, 200)
    assert mini.geometry().right() == geo.right() and mini.geometry().bottom() == geo.bottom()


def test_mini_player_edges_are_resize_grips(mini):
    from PySide6.QtCore import QPoint, Qt
    from ui import spotify_widget as sw
    mini.set_card(340, 138)
    s = sw.SHADOW
    assert mini.edges_at(QPoint(mini.width() - s - 2, mini.height() // 2)) == Qt.RightEdge
    assert mini.edges_at(QPoint(mini.width() // 2, mini.height() - s - 2)) == Qt.BottomEdge
    assert mini.edges_at(QPoint(s + 1, s + 1)) == (Qt.LeftEdge | Qt.TopEdge)
    assert not mini.edges_at(QPoint(mini.width() // 2, mini.height() // 2))


def test_mini_player_has_volume_and_the_art_view_follows_the_player(mini):
    from ui.reactive import ui_bus
    from core.events import EventType
    import time
    mini.set_card(150, 150)
    mini.show()
    ui_bus.inject(EventType.MEDIA_CHANGED, {"title": "Balloon", "artist": "Tyler", "is_playing": True,
                                            "position_ms": 1000, "duration_ms": 100000, "at": time.time(),
                                            "app": "Opera", "app_id": "Opera.123"})
    mini._flush()
    assert mini.art.title.text() == "Balloon" and mini.art.play._name == "pause"
    assert not mini.player.vol_icon.isHidden()                    # the full layout has a volume slider
    mini.set_card(340, 138)
    assert mini.player.vol.isVisibleTo(mini)


def test_app_stem_matches_the_volume_mixer():
    from ui.actions import app_stem
    assert app_stem({"app": "Spotify", "app_id": "Spotify.exe"}) == "spotify"
    assert app_stem({"app_id": "OperaSoftware.OperaWebBrowser.1777667688"}) == "opera"
    assert app_stem({"app": "Edge"}) == "msedge"


# ---------------------------------------------------------------------- #
# SAINT Link firewall rule (2026-10-06: left on a test's random port, PCs couldn't connect)
# ---------------------------------------------------------------------- #
_NETSH = """
Rule Name:                            SAINT Link
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Protocol:                             UDP
LocalPort:                            8766
Action:                               Allow

Rule Name:                            SAINT Link
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Protocol:                             TCP
LocalPort:                            53430
Action:                               Allow
Ok.
"""


def test_firewall_rule_ports_are_read_exactly():
    from modules.link.service import rule_tcp_ports
    assert rule_tcp_ports(_NETSH) == {53430}                       # the UDP discovery port isn't the TCP one
    assert rule_tcp_ports("No rules match the specified criteria.") == set()


def test_a_stale_firewall_rule_is_fixed_even_after_asking(monkeypatch):
    import subprocess
    import sys
    from core.config import config
    from modules.link import service
    if sys.platform != "win32":
        pytest.skip("Windows Firewall")
    asked = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0,
                                                                          "stdout": _NETSH.encode()})())
    import ctypes
    monkeypatch.setattr(ctypes.windll.shell32, "ShellExecuteW", lambda *a: asked.append(a[3]) or 42)
    monkeypatch.setitem(config._data.setdefault("link", {}), "manage_firewall", True)
    monkeypatch.setitem(config._data["link"], "firewall_asked_port", 8765)
    monkeypatch.setattr(config, "set", lambda *a, **k: None)
    service.ensure_firewall_rule(8765)
    assert asked and "localport=8765" in asked[0]                  # wrong port: asked again, for 8765
    asked.clear()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": b""})())
    service.ensure_firewall_rule(8765)
    assert asked == []                                             # no rule after asking = you said no

"""Now playing from any app, movable overlay cards, switches, the action notice."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from modules.desktop.media import app_label, pick  # noqa: E402


def _s(app_id, title, playing):
    return {"app_id": app_id, "title": title, "is_playing": playing}


def test_pick_prefers_whats_playing():
    sessions = [_s("Spotify.exe", "Song", False), _s("Opera.123", "Video", True)]
    assert pick(sessions, current_id="Spotify.exe")["title"] == "Video"
    both = [_s("Spotify.exe", "Song", True), _s("Opera.123", "Video", True)]
    assert pick(both, current_id="Opera.123")["title"] == "Video"
    assert pick([_s("Spotify.exe", "Song", False)], "")["title"] == "Song"
    assert pick([], "") is None


def test_app_labels():
    assert app_label("OperaSoftware.OperaWebBrowser.1777667688") == "Opera"
    assert app_label("Spotify.exe") == "Spotify"
    assert app_label("308046B0AF4A39CB") == "308046B0AF4A39CB"


def test_now_playing_order():
    from ui.reactive import ui_bus
    saved = (ui_bus.media, ui_bus.spotify)
    try:
        ui_bus.spotify = {"track": "Song", "artists": "Band", "is_playing": True, "duration_ms": 1000}
        ui_bus.media = {"title": "Video", "app": "Opera", "is_playing": True, "is_spotify": False, "at": time.time()}
        assert ui_bus.now_playing()["title"] == "Video"                  # a playing video comes first
        ui_bus.media = dict(ui_bus.media, is_playing=False)
        assert ui_bus.now_playing()["source"] == "spotify"               # paused video: back to Spotify
        ui_bus.spotify = {}
        np = ui_bus.now_playing()
        assert np["source"] == "media" and np["artist"] == "Opera"
    finally:
        ui_bus.media, ui_bus.spotify = saved


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pump(app, secs):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def test_switch_reads_on_and_off():
    _app()
    from ui.widgets import Switch
    s = Switch("Halo")
    s.setChecked(True)
    assert s.isChecked() and s._state_word() == "On" and s._pos == 1.0
    s.setChecked(False)
    assert s._state_word() == "Off"


def test_overlay_cards_move_resize_and_remember(monkeypatch):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from core.config import config
    from ui.overlay import CardCanvas, FloatingCard
    app = _app()
    config.set("overlay.layout", {}, persist=False)
    canvas = CardCanvas()
    card = FloatingCard("now_playing", "Now playing")
    canvas.add(card)
    canvas.resize(1600, 800)
    canvas.show()
    _pump(app, 0.05)
    start = card.geometry()

    def drag(local, delta):
        g = card.mapToGlobal(local)
        for kind, pos, gpos, btns in ((QMouseEvent.MouseButtonPress, local, g, Qt.LeftButton),
                                      (QMouseEvent.MouseMove, local + delta, g + delta, Qt.LeftButton),
                                      (QMouseEvent.MouseButtonRelease, local + delta, g + delta, Qt.NoButton)):
            ev = QMouseEvent(kind, QPointF(pos), QPointF(gpos), Qt.LeftButton, btns, Qt.NoModifier)
            {QMouseEvent.MouseButtonPress: card.mousePressEvent, QMouseEvent.MouseMove: card.mouseMoveEvent,
             QMouseEvent.MouseButtonRelease: card.mouseReleaseEvent}[kind](ev)

    drag(QPoint(60, 20), QPoint(200, 40))                       # title bar → move
    assert abs(card.x() - (start.x() + 200)) <= 4 and abs(card.y() - (start.y() + 40)) <= 4   # 8 px grid
    assert card.size() == start.size()
    before = card.geometry()
    drag(QPoint(card.width() - 3, card.height() - 3), QPoint(-80, -64))    # corner → resize
    assert abs(card.width() - (before.width() - 80)) <= 4 and abs(card.height() - (before.height() - 64)) <= 4
    assert card.pos() == before.topLeft()
    saved = config.get("overlay.layout")["now_playing"]
    assert abs(saved[0] * 1600 - card.x()) < 2 and abs(saved[2] * 1600 - card.width()) < 2
    moved = card.geometry()
    canvas.restore()                                              # reopening puts it back where it was
    assert card.geometry() == moved
    canvas.reset()
    _pump(app, 0.5)
    assert config.get("overlay.layout") == {}
    canvas.hide()


def test_action_notice_shows_what_happened():
    from core.config import config
    from ui.action_notice import ActionNotice, label_for
    app = _app()
    config.set("notifications.actions", True, persist=False)
    assert label_for("browser.youtube", {"action": "speed", "value": 1.5}) == "YouTube · 1.5x speed"
    assert label_for("desktop.web_search", {"query": "lofi", "site": "youtube"}) == "Search Youtube for lofi"
    n = ActionNotice()
    n.started("spotify.next")
    _pump(app, 0.1)
    assert n.isVisible() and n._state == "running"
    n.finished(False, "I found 3 browser windows. Which one should I use?")
    assert n._state == "ask"
    n.finished(True)
    assert n._state == "ok"
    n.leave()
    _pump(app, 0.5)
    assert not n.isVisible()
    n.close()

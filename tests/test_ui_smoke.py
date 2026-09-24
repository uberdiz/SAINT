"""Build the whole UI headless and exercise the shell: pages, palette, overlay, Halo, mini player, demo."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def window():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from core.config import config
    config.set("overlay.hotkey", "", persist=False)          # never grab a real system hotkey in tests
    from ui.main_window import MainWindow
    w = MainWindow(app, None)
    w.show()
    yield app, w
    w.demo.stop()
    w.halo.shutdown()
    w.overlay.hide()
    w.widget.hide()


def _pump(app, secs):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def test_every_page_opens(window):
    from ui.main_window import PAGES
    app, w = window
    for name, _icon in PAGES:
        w.navigate(name, animate=False)
        _pump(app, 0.05)
        assert w.stack.currentIndex() == w.sidebar.group.checkedId()


def test_palette_lists_pages_and_runs_a_command(window):
    app, w = window
    w.palette.open()
    w.palette.input.setText("histor")
    assert w.palette.list.item(0).text() == "Go to History"
    w.palette._run()
    assert not w.palette.isVisible() and w.stack.currentWidget().title.text() == "History"


def test_overlay_halo_and_mini_player(window):
    app, w = window
    w.toggle_overlay()
    _pump(app, 0.4)
    assert w.overlay.isVisible()
    w._last_toggle = 0
    w.toggle_overlay()
    _pump(app, 0.4)
    assert not w.overlay.isVisible()
    w.halo.set_visible(True)
    _pump(app, 0.2)
    assert len(w.halo._strips) == 4
    w.halo.set_visible(False)
    w.set_widget(True)
    assert w.widget.isVisible()
    w.set_widget(False)
    assert not w.widget.isVisible()


def test_demo_never_reaches_core(window):
    """Scripted demo events must not reach core subscribers (history, conversation, analytics)."""
    from core.events import event_bus
    from ui.reactive import ui_bus
    app, w = window
    seen = []
    handler = seen.append
    event_bus.subscribe(handler)
    try:
        w.start_demo()
        _pump(app, 3.0)
        assert ui_bus.demo
        assert not [e for e in seen if e.type in ("conversation.turn.start", "tool.started", "spotify.playback.changed")]
        w.demo.stop()
        assert not ui_bus.demo
    finally:
        event_bus.unsubscribe(handler)


def test_overlay_shows_the_spotify_queue(window, monkeypatch):
    from PySide6.QtWidgets import QLabel
    from ui import actions
    app, w = window
    monkeypatch.setattr(actions, "fetch_queue", lambda limit=5: [("Song A", "Artist One"), ("Song B", "Artist Two")])
    w.overlay._refresh_queue()
    _pump(app, 0.4)
    texts = [lab.text() for lab in w.overlay.findChildren(QLabel)]
    assert any("Song A" in t for t in texts) and any("Artist Two" in t for t in texts), texts[:20]
    monkeypatch.setattr(actions, "fetch_queue", lambda limit=5: (_ for _ in ()).throw(RuntimeError("Spotify isn't connected.")))
    w.overlay._refresh_queue()
    _pump(app, 0.4)
    assert any("isn't connected" in lab.text() for lab in w.overlay.findChildren(QLabel))

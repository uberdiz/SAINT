"""v0.4 fixes: Spotify volume sync both ways, album-art quality, app-icon "covers", and the
pairing QR code that was clipped on the Devices page (2026-10-06 notes)."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pump(app, secs):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


# ---------------------------------------------------------------- Spotify volume, SAINT -> Spotify
@pytest.fixture
def mini(app, monkeypatch):
    from ui import actions
    sent = []
    monkeypatch.setattr(actions, "refresh_spotify", lambda *a, **k: None)
    monkeypatch.setattr(actions, "spotify_status", lambda: (True, ""))
    monkeypatch.setattr(actions, "app_volume", lambda *a, **k: None)
    monkeypatch.setattr(actions, "spotify", lambda tool, on_error=None, **kw: sent.append((tool, kw)))
    monkeypatch.setattr(actions, "spotify_fast_poll", lambda key, on: None)
    from ui.reactive import ui_bus
    # Spotify is what's playing (earlier tests may have left a video in the shared state).
    monkeypatch.setattr(ui_bus, "media", {})
    monkeypatch.setattr(ui_bus, "spotify", {"track": "Song", "artists": "Artist", "is_playing": True,
                                            "volume": 100, "supports_volume": True, "duration_ms": 1000})
    from ui.spotify_widget import SpotifyWidget

    class Shell:
        def set_widget(self, on): pass
        def toggle_overlay(self): pass
        def show_normal(self): pass
        def navigate(self, page): pass
    w = SpotifyWidget(Shell())
    w.set_lyrics(False)
    w.player._sp_ok = True
    w.player._app_vol = ""
    w.sent = sent
    yield w
    w.hide()
    w.deleteLater()
    app.processEvents()


def test_clicking_the_volume_track_reaches_spotify(app, mini):
    """A click on the slider's track changes its value without a drag release. It used to move
    the slider, send nothing, and snap back to 100% on the next update."""
    p = mini.player
    p._show_volume({"volume": 100}, force=True)
    p.vol.setValue(60)                                   # what a click / arrow key does (signals on)
    _pump(app, 0.4)
    assert ("spotify.volume", {"percent": 60}) in mini.sent
    # Spotify still reports the old value for a moment: the slider holds what the user chose.
    p._show_volume({"volume": 100})
    assert p.vol.value() == 60


def test_mini_player_art_slider_and_wheel_drive_the_player(app, mini):
    p = mini.player
    p._show_volume({"volume": 80}, force=True)
    mini.art.vol.setValue(30)                            # the slider over the album art
    assert p.vol.value() == 30                           # sync() can't put the old value back
    mini.art.sync()
    assert mini.art.vol.value() == 30
    _pump(app, 0.4)
    assert mini.sent[-1] == ("spotify.volume", {"percent": 30})


def test_spotify_reports_are_never_sent_back(app, mini):
    """No feedback loop: what Spotify reports updates the slider without a volume request."""
    from ui.reactive import ui_bus
    p = mini.player
    p._pending_vol, p._vol_hold = None, 0
    ui_bus.spotify["volume"] = 42                        # what Spotify now reports
    p._show_volume(ui_bus.spotify)
    _pump(app, 0.4)
    assert p.vol.value() == 42
    assert not any(t == "spotify.volume" for t, _ in mini.sent)


def test_external_volume_change_shows_after_the_hold(app, mini):
    p = mini.player
    p.user_volume(50)
    _pump(app, 0.4)
    p._vol_hold = 0                                      # Spotify had time to apply it
    p._show_volume({"volume": 35})                       # then changed on the phone
    assert p.vol.value() == 35


def test_spotify_volume_never_falls_back_to_the_windows_mixer(app, mini):
    p = mini.player
    p._sp_ok = False                                     # Spotify not connected in SAINT
    p._follow_volume({"source": "spotify_media", "app": "Spotify", "is_spotify": True})
    assert p._app_vol == "" and not p.vol.isEnabled()


# ---------------------------------------------------------------- Spotify volume, Spotify -> SAINT
def test_fast_poll_only_while_a_volume_control_is_visible():
    from modules.spotify.module import SpotifyModule
    sp = SpotifyModule()
    assert not sp.fast_polling
    sp.want_fast_poll("mini-player", True)
    sp.want_fast_poll("music-page", True)
    sp.want_fast_poll("mini-player", False)
    assert sp.fast_polling
    sp.want_fast_poll("music-page", False)
    assert not sp.fast_polling


def test_light_poll_publishes_only_on_change(monkeypatch):
    from modules.spotify.tools import SpotifyTools
    tools = SpotifyTools.__new__(SpotifyTools)
    published = []
    state = {"id": "a", "is_playing": True, "volume": 70, "device": "PC", "shuffle": False}
    monkeypatch.setattr(tools, "_state", lambda force=False: dict(state), raising=False)
    monkeypatch.setattr(tools, "_publish", lambda st: published.append(st["volume"]), raising=False)
    tools.poll_light()
    tools.poll_light()
    state["volume"] = 40                                 # changed in Spotify / on the phone
    tools.poll_light()
    assert published == [70, 40]


def test_playback_state_reports_volume_support_and_hires_cover():
    from modules.spotify.tools import SpotifyTools
    tools = SpotifyTools.__new__(SpotifyTools)
    images = [{"url": "https://i.scdn.co/image/ab67616d0000b273abc"},
              {"url": "https://i.scdn.co/image/ab67616d00001e02abc"}]
    st = tools._build_state({"device": {"name": "iPhone", "volume_percent": 50, "supports_volume": False}},
                            {"name": "x", "album": {"images": images}}, images)
    assert st["supports_volume"] is False
    assert st["image_hires"] == "https://i.scdn.co/image/ab67616d000082c1abc"


# ---------------------------------------------------------------- album art
def test_hires_cover_url():
    from modules.spotify.tools import hires_cover_url
    assert hires_cover_url("https://i.scdn.co/image/ab67616d00001e02ffff").endswith("ab67616d000082c1ffff")
    assert hires_cover_url("https://example.com/x.jpg") == ""
    assert hires_cover_url("") == ""


def _image(w, h, alpha=False):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage, QPainter
    img = QImage(w, h, QImage.Format_ARGB32 if alpha else QImage.Format_RGB32)
    img.fill(Qt.transparent if alpha else QColor("#334455"))
    p = QPainter(img)
    p.setBrush(QColor("#4285f4"))
    p.drawEllipse(w // 8, h // 8, w * 3 // 4, h * 3 // 4)
    p.end()
    return img


def test_app_icon_thumbnails_are_recognised(app):
    from ui.widgets import looks_like_icon
    assert looks_like_icon(_image(256, 256, alpha=True))          # Chrome's logo standing in for art
    assert looks_like_icon(_image(64, 64))                        # tiny
    assert not looks_like_icon(_image(640, 640))                  # an album cover
    assert not looks_like_icon(_image(480, 270))                  # a video thumbnail


def test_big_art_asks_for_the_hires_cover_and_falls_back(app, monkeypatch):
    from PySide6.QtGui import QPixmap
    from ui import widgets
    asked = []

    def fake_get(url, cb):
        asked.append(url)
        cb(None if "082c1" in url else QPixmap.fromImage(_image(640, 640)))
    monkeypatch.setattr(widgets.covers, "get", fake_get)
    art = widgets.CoverArt(80)
    art.set_url("https://i.scdn.co/image/ab67616d0000b273q",
                hires="https://i.scdn.co/image/ab67616d000082c1q")
    assert asked == ["https://i.scdn.co/image/ab67616d0000b273q"]       # small: the normal cover
    art.set_size(900, 900)                                              # big album art mode
    assert asked[-2:] == ["https://i.scdn.co/image/ab67616d000082c1q",   # tried the original...
                          "https://i.scdn.co/image/ab67616d0000b273q"]   # ...and fell back
    assert art._pm is not None


def test_tall_mini_player_keeps_the_whole_cover_and_controls(app, mini):
    mini.set_card(220, 520)
    assert mini._mode == "art"
    mini.show()
    app.processEvents()
    assert mini.art.cover._fit == "contain" and mini.art._room
    assert mini.art.scrim.isVisibleTo(mini.art)                  # title and controls stay visible


# ---------------------------------------------------------------- pairing QR code
def test_qr_modules_are_whole_device_pixels_with_a_quiet_zone(app):
    from ui.components.qr_view import QUIET, module_px, qr_image
    matrix = [[(x * y) % 3 == 0 for x in range(57)] for y in range(57)]
    for dpr in (1.0, 1.25, 1.5, 2.0):
        cell = module_px(260 * dpr, 57)
        assert cell >= 3
        img = qr_image(matrix, cell)
        assert img.width() == (57 + 2 * QUIET) * cell
        assert img.pixelColor(0, 0).name() == "#ffffff"         # quiet zone


def test_qr_view_asks_for_enough_room_to_stay_scannable(app):
    from ui.components.qr_view import MIN_MODULE_PX, QrView
    from modules.link.service import qr_matrix
    from modules.link.identity import PairingOffer
    offer = PairingOffer(os.urandom(5), "own", 9e12)
    uri = offer.uri("192.168.1.123", 8765, "HOME-PC1", alternates=[
        "10.0.0.37", "home-pc1.tail1234.ts.net", "2001:db8:8380:3b10:9d2c:1f4b:7e3a:55c1", "203.0.113.170"])
    m = qr_matrix(uri)
    if m is None:
        pytest.skip("segno not installed")
    v = QrView()
    v.set_matrix(m)
    v.resize(v.minimumSizeHint())
    assert v.cell_px() >= MIN_MODULE_PX and v.scannable()
    assert v.heightForWidth(300) == 300


def test_devices_page_never_forces_itself_wider_than_the_window(app, monkeypatch):
    """The Received card's label held the inbox path without wrapping: its minimum width forced the
    page wider than a small or scaled window and clipped the QR code and the address."""
    import ui.pages.devices as dev
    page = dev.DevicesPage()
    page.inbox_label.setText("Nothing shared with you is waiting. Files from your devices land in "
                             + "C:\\Users\\someone\\AppData\\Local\\SAINT\\link\\inbox" * 3)
    assert page.inbox_label.wordWrap()
    assert page.scroll is not None                                     # the page scrolls instead
    # Ignored horizontally: the layout doesn't take the unbreakable path as a minimum width.
    from PySide6.QtWidgets import QSizePolicy
    assert page.inbox_label.sizePolicy().horizontalPolicy() == QSizePolicy.Ignored
    assert page.scroll.widget().minimumSizeHint().width() < 900
    page.deleteLater()

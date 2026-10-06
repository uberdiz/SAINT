"""Fixes from the 2026-10-06 test notes: "Fancy That by Pink Panthers" is an album
(it played the song "Tonight" plus a radio of look-alikes), "not the vibe I asked
for" skips without disliking, the mini player's album art can grow in both
directions and can't be dragged off screen, the mic is remembered by name and a
Bluetooth hands-free mic (AirPods) is never opened by accident, SAINT Link's
router port mapping (no Tailscale) and every device's logs collected on this PC."""

import os

import pytest

from core.config import config
from tests.test_session_0930 import Client0930
from tests.test_spotify_0929 import _track


# ---------------------------------------------------------------- Spotify: albums
class AlbumClient(Client0930):
    def search(self, q, types="track", limit=10, offset=0):
        ql = q.lower()
        if types == "track" and ("pink panther" in ql or "pinkpantheress" in ql):
            return {"tracks": {"items": [_track("tn", "Tonight", "PinkPantheress", "pp", 70)]}}
        return super().search(q, types, limit, offset)

    def artist_top_tracks(self, artist_id):
        return [_track("tn", "Tonight", "PinkPantheress", "pp", 70)]


@pytest.fixture
def sp(tmp_path, monkeypatch):
    from modules.spotify.memory import SpotifyMemory
    from modules.spotify.tools import SpotifyTools
    t = SpotifyTools(AlbumClient())
    t._memory = SpotifyMemory(str(tmp_path / "sp.db"))
    monkeypatch.setattr(t, "_refresh_soon", lambda *a, **k: None)
    return t


def test_album_by_a_misheard_artist_is_the_album_not_a_song(sp):
    ent = sp.resolve("Fancy That by Pink Panthers")
    assert ent["kind"] == "album" and ent["name"] == "Fancy That" and ent.get("context")
    assert sp.resolve("Fancy That by Pink Panthers", "album")["name"] == "Fancy That"
    assert sp.resolve("Tonight by Pink Panthers")["kind"] == "track"        # a real song stays a song


def test_playing_an_album_plays_the_context_without_a_radio(sp, monkeypatch):
    played = []
    monkeypatch.setattr(sp, "_with_device", lambda fn: played.append(fn("dev")))
    monkeypatch.setattr(sp.client, "play", lambda **kw: kw, raising=False)
    monkeypatch.setattr(sp.client, "repeat", lambda state, device_id=None: ("repeat", state), raising=False)
    monkeypatch.setattr(sp, "_radio_start", lambda **kw: pytest.fail("an album never starts a song radio"))
    r = sp.play_query("Fancy That by Pink Panthers")
    assert r["kind"] == "album" and not r["radio"]
    assert {"context_uri": "spotify:album:ft", "device_id": "dev"} in played
    assert ("repeat", "context") in played


def test_album_phrasings_route_to_the_album():
    from modules.agent.router import spotify_intent
    for text, query in (("play the album, fancy that.", "fancy that"),
                        ("play Fancy That album by PinkPantheress", "Fancy That by PinkPantheress"),
                        ("play PinkPantheress's album Fancy That", "Fancy That by PinkPantheress")):
        si = spotify_intent(text)
        assert si.kind == "play_album" and si.kwargs == {"query": query, "kind": "album"}, (text, si)


def test_off_vibe_is_not_a_dislike(sp, monkeypatch):
    monkeypatch.setattr(sp, "next", lambda source="voice": {"success": True})
    monkeypatch.setattr(sp, "_state", lambda: {"item": {"id": "c", "name": "Now", "artists": [{"name": "X"}]}})
    recorded = []
    monkeypatch.setattr(sp.memory, "record_feedback", lambda *a, **k: recorded.append(a))
    sp.not_mood("", "current")
    assert recorded == []


# ---------------------------------------------------------------- mini player
@pytest.fixture
def mini(monkeypatch):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ui import actions
    monkeypatch.setattr(actions, "refresh_spotify", lambda *a, **k: None)
    monkeypatch.setattr(actions, "spotify_status", lambda: (True, ""))
    monkeypatch.setattr(actions, "app_volume", lambda *a, **k: None)
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


def test_album_art_grows_wide_and_tall(mini):
    from ui import spotify_widget as sw
    mini.set_card(500, 500)                                 # a big square: still just the art
    assert mini._mode == "art" and mini.width() == 500 + 2 * sw.SHADOW
    mini.set_card(700, 600)
    assert mini._mode == "art"
    mini.set_card(520, 150)                                 # a wide strip: the full player
    assert mini._mode == "full"
    mini.set_card(900, 400)                                 # big full player: the cover isn't capped at 260
    assert mini._mode == "full" and mini.player.cover.width() > 260
    assert sw.MAX_H >= 900


def test_lyrics_from_big_album_art_switch_to_the_player(mini):
    mini.set_card(600, 600)
    mini.set_lyrics(True)
    assert mini._mode == "full" and mini.lyrics.isVisibleTo(mini)
    mini.set_lyrics(False)


def test_mini_player_cannot_be_dragged_off_screen(mini):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QGuiApplication
    area = QGuiApplication.primaryScreen().availableGeometry()
    mini.set_card(340, 138)
    pos = mini.clamped_position(QPoint(area.right() + 500, area.bottom() + 500), area.center())
    assert pos.x() + mini.width() - 2 * 14 <= area.right() + 1
    assert pos.y() + mini.height() - 2 * 14 <= area.bottom() + 1
    pos = mini.clamped_position(QPoint(area.left() - 900, area.top() - 900), area.center())
    assert pos.x() >= area.left() - 14 and pos.y() >= area.top() - 14


def test_spotify_desktop_app_volume_is_spotifys_own(mini):
    p = mini.player
    p._sp_ok = True
    p._app_vol = "opera"
    p._follow_volume({"source": "media", "app": "Spotify", "app_id": "Spotify.exe"})
    assert p._app_vol == ""                               # Spotify's API volume, not the Windows mixer


# ---------------------------------------------------------------- microphone
DEVS = [
    {"name": "Microsoft Sound Mapper - Input", "max_input_channels": 2, "hostapi": 0},
    {"name": "Headset (Seb's AirPods Pro Hands", "max_input_channels": 1, "hostapi": 0},
    {"name": "Microphone (EMEET SmartCam C60E", "max_input_channels": 2, "hostapi": 0},
    {"name": "Voicemeeter Out B1 (VB-Audio Vo", "max_input_channels": 8, "hostapi": 0},
    {"name": "Speakers (Realtek)", "max_input_channels": 0, "hostapi": 0},
]


def test_mic_is_found_by_name_when_airpods_shift_the_indexes():
    from modules.voice.mic_select import pick_input
    idx, name, note = pick_input("Voicemeeter Out B1 (VB-Audio Vo", DEVS, default=1)
    assert idx == 3 and name.startswith("Voicemeeter") and not note


def test_old_index_setting_never_opens_the_airpods_mic():
    from modules.voice.mic_select import pick_input, is_bluetooth_headset
    assert is_bluetooth_headset("Headset (Seb's AirPods Pro Hands")
    assert not is_bluetooth_headset("Headset Microphone (Oculus Virt")
    idx, name, note = pick_input(1, DEVS, default=1)
    assert idx == 2 and "AirPods" in note                 # the webcam mic, not the AirPods
    idx, name, note = pick_input(None, DEVS, default=1)   # Windows made the AirPods the default
    assert idx == 2 and note
    idx, _, note = pick_input(1, DEVS, default=1, allow_bluetooth=True)
    assert idx == 1 and not note


def test_missing_mic_falls_back_to_default():
    from modules.voice.mic_select import pick_input
    idx, name, note = pick_input("USB Mic that was unplugged", DEVS, default=2)
    assert idx == 2 and "isn't connected" in note


# ---------------------------------------------------------------- remote access (UPnP)
DESC = """<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><URLBase>http://192.168.1.1:5000/</URLBase>
<device><deviceList><device><deviceList><device><serviceList><service>
<serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
<controlURL>/ctl/IPConn</controlURL></service></serviceList></device></deviceList></device></deviceList></device></root>"""


def test_upnp_description_and_public_address_checks():
    from modules.link import remote
    url, st = remote.control_url(DESC, "http://192.168.1.1:5000/rootDesc.xml")
    assert url == "http://192.168.1.1:5000/ctl/IPConn" and st.endswith("WANIPConnection:1")
    assert remote.is_public("81.2.69.160")
    for ip in ("192.168.1.5", "10.0.0.2", "172.20.1.1", "100.80.1.1", "127.0.0.1", "", "nonsense"):
        assert not remote.is_public(ip), ip


def test_upnp_mapping_flow(monkeypatch):
    from modules.link import remote
    calls = []

    def soap(url, st, action, args, timeout=4.0):
        calls.append(action)
        if action == "GetExternalIPAddress":
            return {"NewExternalIPAddress": "81.2.69.160"}
        return {}
    monkeypatch.setattr(remote, "discover", lambda **kw: ("http://r/ctl", "urn:x:WANIPConnection:1"))
    monkeypatch.setattr(remote, "_soap", soap)
    ra = remote.RemoteAccess()
    st = ra.open(8765, "192.168.1.5")
    assert st["state"] == "open" and st["address"] == "81.2.69.160" and calls[:2] == ["AddPortMapping",
                                                                                      "GetExternalIPAddress"]
    assert ra.public_address() == "81.2.69.160"
    ra.close()
    assert "DeletePortMapping" in calls and ra.public_address() == ""


def test_upnp_behind_carrier_nat_says_so(monkeypatch):
    from modules.link import remote
    monkeypatch.setattr(remote, "discover", lambda **kw: ("http://r/ctl", "urn:x:WANIPConnection:1"))
    monkeypatch.setattr(remote, "_soap", lambda url, st, action, args, timeout=4.0:
                        {"NewExternalIPAddress": "100.72.3.4"} if action == "GetExternalIPAddress" else {})
    st = remote.RemoteAccess().open(8765, "192.168.1.5")
    assert st["state"] == "blocked" and "carrier" in st["message"].lower()


def test_no_router_support_is_explained(monkeypatch):
    from modules.link import remote
    monkeypatch.setattr(remote, "discover", lambda **kw: None)
    st = remote.RemoteAccess().open(8765, "192.168.1.5")
    assert st["state"] == "unavailable" and "UPnP" in st["message"]


# ---------------------------------------------------------------- logs from every device
def test_log_ring_returns_lines_after_a_cursor():
    import logging
    from core.logger import LogRing
    ring = LogRing(capacity=5)
    lg = logging.getLogger("saint.test_ring")
    lg.addHandler(ring)
    lg.setLevel(logging.INFO)
    try:
        for i in range(8):
            lg.info("line %d", i)
    finally:
        lg.removeHandler(ring)
    out = ring.since(0)
    assert [l.endswith(f"line {i}") for i, l in zip(range(3, 8), out["lines"])] == [True] * 5
    assert out["cursor"] == 8 and out["dropped"] == 3
    assert ring.since(8)["lines"] == [] and len(ring.since(6)["lines"]) == 2


def test_collector_writes_one_file_per_device(tmp_path):
    from modules.link.logs import LogCollector
    c = LogCollector(str(tmp_path))
    c.store("abcd1234ef", "Seb's iPhone", {"lines": ["a", "b"], "cursor": 2, "dropped": 0})
    c.store("abcd1234ef", "Seb's iPhone", {"lines": ["c"], "cursor": 3, "dropped": 0})
    files = [f for f in os.listdir(tmp_path) if f.endswith(".log")]
    assert len(files) == 1 and files[0].startswith("Seb-s-iPhone")
    text = open(tmp_path / files[0], encoding="utf-8").read()
    assert text.splitlines() == ["a", "b", "c"]
    assert c.cursor("abcd1234ef") == 3
    # A restarted device starts counting again: its cursor goes back to the start.
    c.store("abcd1234ef", "Seb's iPhone", {"lines": ["d"], "cursor": 1, "dropped": 0, "boot": "x"})
    assert c.cursor("abcd1234ef") == 1


def test_link_answers_log_get_for_own_devices_only():
    from modules.link.service import LinkService
    from modules.link.identity import Peer
    svc = LinkService.__new__(LinkService)

    class Ctx:
        def __init__(self, role):
            self.peer = Peer(id="p", name="P", public_key="", role=role)
    out = svc._h_log_get(Ctx("own"), {"after": 0})
    assert "lines" in out and "cursor" in out
    from modules.link.wire import LinkError
    with pytest.raises(LinkError):
        svc._h_log_get(Ctx("collaborator"), {"after": 0})


def test_away_from_home_over_ipv6_with_the_address_learnt_from_the_hello():
    import socket
    import time
    if not socket.has_ipv6:
        pytest.skip("no IPv6 on this machine")
    from tests.test_link_node import make, paired
    a, b = make("Home PC"), make("Phone")
    a.port_ = a.start("127.0.0.1", 0)
    b.port_ = b.start("127.0.0.1", 0)
    a.register("echo", lambda ctx, d: {"said": d.get("x")})
    try:
        if not a.start_ipv6():
            pytest.skip("can't listen on IPv6 here")
        a.public_addrs = lambda: ["::1"]                      # what "reach this PC from anywhere" adds
        peer = paired(a, b)
        assert "::1" in b.peers.get(peer.id).addrs            # the phone learnt it at home
        b.close_peer(peer.id)
        time.sleep(0.2)
        b.peers.update(peer.id, host="::1", addrs=["::1"])    # away: only the internet address works
        b.connect(b.peers.get(peer.id))
        assert b.request(peer.id, "echo", {"x": 6})["said"] == 6
        a.stop_ipv6()
        assert not a.ipv6_listening
    finally:
        a.stop()
        b.stop()

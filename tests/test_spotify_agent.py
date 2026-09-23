"""Spotify tools against a fake Web API: real entity resolution, device recovery,
listening memory, honest replies."""

import pytest

from modules.spotify.client import SpotifyAPIError
from modules.spotify.memory import SpotifyMemory
from modules.spotify.tools import SpotifyTools


def _track(tid, name, artist, aid=None):
    return {"id": tid, "uri": f"spotify:track:{tid}", "name": name, "duration_ms": 200000,
            "popularity": 50, "artists": [{"name": artist, "id": aid or artist.lower()}],
            "album": {"name": name + " (album)", "images": []}}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.active_device = True
        self.devices_list = [{"id": "pc", "name": "DESKTOP", "type": "Computer", "is_active": False}]
        self.state = {"is_playing": True, "progress_ms": 10000, "item": _track("t1", "Blinding Lights", "The Weeknd"),
                      "device": {"name": "DESKTOP", "volume_percent": 40}}

    def search(self, q, types="track", limit=10):
        self.calls.append(("search", q, types))
        data = {}
        if "artist" in types:
            data["artists"] = {"items": [{"id": "dp", "uri": "spotify:artist:dp", "name": "Daft Punk", "popularity": 80}]}
        if "track" in types:
            if "weeknd" in q.lower() or "blinding" in q.lower():
                items = [_track("t1", "Blinding Lights", "The Weeknd"), _track("t9", "Blinding Lights (Remix)", "Someone")]
            elif "jazz" in q.lower():
                items = [_track("j1", "So What", "Miles Davis")]
            else:
                items = [_track("t2", "One More Time", "Daft Punk", "dp")]
            data["tracks"] = {"items": items}
        if "album" in types:
            data["albums"] = {"items": [{"id": "al", "uri": "spotify:album:al", "name": "Discovery",
                                         "artists": [{"name": "Daft Punk"}]}]}
        if "playlist" in types:
            data["playlists"] = {"items": [None, {"id": "pj", "uri": "spotify:playlist:pj", "name": "Jazz Classics",
                                                  "description": "the best jazz", "owner": {"display_name": "Spotify"}}]}
        return data

    def playlists(self, limit=50):
        return {"items": [{"id": "gym", "uri": "spotify:playlist:gym", "name": "Gym Hits"},
                          {"id": "chill", "uri": "spotify:playlist:chill", "name": "Chill Evenings"}]}

    def play(self, context_uri=None, uris=None, device_id=None):
        if not self.active_device and device_id is None:
            raise SpotifyAPIError("no device", status=404, reason="NO_ACTIVE_DEVICE")
        self.calls.append(("play", context_uri, tuple(uris or ()), device_id))

    def devices(self):
        return {"devices": self.devices_list}

    def transfer(self, device_id, play=False):
        self.calls.append(("transfer", device_id))
        self.active_device = True

    def playback(self):
        return self.state

    def next(self, device_id=None):
        self.calls.append(("next",))

    def pause(self, device_id=None):
        self.calls.append(("pause",))

    def volume(self, percent, device_id=None):
        self.calls.append(("volume", percent))

    def add_to_playlist(self, pid, uris):
        self.calls.append(("add", pid, tuple(uris)))

    def top_artists(self, *a, **k):
        raise SpotifyAPIError("forbidden", status=403)

    def artist(self, aid):
        return {"genres": ["electronic", "french house"]}


@pytest.fixture
def tools(tmp_path):
    t = SpotifyTools(FakeClient())
    t._memory = SpotifyMemory(str(tmp_path / "spotify.db"))
    return t


def test_play_track_by_artist(tools):
    r = tools.play_query("Blinding Lights by The Weeknd")
    assert r["kind"] == "track" and r["name"] == "Blinding Lights" and r["artist"] == "The Weeknd"
    assert ("play", None, ("spotify:track:t1",), None) in tools.client.calls
    assert tools.memory.recent_requests()[0]["entity_name"] == "Blinding Lights"


def test_play_artist_resolves_artist_context(tools):
    r = tools.play_query("Daft Punk")
    assert r["kind"] == "artist" and ("play", "spotify:artist:dp", (), None) in tools.client.calls


def test_play_user_playlist(tools):
    r = tools.play_query("gym", kind="playlist")
    assert r["name"] == "Gym Hits" and r["owned"]


def test_play_genre(tools):
    r = tools.play_query("jazz", kind="genre")
    assert r["kind"] == "genre" and r["name"] == "Jazz Classics"


def test_no_active_device_is_recovered(tools):
    tools.client.active_device = False
    tools.play_query("Blinding Lights by The Weeknd")
    assert ("transfer", "pc") in tools.client.calls
    assert any(c[0] == "play" and c[3] == "pc" for c in tools.client.calls)


def test_skip_is_remembered(tools):
    tools.next()
    assert "t1" in tools.memory.skipped_track_ids()
    assert ("next",) in tools.client.calls


def test_volume_step(tools):
    assert tools.volume_step("up")["percent"] == 55
    assert tools.volume_step("down", step=10)["percent"] == 30


def test_add_current_to_playlist(tools):
    r = tools.add_current_to_playlist("chill")
    assert r["playlist"] == "Chill Evenings" and ("add", "chill", ("spotify:track:t1",)) in tools.client.calls
    with pytest.raises(SpotifyAPIError) as e:
        tools.add_current_to_playlist("nonexistent mix")
    assert "couldn't find a playlist" in e.value.user_message


def test_recommend_requires_real_history(tools):
    assert tools.recommend()["reason"] == "no_history"
    with pytest.raises(SpotifyAPIError) as e:
        tools.play_recommended()
    assert e.value.code == "NO_HISTORY"
    # After real listening history exists, recommendations come from it.
    for _ in range(3):
        tools.memory.record_listening(_track("t2", "One More Time", "Daft Punk", "dp"),
                                      played_at=__import__("time").time() - 86400 * 2)
    rec = tools.recommend()
    assert rec["recommendations"] and "Daft Punk" in rec["basis"]


def test_poller_records_listening_and_user_skips(tools):
    tools.poll_once()
    assert tools.memory.today()[0]["track_name"] == "Blinding Lights"
    tools.client.state = dict(tools.client.state, item=_track("t3", "Other", "X"), progress_ms=0)
    tools.poll_once()
    assert "t1" in tools.memory.skipped_track_ids()      # changed at ~5% -> user skip


def test_router_reports_real_failure(monkeypatch):
    """When Spotify isn't set up, SAINT says so instead of pretending."""
    from modules.agent.agent import agent
    r = agent.handle("play Daft Punk")
    assert r is not None and not r.ok
    assert "spotify" in r.text.lower() and "playing" not in r.text.lower()

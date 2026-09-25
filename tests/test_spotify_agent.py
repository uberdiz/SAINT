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


# ---------------------------------------------------------------- watching the player
def _skips(tools):
    return tools.memory._query("SELECT track_id, source FROM skips ORDER BY id")


def test_voice_skip_is_counted_once(tools):
    tools.poll_once()                                   # watching t1
    tools.next()                                        # "skip" said to SAINT at 5%
    tools.client.state = dict(tools.client.state, item=_track("t3", "Other", "X"), progress_ms=0)
    tools._saint_skip_at -= 30                          # the next poll comes well after the skip
    tools.poll_once()
    assert _skips(tools) == [{"track_id": "t1", "source": "voice"}]


def test_app_skip_is_recorded_with_its_source(tools):
    tools.poll_once()
    tools.client.state = dict(tools.client.state, item=_track("t3", "Other", "X"), progress_ms=0)
    tools.poll_once()
    assert _skips(tools) == [{"track_id": "t1", "source": "app"}]


def test_mini_player_skip_source(tools):
    tools.next(source="ui")
    assert _skips(tools)[-1]["source"] == "ui"


def test_paused_time_is_not_listening(tools):
    import time
    tools.client.state = dict(tools.client.state, is_playing=False, progress_ms=20000)
    tools.poll_once()
    tools._last["seen_at"] = time.time() - 600          # paused for 10 minutes, then changed
    tools.client.state = dict(tools.client.state, is_playing=True, item=_track("t3", "Other", "X"), progress_ms=0)
    tools.poll_once()
    assert _skips(tools) == [{"track_id": "t1", "source": "app"}]


def test_saint_started_playback_is_not_a_skip(tools):
    tools.poll_once()
    tools.play_query("Daft Punk")                       # SAINT changed the music
    tools.client.state = dict(tools.client.state, item=_track("t2", "One More Time", "Daft Punk"), progress_ms=0)
    tools.poll_once()
    assert _skips(tools) == []


def test_recommendation_feedback_keeps_the_artist(tools):
    import time
    tools._rec_ids["t1"] = {"at": time.time(), "track": {"id": "t1", "name": "Blinding Lights",
                                                          "artist": "The Weeknd"}}
    tools._note_rec_outcome("t1", 5000, 200000, skipped=True)
    assert tools.memory.feedback_scores() == {"The Weeknd": -0.5}


def test_backfill_from_recently_played(tools):
    tools.client.recently_played = lambda limit=20: {"items": [
        {"track": _track("r1", "Earlier", "Somebody"), "played_at": "2026-09-24T10:00:00Z"}]}
    tools.poll_once()
    assert any(r["track_id"] == "r1" and r["source"] == "recently_played" for r in tools.memory.recent(limit=10))


def test_spotify_taste_feeds_seeds(tools):
    tools.client.top_tracks = lambda *a, **k: {"items": [_track("x1", "Hit", "Kendrick Lamar")]}
    tools.client.saved_tracks = lambda *a, **k: {"items": [{"track": _track("x2", "Saved", "Frank Ocean")}]}
    seeds = tools._seed_artists()
    assert seeds.get("Kendrick Lamar", 0) > 0 and seeds.get("Frank Ocean", 0) > 0


def test_banned_artists_are_left_out(tools):
    import time
    tools.memory.record_listening(_track("t2", "One More Time", "Daft Punk", "dp"), played_at=time.time() - 2 * 86400)
    tools.memory.record_listening(_track("t5", "Around the World", "Daft Punk", "dp"),
                                  played_at=time.time() - 3 * 86400)
    assert tools.recommend()["recommendations"]
    tools.ban_artist("Daft Punk")
    assert tools.recommend().get("reason") == "no_history"
    assert tools.unban_artist("Daft Punk")["was_banned"]


@pytest.mark.parametrize("text,kind,kwargs", [
    ("more energetic", "dj", {"mood": "energetic"}),
    ("make it darker", "dj", {"mood": "darker"}),
    ("something chill", "dj", {"mood": "chill"}),
    ("bring the energy back down", "dj", {"mood": "chill"}),
    ("more like the last song", "dj", {"seed_last": True}),
    ("play something I haven't heard", "dj", {"novel": True}),
    ("no more of this artist", "ban", {}),
    ("no more Drake", "ban", {"name": "drake"}),
    ("unban Drake", "unban", {"name": "drake"}),
    ("DJ mode", "play_for_me", {"context": ""}),
])
def test_dj_phrases(text, kind, kwargs):
    from modules.agent.router import spotify_intent
    si = spotify_intent(text)
    assert si is not None and si.kind == kind and si.kwargs == kwargs, (text, si)


@pytest.mark.parametrize("text", ["no more songs like this", "play Drake", "stop the music", "no more notifications"])
def test_dj_does_not_steal(text):
    from modules.agent.router import spotify_intent
    si = spotify_intent(text)
    assert si is None or si.kind not in ("ban", "dj"), (text, si)

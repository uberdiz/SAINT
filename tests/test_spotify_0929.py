"""Fixes from the 2026-09-29 log: songs keep going (auto-queue, 5 at a time),
"queue more like this" doesn't interrupt, mood queues from listening history,
query understanding ("Spanish music", "a playlist called X", "TT May Praguntha,
Bye Bad Bunny"), lyrics (mini player + telling the song apart from the user),
plus the other log bugs (code-dump replies, "No, bro" corrections, "turn it up
to 80%", "type ... in my room ...") and Start with Windows."""

import time

import pytest

from modules.spotify.client import SpotifyAPIError
from modules.spotify.tools import RADIO_BATCH, SpotifyTools


def _track(tid, name, artist, artist_id=None, pop=50):
    return {"id": tid, "uri": f"spotify:track:{tid}", "name": name, "popularity": pop, "duration_ms": 200000,
            "artists": [{"id": artist_id or artist.lower().replace(" ", ""), "name": artist}],
            "album": {"name": "Album"}}


BAD_BUNNY = [_track("titi", "Tití Me Preguntó", "Bad Bunny", "bb", 85),
             _track("casar", "NO ME QUIERO CASAR", "Bad Bunny", "bb", 70),
             _track("dtmf", "DtMF", "Bad Bunny", "bb", 90)]
SIMILAR = [_track(f"s{i}", f"Similar {i}", f"Artist {i % 7}", f"a{i % 7}", 60) for i in range(30)]


class FakeClient:
    def __init__(self):
        self.calls = []
        self.state_item = BAD_BUNNY[0]

    # --- search / catalogue -------------------------------------------------
    def search(self, q, types="track", limit=10, offset=0):
        self.calls.append(("search", q, types, offset))
        ql = q.lower()
        data = {}
        if "artist" in types.split(","):
            items = [{"id": "bb", "uri": "spotify:artist:bb", "name": "Bad Bunny", "popularity": 95}] \
                if "bad bunny" in ql else []
            data["artists"] = {"items": items}
        if "track" in types.split(","):
            if 'artist:"bad bunny"' in ql:
                items = BAD_BUNNY if offset == 0 else []
            elif "bad bunny" in ql:
                items = [BAD_BUNNY[1], _track("x", "Pink Notes", "DigDat")]
            elif "stand by me" in ql:
                items = [_track("sbm", "Stand By Me", "Ben E. King")]
            elif ql.startswith("artist:") or ql.startswith("genre:"):
                items = SIMILAR[:10]
            else:
                items = [_track("la", "La Chona", "Los Tucanes De Tijuana")]
            data["tracks"] = {"items": items}
        if "album" in types.split(","):
            data["albums"] = {"items": []}
        if "playlist" in types.split(","):
            if "spanish" in ql:
                items = [{"id": "hs", "uri": "spotify:playlist:hs", "name": "Hispanic songs", "description": "",
                          "owner": {"id": "u1"}, "tracks": {"total": 60}},
                         {"id": "sp", "uri": "spotify:playlist:sp", "name": "Hype spanish music", "description": "",
                          "owner": {"id": "u2"}, "tracks": {"total": 80}}]
            elif "dominican" in ql:
                items = [{"id": "dd", "uri": "spotify:playlist:dd", "name": "Dominican Dembow", "description": "",
                          "owner": {"id": "u3"}, "tracks": {"total": 40}}]
            elif "titi" in ql or "bad bunny" in ql:
                items = [{"id": f"p{i}", "uri": f"spotify:playlist:p{i}", "name": f"Reggaeton {i}",
                          "owner": {"id": "u"}, "tracks": {"total": 50}} for i in range(3)]
            else:
                items = []
            data["playlists"] = {"items": items}
        return data

    def playlist_items(self, pid, limit=50):
        return {"items": [{"track": t} for t in [BAD_BUNNY[0]] + SIMILAR[:12]]}

    def artist_top_tracks(self, artist_id):
        return BAD_BUNNY if artist_id == "bb" else []

    def artist(self, artist_id):
        return {"genres": ["reggaeton", "latin"]}

    def playlists(self, limit=50, offset=0):
        return {"items": []}

    def me(self):
        return {"id": "me"}

    def top_artists(self, *a, **k):
        return {"items": []}

    def top_tracks(self, *a, **k):
        return {"items": []}

    def saved_tracks(self, *a, **k):
        return {"items": []}

    def request(self, method, path, **kw):
        return ({}, 0)

    # --- player ---------------------------------------------------------------
    def play(self, context_uri=None, uris=None, device_id=None):
        self.calls.append(("play", context_uri, tuple(uris or ())))

    def queue(self, uri, device_id=None):
        self.calls.append(("queue", uri))

    def next(self, device_id=None):
        self.calls.append(("next",))

    def playback(self):
        return {"is_playing": True, "progress_ms": 1000, "item": self.state_item,
                "device": {"name": "PC", "volume_percent": 50}}

    def played(self):
        return [c for c in self.calls if c[0] == "play"]

    def queued(self):
        return [c[1] for c in self.calls if c[0] == "queue"]


@pytest.fixture
def sp(tmp_path, monkeypatch):
    from modules.spotify.memory import SpotifyMemory
    t = SpotifyTools(FakeClient())
    t._memory = SpotifyMemory(str(tmp_path / "sp.db"))
    monkeypatch.setattr(t, "_refresh_soon", lambda *a, **k: None)
    return t


def _wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------ query understanding
def test_misheard_title_by_artist_plays_that_artists_song(sp):
    for q in ("Titi Me Pragunta by Bad Bunny", "TT May Praguntha, Bye Bad Bunny", "to Team Efragonta, bye bad bunny"):
        ent = sp.resolve(q)
        assert ent["kind"] == "track" and ent["name"] == "Tití Me Preguntó", (q, ent.get("name"))


def test_stand_by_me_is_a_title_not_a_split(sp):
    ent = sp.resolve("Stand By Me")
    assert ent["name"] == "Stand By Me"


def test_spanish_music_plays_a_playlist_not_one_song(sp):
    for q in ("SPANISH MUSIC", "more Spanish music", "Spanish"):
        ent = sp.resolve(q)
        assert ent["kind"] == "genre" and ent.get("context"), (q, ent)
        assert "spanish" in ent["name"].lower()


def test_a_playlist_called_x(sp):
    ent = sp.resolve("a playlist called Dominican Dembeau")
    assert ent["kind"] == "playlist" and ent["name"] == "Dominican Dembow"


# ------------------------------------------------------------------ auto-queue
def test_playing_a_song_queues_similar_songs_five_at_a_time(sp):
    r = sp.play_query("Titi Me Pregunto by Bad Bunny")
    assert r["radio"] and sp.client.played()[-1][2] == ("spotify:track:titi",)
    assert _wait_for(lambda: len(sp.client.queued()) >= RADIO_BATCH)
    queued = sp.client.queued()
    assert len(queued) == RADIO_BATCH and "spotify:track:titi" not in queued
    # Top-up: when the second-last queued song plays, five more go in.
    radio = sp._radio
    sp._radio_tick({"id": radio["queued"][-2], "is_playing": True}, None)
    assert _wait_for(lambda: len(sp.client.queued()) >= 2 * RADIO_BATCH)
    assert len(set(sp.client.queued())) == len(sp.client.queued())        # never the same song twice


def test_playing_a_playlist_stops_the_auto_queue(sp):
    sp.play_query("Titi Me Pregunto by Bad Bunny")
    assert sp._radio is not None
    sp.play_query("SPANISH MUSIC")
    assert sp._radio is None


def test_user_moving_on_stops_radio_but_never_skips_what_is_queued(sp):
    # 2026-10-01: noticing "something else" playing used to mark the whole auto-queue
    # as leftovers, and SAINT then skipped song after song the user wanted.
    sp.play_query("Titi Me Pregunto by Bad Bunny")
    assert _wait_for(lambda: len(sp.client.queued()) >= RADIO_BATCH)
    queued = sp._radio["queued"][2]
    sp._radio["started"] -= 60
    sp._radio_tick({"id": "something-else", "is_playing": True}, None)
    assert sp._radio is None
    sp._radio_tick({"id": queued, "is_playing": True}, None)
    assert ("next",) not in sp.client.calls


def test_a_queued_song_playing_under_another_id_keeps_the_radio(sp):
    """Spotify relinks tracks per market: the id that plays isn't always the one queued."""
    sp.play_query("Titi Me Pregunto by Bad Bunny")
    assert _wait_for(lambda: len(sp.client.queued()) >= RADIO_BATCH)
    r = sp._radio
    r["started"] -= 60
    rec = sp._rec_ids[r["queued"][1]]["track"]
    item = {"id": "relinked-id", "name": rec["name"], "artists": [{"name": rec["artist"]}]}
    sp._radio_tick({"id": "relinked-id", "is_playing": True, "item": item}, None)
    assert sp._radio is r and r["last_index"] == 1
    linked = {"id": "other-id", "name": "x", "artists": [], "linked_from": {"id": r["queued"][2]}}
    sp._radio_tick({"id": "other-id", "is_playing": True, "item": linked}, None)
    assert sp._radio is r and r["last_index"] == 2


def test_leftover_skips_never_turn_into_a_skip_storm(sp):
    sp.play_query("Titi Me Pregunto by Bad Bunny")
    assert _wait_for(lambda: len(sp.client.queued()) >= RADIO_BATCH)
    leftovers = list(sp._radio["queued"])
    sp.play_query("Stand By Me")                       # replaced by SAINT: those are leftovers now
    for tid in leftovers:
        sp._radio_tick({"id": tid, "is_playing": True}, None)
    assert sp.client.calls.count(("next",)) == 3
    assert not sp._radio_orphans


def test_queue_more_like_this_keeps_the_song_playing(sp):
    r = sp.queue_similar()
    assert r["count"] == RADIO_BATCH and len(sp.client.queued()) == RADIO_BATCH
    assert not sp.client.played()                  # nothing was interrupted
    assert sp._radio is not None and sp._radio["similar"]


def test_mood_queue_is_built_from_the_users_own_listening(sp):
    mem = sp.memory
    for i in range(6):
        t = _track(f"fav{i}", f"Fav {i}", "Chill Artist", "chilla")
        for _ in range(3):
            mem.record_listening(t, played_at=time.time() - 86400 * (i + 1) - _ * 900)
    mem.cache_artist_genres("chilla", "Chill Artist", ["bedroom pop", "chill r&b"])
    r = sp.play_recommended(mood="chill")
    assert sp.client.played()[-1][2][0].startswith("spotify:track:")
    assert len(sp.client.played()[-1][2]) == 1                  # one song starts...
    assert len(sp.client.queued()) == RADIO_BATCH               # ...and five are queued behind it
    picked = [sp.client.played()[-1][2][0]] + sp.client.queued()
    assert any(u.startswith("spotify:track:fav") for u in picked)
    assert r["mood"] == "chill"


# ------------------------------------------------------------------ router
@pytest.mark.parametrize("said,kind", [
    ("Cue more songs like this.", "queue_similar"),
    ("queue more songs like this", "queue_similar"),
    ("more like this", "queue_similar"),
    ("make me a chill queue", "mood"),
    ("play something for my mood", "mood"),
    ("I'm feeling sad, play something", "mood"),
    ("stop the auto queue", "stop_autoqueue"),
    ("Turn it up to 80%.", "volume_set"),
    ("turn up to AD", "volume_set"),
])
def test_router_music_intents(said, kind):
    from modules.agent.router import spotify_intent
    si = spotify_intent(said)
    assert si is not None and si.kind == kind, (said, si)


def test_router_does_not_mistake_these():
    from modules.agent.router import route_single, spotify_intent
    assert spotify_intent("I hate this game.") is None
    assert spotify_intent("I'm tired") is None
    it = route_single("Turn it up to 80%.")
    assert it.name != "audio.app_volume"
    assert route_single("Turn on the lyrics for Spotify.").name == "ui.lyrics"
    assert route_single("hide the lyrics").name == "ui.lyrics"
    assert route_single("what are the lyrics to this song") is None or \
        route_single("what are the lyrics to this song").name != "ui.lyrics"


def test_playlist_phrasing_in_router():
    from modules.agent.router import spotify_intent
    si = spotify_intent("Play a playlist called Dominican Dembeau")
    assert si.kwargs == {"query": "Dominican Dembeau", "kind": "playlist"}
    assert spotify_intent("Play A Spanish Playlist").kwargs["kind"] == "playlist"


# ------------------------------------------------------------------ lyrics
LRC = "[00:10.00] Wake up, wake up\n[00:14.50] Open up your eyes\n[00:18.00] The sun is coming up\n" \
      "[01:30.00] Different verse entirely here\n"


def test_parse_lrc_and_line_at():
    from modules.spotify.lyrics import Lyrics, parse_lrc
    ly = Lyrics("wake up", "Lil Darkie", parse_lrc(LRC))
    assert ly.synced[1] == (14500, "Open up your eyes")
    assert ly.index_at(15000) == 1 and ly.index_at(5000) == -1


def test_heard_lyric_is_recognised_near_the_position_only():
    from modules.spotify.lyrics import Lyrics, is_lyric, parse_lrc
    ly = Lyrics("wake up", "Lil Darkie", parse_lrc(LRC))
    assert is_lyric("Open up your eyes.", ly, pos_ms=16000)
    assert not is_lyric("Open up your eyes.", ly, pos_ms=200000)       # far from that line
    assert not is_lyric("open spotify on my right screen", ly, pos_ms=16000)
    assert not is_lyric("skip", ly, pos_ms=16000)


def test_voice_gate_drops_the_song_singing(monkeypatch):
    from modules.spotify.lyrics import Lyrics, lyrics_service, parse_lrc
    from modules.voice.module import VoiceModule
    vm = VoiceModule.__new__(VoiceModule)
    vm._music_playing = True
    monkeypatch.setattr(VoiceModule, "_is_command", staticmethod(lambda text: True))
    monkeypatch.setattr(lyrics_service, "_current", Lyrics("wake up", "Lil Darkie", parse_lrc(LRC)))
    monkeypatch.setattr(lyrics_service, "_current_pos", (15000, time.time(), False))
    ok, reason = vm._passes_activation_gate("Open up your eyes.", 0.43, follow_up=True)
    assert not ok and reason == "song_lyrics"
    ok, _ = vm._passes_activation_gate("Pause.", 0.43, follow_up=True)
    assert ok


# ------------------------------------------------------------------ other log bugs
def test_code_dump_replies_are_not_spoken():
    from modules.agent.output import clean_reply
    assert clean_reply('{"type":"function","name":"spotify__queue","parameters{"}}', "hi") == ""
    assert clean_reply("Here is the code that corresponds to the provided specification:\n\npython\n{\n"
                       '  "type": "function",\n  "parameters": {"query": "x", "own_only": True}\n}', "hi") == ""
    assert clean_reply('{"name": "prompt", "parameters": {"result": "It is 828 meters tall."}}', "hi") == \
        "It is 828 meters tall."
    assert clean_reply("Kendrick Lamar is from Compton.", "hi") == "Kendrick Lamar is from Compton."


def test_no_bro_and_forget_that_are_not_corrections():
    from modules.learning.corrections import CorrectionTracker
    c = CorrectionTracker()
    c.note("Play TT May Praguntha, Bye Bad Bunny!", "spotify.play", True)
    assert c.detect("No, bro") is None
    assert c.detect("No, don't do that. Forget that.") is None
    assert c.detect("no, play my moe playlist") == "play my moe playlist"


def test_forget_that_after_no(monkeypatch):
    from modules.learning import intents
    monkeypatch.setattr(intents.skills, "last_learned", object(), raising=False)
    monkeypatch.setattr(intents.skills, "last_learned_at", time.time(), raising=False)
    assert intents.parse("No, don't do that. Forget that.") == ("forget_last", "")


def test_type_a_long_sentence_has_no_target():
    from modules.agent.router import parse_desktop
    said = "type I want to turn off my LED lights in my room through my PC how would I go about doing that?"
    it = parse_desktop(said)
    assert it is not None and it.name == "desktop.type_text"
    # The closure holds the kwargs: no "target" means it types the whole sentence where the cursor is.
    kwargs = next(c.cell_contents for c in it.run.__closure__ if isinstance(c.cell_contents, dict))
    assert "target" not in kwargs and kwargs["text"].endswith("doing that?")


# ------------------------------------------------------------------ start with Windows
def test_autostart_shortcut(monkeypatch, tmp_path):
    from core import autostart
    from core.config import config
    made = {}
    monkeypatch.setattr(autostart, "_programs_dir", lambda: tmp_path)

    def fake_make(path, target, args, workdir, icon):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("lnk")
        made[path.name if path.parent.name != "Startup" else "startup"] = (target, args)
    monkeypatch.setattr(autostart, "_make_shortcut", fake_make)
    ok, msg = autostart.set_enabled(True)
    assert ok and autostart.is_enabled() and config.get("system.start_with_windows") is True
    target, args = made["startup"]
    assert target.lower().endswith(("pythonw.exe", "python.exe", "saint.exe")) and "--background" in args
    ok, _ = autostart.set_enabled(False)
    assert ok and not autostart.is_enabled() and config.get("system.start_with_windows") is False


def test_a_leftover_asked_for_by_name_is_not_skipped(sp):
    sp.play_query("Titi Me Pregunto by Bad Bunny")
    assert _wait_for(lambda: len(sp.client.queued()) >= RADIO_BATCH)
    sp.play_query("SPANISH MUSIC")                     # radio replaced: its queue is leftover now
    sp._radio_orphans["sbm"] = time.time()
    sp.play_query("Stand By Me")
    sp._radio_tick({"id": "sbm", "is_playing": True}, None)
    assert ("next",) not in sp.client.calls


def test_smart_shuffle_on_a_single_song_says_why_instead_of_cycling(sp, monkeypatch):
    clicks = []
    monkeypatch.setattr(sp, "_spotify_shuffle_label",
                        lambda click=False: clicks.append(click) or "enable shuffle")
    r = sp.smart_shuffle(True)                     # FakeClient playback has no playlist context
    assert r.get("unsupported") and not any(clicks)
    from modules.agent.router import SpotifyIntent, _spotify_reply
    assert "playlist" in _spotify_reply(SpotifyIntent("smart_shuffle", "", {}), r)


def test_browser_lead_in_and_comma_in_search_query():
    from modules.agent.router import route
    it = route("Use my browser, make a new tab, and search for Backboard Defense Rocket League Codes, "
               "training pack codes.")
    assert it is not None and it.name == "composite:browser.new_tab+browser.search"

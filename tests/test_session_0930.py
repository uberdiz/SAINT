"""Fixes from the 2026-09-30 log and requests: no actions the user didn't ask
for (scene steps run literally, plans can't invent extractions / Steam / web
searches), exact-path launching, Spotify (own playlist first, newest / misheard
albums, the queue, playlist skip dislikes, singing along), volume ("turn it
down" = the PC via Voicemeeter, "a little bit"), Task Manager by voice, speech
of ALL-CAPS titles, the open-mic gate, the focus guard, messaging apps and
learning from complaints and rephrasings."""

import os
import time

import numpy as np
import pytest

from core.config import config
from modules.agent.router import Reply, route


# ---------------------------------------------------------------- no unrequested actions
def test_planner_rejects_steps_nobody_asked_for():
    from modules.learning.planner import grounded, worth_planning
    req = r'Open Bloxstrap at "C:\Users\me\Downloads\Bloxstrap-v2.11.3.exe"'
    for step in ("open my downloads folder", "extract the latest download to my games folder",
                 "open my steam library", "search steam for bloxstrap"):
        assert not grounded(step, req), step
    assert not worth_planning(req)                       # an exact path: nothing to work out
    album = "play Pink Panther's newest music, newest album."
    for step in ("search google for pink panther music", "open youtube",
                 "search youtube for pink panther latest album"):
        assert not grounded(step, album), step
    assert grounded("play pinkpantheress", album)
    assert not grounded("turn down spotify", "lower volume")
    assert grounded("double click the recycle bin", "tidy my screen and open the bin")
    assert grounded("extract the latest download to my games folder", "unzip my latest download into games")


def test_music_request_is_only_retried_as_music(monkeypatch):
    from modules.learning import planner
    monkeypatch.setitem(config._data.setdefault("learning", {}), "planner", True)
    monkeypatch.setattr(planner, "plan", lambda *a, **k: ["open youtube"])
    ran = []
    import modules.agent.router as router
    monkeypatch.setattr(router, "run_plan", lambda intents: ran.append(intents) or Reply("done"))
    assert planner.attempt("play fancy that by pink panther", "Nothing found", domain="spotify") is None
    assert not ran


def test_scene_steps_run_literally(monkeypatch):
    from modules.agent.agent import agent
    from modules.learning import planner
    monkeypatch.setattr(planner, "attempt", lambda *a, **k: pytest.fail("a scene step must never be planned"))
    out = agent.run_command("wiggle the thingamajig sideways")
    assert "didn't" in out.lower() or "recognise" in out.lower()


def test_open_exact_path(tmp_path):
    exe = tmp_path / "Bloxstrap-v2.11.3.exe"
    exe.write_bytes(b"")
    it = route(f'Open Bloxstrap at "{exe}"')
    assert it is not None and it.name == "desktop.open_path"
    from modules.desktop.apps import app_catalog
    e = app_catalog.resolve(f'"{exe}"')
    assert e.kind == "path" and e.target == str(exe)
    assert route(r'open "C:\definitely\not\here.exe"').run().ok is False


def test_custom_app_path_quotes_are_stripped(monkeypatch):
    monkeypatch.setitem(config._data.setdefault("desktop", {}), "apps", {"Bloxtrap": '"C:\\x\\Bloxstrap.exe"'})
    from modules.desktop.apps import app_catalog
    assert app_catalog.resolve("bloxtrap").target == "C:\\x\\Bloxstrap.exe"


# ---------------------------------------------------------------- Spotify
from tests.test_spotify_0929 import FakeClient, _track  # noqa: E402


class Client0930(FakeClient):
    def __init__(self):
        super().__init__()
        self.removed = []

    def playlists(self, limit=50, offset=0):
        if offset:
            return {"items": []}
        return {"items": [{"id": "rr", "uri": "spotify:playlist:rr", "name": "Radiohead Radio",
                           "owner": {"id": "me"}, "tracks": {"total": 40}},
                          {"id": "gym", "uri": "spotify:playlist:gym", "name": "Gym", "owner": {"id": "me"},
                           "tracks": {"total": 20}}]}

    def search(self, q, types="track", limit=10, offset=0):
        ql = q.lower()
        if types == "artist" and "pink panther" in ql:
            return {"artists": {"items": [{"id": "pp", "uri": "spotify:artist:pp", "name": "PinkPantheress",
                                           "popularity": 80}]}}
        if types == "album":
            return {"albums": {"items": []}}
        return super().search(q, types, limit, offset)

    def artist_albums(self, artist_id, limit=10):
        if artist_id != "pp":
            return []
        me = [{"id": "pp", "name": "PinkPantheress"}]
        return [{"id": "ft", "uri": "spotify:album:ft", "name": "Fancy That", "release_date": "2025-05-09",
                 "album_type": "album", "artists": me},
                {"id": "hm", "uri": "spotify:album:hm", "name": "Heaven knows", "release_date": "2023-11-10",
                 "album_type": "album", "artists": me}]

    def get_queue(self):
        return {"currently_playing": _track("c", "Now", "Someone"),
                "queue": [_track(f"q{i}", f"Song {i}", f"Artist {i}") for i in range(7)]}

    def remove_from_playlist(self, pid, uris):
        self.removed.append((pid, tuple(uris)))

    def volume(self, percent, device_id=None):
        self.calls.append(("volume", percent))


@pytest.fixture
def sp(tmp_path, monkeypatch):
    from modules.spotify.memory import SpotifyMemory
    from modules.spotify.tools import SpotifyTools
    t = SpotifyTools(Client0930())
    t._memory = SpotifyMemory(str(tmp_path / "sp.db"))
    monkeypatch.setattr(t, "_refresh_soon", lambda *a, **k: None)
    return t


def test_own_playlist_beats_the_artist(sp):
    r = sp.play_query("radio head radio")
    assert r["kind"] == "playlist" and r["name"] == "Radiohead Radio"


def test_newest_album_of_a_misheard_artist(sp):
    assert sp.resolve("Pink Panther's newest music, newest", "album")["name"] == "Fancy That"
    assert sp.resolve("Pink Panther says fancy that", "album")["name"] == "Fancy That"


def test_queue_is_listed_without_touching_the_screen(sp):
    it = route("list my queue on spotify")
    assert it.name == "spotify.queue_list"
    r = sp.queue_list()
    assert r["total"] == 7 and r["queue"][0]["name"] == "Song 0"
    from modules.agent.router import SpotifyIntent, _spotify_reply
    text = _spotify_reply(SpotifyIntent("queue_list", "spotify.queue_list", {}), r)
    assert text.startswith("Up next: Song 0 by Artist 0") and "2 more" in text


def test_a_song_always_skipped_in_a_playlist_is_learned_and_skipped(sp):
    ctx = "spotify:playlist:gym"
    bad = _track("bad", "Bad Song", "Meh")
    for _ in range(2):
        sp.memory.record_listening(bad, context_uri=ctx, played_at=time.time() - 100 * _)
        sp.memory.record_skip(bad, 12000, context_uri=ctx)
    assert sp._check_playlist_dislike(bad, ctx)
    assert "bad" in sp.memory.playlist_dislikes(ctx)
    st = {"id": "bad", "is_playing": True, "context_uri": ctx}
    assert sp._disliked_here(st)
    sp._played_by_request_at = time.time()                # asked for by name: not skipped
    assert not sp._disliked_here(st)
    assert not sp._check_playlist_dislike(_track("ok", "Fine", "X"), ctx)


def test_remove_the_playing_song_from_its_playlist(sp, monkeypatch):
    item = _track("bad", "Bad Song", "Meh")
    monkeypatch.setattr(sp, "_state", lambda force=False: {"uri": item["uri"], "id": "bad", "track": "Bad Song",
                                                           "context_uri": "spotify:playlist:gym", "item": item,
                                                           "progress_ms": 1000, "duration_ms": 200000})
    monkeypatch.setattr(sp, "next", lambda source="voice": None)
    r = sp.remove_current_from_playlist()
    assert r["playlist"] == "Gym" and sp.client.removed == [("gym", ("spotify:track:bad",))]
    assert route("remove this song from the playlist").name == "spotify.playlist_remove"


def test_singing_along_counts_once_per_play(sp):
    sp._last = {"id": "t", "item": _track("t", "Tune", "Band")}
    assert sp.note_sang_along("la la la")
    assert not sp.note_sang_along("la la la")
    assert sp.memory.feedback_scores().get("Band", 0) > 0


# ---------------------------------------------------------------- volume
@pytest.mark.parametrize("text,name", [
    ("turn it down", "desktop.volume"), ("turn it up a little bit", "desktop.volume"),
    ("make it a little louder", "desktop.volume"), ("turn it way up", "desktop.volume"),
    ("turn spotify down", "spotify.volume_down"), ("turn the music up a little", "spotify.volume_up"),
])
def test_volume_targets(text, name):
    assert route(text).name == name


def test_a_little_means_a_small_step():
    from modules.agent.desktop_intents import volume_step_points
    assert volume_step_points("turn it up a little bit") == 5
    assert volume_step_points("turn it up") == 10
    assert volume_step_points("turn it up a lot") == 20
    it = route("turn spotify up a little bit")
    assert it.name == "spotify.volume_up"


def test_voicemeeter_gain_percent_round_trip():
    from modules.desktop.voicemeeter import gain_to_percent, percent_to_gain
    assert gain_to_percent(0) == 100 and gain_to_percent(-60) == 0
    assert abs(gain_to_percent(percent_to_gain(50)) - 50) <= 1


# ---------------------------------------------------------------- Task Manager
@pytest.mark.parametrize("text,name", [
    ("end task on discord", "system.end_task"), ("force quit roblox", "system.end_task"),
    ("what's running", "system.processes"), ("what's using my ram", "system.processes"),
    ("why is my pc so slow", "system.processes"), ("device health", "system.health"),
    ("how's my pc doing", "system.health"), ("what's my gpu temperature", "system.health"),
    ("list my startup apps", "system.startup_apps"),
])
def test_task_manager_phrases(text, name):
    assert route(text).name == name


def test_end_task_always_asks_and_protects_windows():
    from core.permissions import ALWAYS_CONFIRM
    assert "system.end_task" in ALWAYS_CONFIRM
    from modules.desktop import taskmgr
    assert "explorer" in taskmgr.PROTECTED and "svchost" in taskmgr.PROTECTED


def test_processes_are_grouped_by_program():
    from modules.desktop import taskmgr
    r = taskmgr.processes("memory", 5)
    assert r["processes"] and all(p["count"] >= 1 for p in r["processes"])
    assert not any(p["process"] in ("system idle process", "memcompression") for p in r["processes"])


# ---------------------------------------------------------------- speech
@pytest.mark.parametrize("text,said", [
    ("Playing NO ME QUIERO CASAR by Bad Bunny.", "Playing No Me Quiero Casar by Bad Bunny."),
    ("SAINT is ready on your PC.", "Saint is ready on your PC."),
    ("Your GPU is at 60 percent.", "Your GPU is at 60 percent."),
    ("Skipped. Now playing BIRDS OF A FEATHER.", "Skipped. Now playing Birds Of A Feather."),
    ("Playing DtMF.", "Playing DtMF."),
])
def test_caps_are_said_as_words(text, said):
    from modules.voice.speech_text import for_speech
    assert for_speech(text) == said


def test_kokoro_voice_blend():
    from modules.voice.voices import kokoro_voice
    assert kokoro_voice("af_heart") == "af_heart"
    assert kokoro_voice("af_heart", "am_michael", 25) == "af_heart,af_heart,af_heart,am_michael"
    assert kokoro_voice("af_heart", "am_michael", 75) == "af_heart,am_michael,am_michael,am_michael"


# ---------------------------------------------------------------- voice gate / barge-in / profile
@pytest.fixture
def voice():
    from modules.voice.module import VoiceModule
    v = VoiceModule.__new__(VoiceModule)
    v._wake = None                                   # wake word off: open mic
    return v


@pytest.mark.parametrize("text", ["this is tough.", "INTRO MUSIC", "wow", "I can't leave you alone"])
def test_open_mic_ignores_room_talk_and_lyrics(voice, text):
    ok, reason = voice._passes_activation_gate(text, 0.9)
    assert not ok and reason.startswith("open_mic"), (text, reason)


@pytest.mark.parametrize("text", ["open notepad", "skip this song", "What's the weather tomorrow?"])
def test_open_mic_still_takes_commands_and_questions(voice, text):
    ok, reason = voice._passes_activation_gate(text, 0.9)
    assert ok, (text, reason)


def test_barge_in_needs_a_real_voice_level():
    from core.audio_echo import EchoGate
    g = EchoGate(margin=2.5, min_ms=90, frame_ms=30)
    g.speech_floor = 0.03
    assert not any(g.update(0.012, 0.0) for _ in range(20))          # music / room noise
    g.reset_run()
    assert any(g.update(0.12, 0.02) for _ in range(10))              # the user talking over SAINT


def test_stop_words_are_not_saints_own_echo(voice):
    voice._tts_text = "Stopped the timer. Anything else?"
    assert voice._is_own_echo("stopped the timer")
    assert not voice._is_own_echo("stop")
    assert voice._STOP_PHRASE.search("okay stop talking")


def test_voice_profile_learns_and_recognises(tmp_path):
    from modules.voice.speaker import MIN_SAMPLES, SpeakerProfile
    rng = np.random.default_rng(0)
    t = np.arange(16000 * 2) / 16000.0

    def voice_like(f0):
        sig = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 12))
        env = (np.sin(2 * np.pi * 3 * t) > -0.3).astype(np.float32)
        return (0.1 * sig * env + 0.003 * rng.standard_normal(len(t))).astype(np.float32)
    p = SpeakerProfile(str(tmp_path / "vp.json"))
    for _ in range(MIN_SAMPLES + 2):
        assert p.learn(voice_like(110 + rng.uniform(-3, 3)))
    assert p.ready
    assert p.score(voice_like(111)) > p.score(voice_like(260))
    assert SpeakerProfile(str(tmp_path / "vp.json")).samples == MIN_SAMPLES + 2     # persisted (numbers only)


# ---------------------------------------------------------------- focus guard
def test_focus_guard_blocks_unrequested_windows_while_busy(monkeypatch):
    from core.focus_guard import FocusGuard
    g = FocusGuard()
    monkeypatch.setitem(config._data.setdefault("desktop", {}), "focus_guard", True)
    monkeypatch.setattr(FocusGuard, "busy", staticmethod(lambda: "you're playing Roblox"))
    g.begin("play fancy that by pinkpantheress")
    assert "Roblox" in g.refuse("desktop.web_search")
    assert g.refuse("spotify.play_query") is None                 # no window involved
    g.begin("open youtube")
    assert g.refuse("desktop.web_search") is None                 # asked for a window
    g.begin("whatever", explicit=True)                            # a scene the user ran
    assert g.refuse("desktop.open_app") is None


# ---------------------------------------------------------------- messaging apps
@pytest.mark.parametrize("text,name", [
    ("open what my john (gian) sent me on instagram", "social.open_chat"),
    ("open my chat with gian on discord", "social.open_chat"),
    ("check my instagram dms", "social.open_inbox"),
])
def test_messaging_phrases(text, name):
    assert route(text).name == name


def test_bracketed_name_is_the_one_meant():
    from modules.agent.social_intents import _clean_name
    assert _clean_name("my john (gian)") == "gian"


# ---------------------------------------------------------------- learning from mistakes
@pytest.fixture
def learner(tmp_path, monkeypatch):
    from modules.learning import feedback as fb
    from modules.learning.skills import SkillStore
    monkeypatch.setitem(config._data.setdefault("learning", {}), "from_mistakes", True)
    store = SkillStore(str(tmp_path / "skills.json"))
    monkeypatch.setattr("modules.learning.skills.skills", store)
    monkeypatch.setattr(fb, "journal", fb.Journal(str(tmp_path / "journal.jsonl")))
    return fb.FeedbackLearner(), store, fb


def test_complaint_unlearns_a_planned_recipe(learner):
    fl, store, fb = learner
    store.learn("click launch roblox", ["open my steam library", "search steam for roblox"], "planned")
    fl.note_result("click launch roblox", "learning.planned", True, "Searched Steam.",
                   steps=["open my steam library", "search steam for roblox"])
    note = fl.check_complaint("no, I didn't ask for that")
    assert note and store.match("click launch roblox") is None
    assert "WRONG" in fb.journal.lessons()


def test_taught_recipes_only_get_a_strike(learner):
    fl, store, _fb = learner
    store.learn("play my kpop playlist", ["play my yuh playlist"], "corrected")
    fl.note_result("play my kpop playlist", "learning.skill", True, steps=["play my yuh playlist"])
    assert fl.check_complaint("that's wrong") is None
    assert store.match("play my kpop playlist") is not None


def test_a_rephrase_that_works_is_learned(learner, monkeypatch):
    fl, store, fb = learner
    fl.note_result("play pink panther says fancy that album", "spotify.play_album", False, "Nothing found")
    fl.note_result("play fancy that by pinkpantheress", "spotify.play_track", True, "Playing.")
    sk = store.match("play pink panther says fancy that album")
    assert sk is not None and sk.steps == ["play fancy that by pinkpantheress"] and sk.how == "rephrased"
    assert any(r["kind"] == "rephrase_fixed" for r in fb.journal.recent())


def test_unrelated_next_request_is_not_a_rephrase(learner):
    fl, store, _fb = learner
    fl.note_result("open the thingamajig", "learning.unknown", False, "I don't know how")
    fl.note_result("what time is it", "time", True, "It's 5.")
    assert store.match("open the thingamajig") is None


def test_complaint_detection():
    from modules.learning.feedback import is_complaint
    for t in ("no", "that's wrong", "I didn't ask for that", "why did you do that", "no no no",
              "that's not what I asked"):
        assert is_complaint(t), t
    for t in ("no more drake", "play something", "nothing much"):
        assert not is_complaint(t), t

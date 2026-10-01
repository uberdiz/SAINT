"""2026-09-30 evening: background audio and quiet voices kept out, one-word
answers and "SHUT UP SAINT" while it talks, "turn it back up", "that's not a
chill song", "his name is ...", being told what to do after "I don't know how",
routines SAINT notices by itself, the mini player during a game, and SAINT.exe."""

import json
import struct
import time

import pytest

from core.config import config
from modules.agent.router import route


# ---------------------------------------------------------------- listening
@pytest.fixture
def voice(monkeypatch):
    from modules.voice.module import VoiceModule
    from modules.voice.output_policy import output_policy
    v = VoiceModule.__new__(VoiceModule)
    v._wake = None                                   # wake word off: open mic
    v._music_playing = False
    monkeypatch.setattr(VoiceModule, "_ai_expects_reply", staticmethod(lambda: False))
    monkeypatch.setattr(VoiceModule, "_question_pending", staticmethod(lambda: False))
    monkeypatch.setattr(VoiceModule, "_answers_set_aside", staticmethod(lambda text: False))
    monkeypatch.setattr(output_policy, "normal_level", lambda: 0.08)     # the user talks at ~0.08 RMS
    return v


@pytest.mark.parametrize("text,rms", [("So we'll have next.", 0.0059), ("IS WHAT HAPPENED!", 0.016),
                                      ("Don't you ask for a hug?", 0.009)])
def test_quiet_background_voices_are_not_requests(voice, text, rms):
    ok, reason = voice._passes_activation_gate(text, 0.0, rms=rms)
    assert not ok and reason == "too_quiet_background", (text, reason)


def test_open_mic_always_needs_intent(voice, monkeypatch):
    # The old "Wake word off" switch (voice.open_mic_requires_intent) is gone:
    # an old config value of False can't open the floodgates any more.
    monkeypatch.setitem(config._data.setdefault("voice", {}), "open_mic_requires_intent", False)
    ok, reason = voice._passes_activation_gate("I would do that if I didn't think you were a doctor.", 0.52,
                                               rms=0.05)
    assert not ok and reason.startswith("open_mic"), reason
    assert voice._passes_activation_gate("open notepad", 0.0, rms=0.07)[0]


def test_saying_saints_name_while_it_talks_is_addressed(voice):
    voice._music_playing = True
    voice._command_reason = "interruption"
    ok, reason = voice._passes_activation_gate("SHUT UP SAINT SHUT UP", 0.58, follow_up=True, rms=0.09)
    assert ok and reason == "named"


@pytest.mark.parametrize("text", ["Yup.", "No.", "Wait.", "Shut up.", "Okay, thanks."])
def test_one_word_answers_when_talking_over_saint(voice, text):
    voice._command_reason = "interruption"
    ok, reason = voice._passes_activation_gate(text, 0.0, follow_up=True, rms=0.07)
    assert ok, (text, reason)


def test_one_word_answer_after_saint_spoke(voice):
    voice._command_reason = "follow-up"
    assert voice._passes_activation_gate("Yup.", 0.0, follow_up=True, rms=0.07)[0]
    assert not voice._passes_activation_gate("Yup.", 0.0, follow_up=True, rms=0.0)[0]   # noise made into "Yup"


# ---------------------------------------------------------------- "turn it back up"
def test_turn_it_back_up_moves_what_was_just_changed(monkeypatch):
    from modules.agent.context import desktop_context
    desktop_context.clear()
    assert route("turn it back up").name == "desktop.volume"
    desktop_context.note_volume("spotify", 10)
    from modules.agent.router import spotify_intent
    assert route("turn it back up").name == "spotify.volume_up"
    assert spotify_intent("turn it back up").kwargs["step"] == 10
    assert route("turn it down a little").name == "spotify.volume_down"
    desktop_context.note_volume("system", 5)
    assert route("turn it back up").name == "desktop.volume"
    desktop_context.clear()


# ---------------------------------------------------------------- "that's not a chill song"
@pytest.mark.parametrize("text", ["thats not a chill song", "That's not, that's not chill.", "this isn't chill",
                                  "that's not the vibe"])
def test_not_the_mood_is_music_feedback(text):
    from modules.agent.context import desktop_context
    desktop_context.note_domain("spotify")
    it = route(text)
    assert it is not None and it.name == "spotify.not_mood", text
    desktop_context.clear()


def test_not_the_mood_after_the_queue_is_about_the_next_song(monkeypatch):
    from modules.learning import feedback as fb
    monkeypatch.setitem(config._data.setdefault("learning", {}), "from_mistakes", True)
    learner = fb.FeedbackLearner()
    learner.note_result("what's in my queue", "spotify.queue_list", True, "Up next: 2K FREESTYLE by Lil Darkie.")
    monkeypatch.setattr(fb, "feedback", learner)
    from modules.agent.context import desktop_context
    from modules.agent.router import spotify_intent
    desktop_context.note_domain("spotify")
    assert spotify_intent("thats not a chill song").kwargs == {"mood": "chill", "which": "next"}
    desktop_context.clear()


@pytest.fixture
def sp(tmp_path, monkeypatch):
    from modules.spotify.memory import SpotifyMemory
    from modules.spotify.tools import SpotifyTools
    from tests.test_session_0930 import Client0930
    t = SpotifyTools(Client0930())
    t._memory = SpotifyMemory(str(tmp_path / "sp.db"))
    monkeypatch.setattr(t, "_refresh_soon", lambda *a, **k: None)
    return t


def test_not_the_mood_skips_it_when_it_comes_up(sp):
    r = sp.not_mood("chill", "next")
    assert r["track"] == "Song 0" and "q0" in sp._skip_when_up
    assert "q0" in sp.memory.mood_mismatches("chill")
    assert sp.memory.mood_mismatches("energetic") == set()


def test_not_the_mood_now_skips_the_song_playing(sp, monkeypatch):
    skipped = []
    monkeypatch.setattr(sp, "next", lambda source="voice": skipped.append(source) or {"success": True})
    monkeypatch.setattr(sp, "_state", lambda: {"item": {"id": "c", "name": "Now", "artists": [{"name": "X"}]}})
    sp.not_mood("chill", "current")
    assert skipped == ["not_mood"] and "c" in sp.memory.mood_mismatches("chill")


# ---------------------------------------------------------------- corrections / being told
def test_his_name_is_corrects_the_whole_name():
    from modules.learning.corrections import CorrectionTracker
    c = CorrectionTracker()
    c.note("Click on John Carlos Mignetti.", "desktop.click", False)
    assert c.detect("his name is Giancarlos Minyetti").lower() == "click on giancarlos minyetti"
    c.note("open the FMA try", "learning.unknown", False)
    assert c.detect("it's called fmhy").lower() == "open fmhy"


@pytest.fixture
def learner(tmp_path, monkeypatch):
    from modules.learning import feedback as fb
    from modules.learning.skills import SkillStore
    monkeypatch.setitem(config._data.setdefault("learning", {}), "from_mistakes", True)
    store = SkillStore(str(tmp_path / "skills.json"))
    monkeypatch.setattr("modules.learning.skills.skills", store)
    monkeypatch.setattr(fb, "journal", fb.Journal(str(tmp_path / "journal.jsonl")))
    return fb.FeedbackLearner(), store


def test_told_what_to_do_after_i_dont_know_how(learner):
    fl, store = learner
    fl.expect_teaching("pump up the jam", "learning.unknown")
    fl.note_result("turn the volume up", "desktop.volume", True, "Volume up.")
    sk = store.match("pump up the jam")
    assert sk is not None and sk.steps == ["turn the volume up"] and sk.how == "taught"
    assert fl.just_taught[0] == "pump up the jam"


def test_cant_watch_during_a_game_asks_to_be_told(monkeypatch):
    from core.game_mode import game_mode
    from modules.learning import demonstration
    monkeypatch.setattr(game_mode, "active", True)
    monkeypatch.setitem(config._data.setdefault("learning", {}), "watch_and_learn", True)
    assert not demonstration.can_watch() and not demonstration.offer("turn it back up")


def test_routine_in_one_sentence():
    from modules.learning.intents import parse
    assert parse("When I say gaming time, open Steam and Discord") == ("make", "gaming time\nopen steam and discord")


# ---------------------------------------------------------------- routines SAINT notices
def _history(path, pairs_by_day):
    lines = []
    for day, pairs in enumerate(pairs_by_day):
        t0 = time.time() - (day + 1) * 86400
        for i, (a, b) in enumerate(pairs):
            t = t0 + i * 3600
            lines.append({"ts": t, "source": "voice", "user": a, "tools": [{"tool": "desktop.open_app", "ok": True}]})
            if b:
                lines.append({"ts": t + 40, "source": "voice", "user": b,
                              "tools": [{"tool": "audio.system_volume", "ok": True}]})
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")


@pytest.fixture
def miner(tmp_path, monkeypatch):
    from modules.learning import habits as H
    from modules.learning.skills import SkillStore
    store = SkillStore(str(tmp_path / "skills.json"))
    monkeypatch.setattr(H, "skills", store)
    monkeypatch.setattr("modules.learning.skills.skills", store)
    monkeypatch.setitem(config._data.setdefault("learning", {}), "notice_habits", True)
    return H.HabitMiner(str(tmp_path / "suggestions.json"), str(tmp_path / "history.jsonl")), store, tmp_path


def test_a_habit_becomes_a_suggestion_and_then_a_routine(miner):
    m, store, tmp = miner
    _history(tmp / "history.jsonl", [[("Open Roblox.", "Turn it down.")], [("open roblox", "turn it down")],
                                     [("Open Roblox", "turn it down")]])
    new = m.scan()
    assert [(s["when"], s["then"]) for s in new] == [("open roblox", "turn it down")]
    assert m.scan() == []                                   # suggested once
    assert store.match("open roblox") is None                # nothing changes until accepted
    msg = m.accept(new[0]["id"])
    sk = store.match("open roblox")
    assert sk is not None and sk.steps == ["open roblox", "turn it down"] and sk.how == "noticed", msg
    assert m.pending() == []


def test_one_day_or_a_rare_pair_is_not_a_habit(miner):
    m, _store, tmp = miner
    _history(tmp / "history.jsonl", [[("open roblox", "turn it down")] * 3])     # all on one day
    assert m.find() == []
    _history(tmp / "history.jsonl", [[("open roblox", "turn it down")], [("open roblox", None)],
                                     [("open roblox", None)], [("open roblox", "turn it down")],
                                     [("open roblox", None)]])
    assert m.find() == []                                   # 2 of 5 times


def test_dismissed_suggestions_stay_dismissed(miner):
    m, _store, tmp = miner
    _history(tmp / "history.jsonl", [[("open roblox", "turn it down")]] * 3)
    s = m.scan()[0]
    m.dismiss(s["id"])
    assert m.scan() == [] and m.pending() == []


# ---------------------------------------------------------------- mini player in Game Mode
def test_asking_for_the_mini_player_shows_it_in_a_game(monkeypatch):
    from core.game_mode import game_mode
    from ui.main_window import MainWindow
    monkeypatch.setattr(type(game_mode), "overlays_blocked", property(lambda s: True))
    monkeypatch.setitem(config._data.setdefault("widgets", {}), "spotify", False)
    shown = []

    class W:
        def isVisible(self):
            return bool(shown)

        def appear(self):
            shown.append(True)

        def hide(self):
            shown.clear()

    class Fake:
        _widget_in_game = False
        widget = W()
        music = type("M", (), {"apply_theme": lambda self: None})()

        def _tray_sync(self):
            pass

    f = Fake()
    MainWindow.set_widget(f, True, explicit=False)          # restored at startup: Game Mode wins
    assert not shown
    MainWindow.set_widget(f, True)                          # "turn on the mini player"
    assert shown and f._widget_in_game


# ---------------------------------------------------------------- SAINT.exe
def test_version_info_names_the_app_saint():
    from core.app_exe import version_info
    blob = version_info()
    assert struct.unpack_from("<H", blob)[0] == len(blob)
    assert "FileDescription\0SAINT".encode("utf-16-le") in blob.replace(b"\0\0\0\0", b"\0\0")
    assert struct.pack("<I", 0xFEEF04BD) in blob


def test_icon_resources_from_an_ico():
    from core.app_exe import icon_resources
    img = b"\x89PNG fake"
    ico = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 32, 32, 0, 0, 1, 32, len(img), 22) + img
    group, images = icon_resources(ico)
    assert images == [img] and len(group) == 6 + 14 and struct.unpack_from("<H", group, 6 + 12)[0] == 1

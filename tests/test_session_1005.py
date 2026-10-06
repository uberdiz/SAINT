"""
tests/test_session_1005.py

2026-10-05 session:
  * a scene with questions in it ("write me an email") asks everything first and
    never clicks Send without a yes (modules/automation/scene_plan.py)
  * misheard app names ("clad" -> Claude) are resolved by sound (modules/desktop/vocabulary.py)
  * SAINT's own voice volume (Settings slider, "talk louder")
  * TTS falls back to the Windows voice instead of going silent
  * a launch that Windows cancels is not blamed on the user
"""

import os
import threading

import pytest

from core.config import config

EMAIL_SCENE = [
    "open browser",
    "make a new tab",
    "go to \"https://mail.google.com/mail/u/0/#inbox\"",
    "ask which email to send from, personal or school",
    "hover over the profile in the top right, if @gmail.com = personal else school",
    "if need change click on the profile and click the other email (me.personal@gmail.com or me@school.example.edu)",
    "Click Compose",
    "ask for email if not saved",
    "ask what to write about",
    "make title based on desc and create contents of the email and ask to send or edit",
    "Click send if yes.",
]


# ---------------------------------------------------------------------- #
# Scene -> plan
# ---------------------------------------------------------------------- #
def test_email_scene_asks_everything_first_and_guards_send():
    from modules.automation.scene_plan import plan
    p = plan("write me an email", EMAIL_SCENE)
    assert p.interactive
    assert p.lines == [
        "ask: Which email should I send from, personal or school? -> account "
        "(personal = me.personal@gmail.com; school = me@school.example.edu)",
        "ask: Who's it to? -> recipient",
        "ask once: What's {recipient}'s email address? -> email",
        "ask: What should I write about? -> topic",
        "open browser",
        "make a new tab",
        'go to "https://mail.google.com/mail/?authuser={account}"',
        "Click Compose",
        "type {email}",
        "write: a short subject line for the email about {topic} -> subject",
        "type {subject} into the subject",
        "write: the email's text about {topic} -> message",
        "type {message} into the message body",
        "confirm: Should I send it?",
        "Click send",
    ]
    assert any("authuser" not in n and "mail.google.com" in n for n in p.notes)


def test_plain_scenes_are_left_alone():
    from modules.automation.scene_plan import plan
    for steps in (["play GM playlist on spotify", "set volume to 70", "minimize all tabs besides claude"],
                  ['Open Bloxstrap at "C:\\Users\\me\\Downloads\\Bloxstrap.exe"', "wait a sec", 'Click "No"'],
                  ["you can talk again", "restore before gaming workspace"],
                  ['go to "https://example.com/?a=b&c=d"']):
        p = plan("x", steps)
        assert not p.interactive, steps
        assert p.lines == steps


def test_unknowable_condition_is_handed_to_the_user():
    from modules.automation.scene_plan import plan
    p = plan("tidy up", ["open discord", "if there are unread messages, mark them read"])
    assert p.lines == ["open discord", "you: if there are unread messages, mark them read"]
    assert p.notes and "step 2" in p.notes[0]


def test_account_mapping_by_kind_of_address():
    from modules.automation.scene_plan import _mapping
    assert _mapping(["personal", "school"], "a@uni.edu or b@gmail.com") == {"personal": "b@gmail.com",
                                                                           "school": "a@uni.edu"}
    assert _mapping(["red", "blue"], "a@uni.edu or b@gmail.com") == {}        # no way to tell: ask the user


# ---------------------------------------------------------------------- #
# Running it through the agent
# ---------------------------------------------------------------------- #
@pytest.fixture
def email_scene(monkeypatch, tmp_path):
    from modules.agent.confirm import choices, confirmations
    from modules.automation import scenes as scenes_mod
    from modules.learning import lesson as L

    store = scenes_mod.SceneStore(str(tmp_path / "scenes.json"))
    store._sync_schedule = lambda s: None
    store.save(scenes_mod.Scene("write me an email", list(EMAIL_SCENE)))
    monkeypatch.setattr(scenes_mod, "scenes", store)
    monkeypatch.setattr(L, "answers", L.Answers(str(tmp_path / "answers.json")))

    done = []
    monkeypatch.setattr(L.LessonManager, "_do_step", lambda self, cmd: (done.append(cmd), L.Reply(f"Did {cmd}."))[1])
    monkeypatch.setattr(L.LessonManager, "_type",
                        staticmethod(lambda value, where="": (done.append(f"TYPE {value} @{where}"),
                                                              L.Reply("Typed it."))[1]))
    monkeypatch.setattr(L.LessonManager, "_draft",
                        staticmethod(lambda step, vals, change="": f"DRAFT {step.var} about {vals.get('topic')}"))
    L.lessons.stop()
    confirmations.clear()
    choices.clear()
    yield done
    L.lessons.stop()


def _say(text):
    from modules.agent.agent import agent
    return agent.handle(text)


def test_email_scene_asks_before_anything_happens(email_scene):
    done = email_scene
    r = _say("write me an email")
    assert r.expects_reply and "Which email should I send from, personal or school?" in r.text
    assert "Before I start" in r.text
    assert done == []                                         # nothing clicked before the answers
    assert "Who's it to" in _say("personal").text
    assert "Mr Norton's email address" in _say("Mr Norton").text
    assert "What should I write about" in _say("norton@example.com").text
    r = _say("the meeting tomorrow")
    assert done[:5] == ["open browser", "make a new tab",
                        'go to "https://mail.google.com/mail/?authuser=me.personal@gmail.com"',
                        "Click Compose", "type norton@example.com"]
    assert "Here's the subject" in r.text and r.expects_reply
    r = _say("yes")
    assert "Here's the message" in r.text
    r = _say("yes")
    assert r.text.endswith("Should I send it?")
    r = _say("no")
    assert "stopped before" in r.text
    assert "Click send" not in done


def test_email_scene_uses_what_the_request_already_said(email_scene):
    done = email_scene
    from modules.learning import lesson as L
    L.answers.put("What's Mr Norton's email address?", "norton@example.com")      # asked once before
    r = _say("write me an email to Mr Norton about the meeting")
    assert "Which email should I send from" in r.text
    r = _say("school")
    assert 'go to "https://mail.google.com/mail/?authuser=me@school.example.edu"' in done
    assert "type norton@example.com" in done
    assert "Here's the subject" in r.text and "the meeting" in r.text
    _say("yes")
    r = _say("yes")
    assert r.text.endswith("Should I send it?")
    _say("yes")
    assert done[-1] == "Click send"


# ---------------------------------------------------------------------- #
# Misheard names
# ---------------------------------------------------------------------- #
APPS = ["claude", "icloud", "rocket league", "bloxstrap", "discord", "opera browser", "clock", "notepad"]


def test_phonetic_keys():
    from modules.desktop.vocabulary import phonetic
    assert phonetic("Claude") == phonetic("clad") == phonetic("clawed") == "KLT"
    assert phonetic("Rocket League") == phonetic("rocket leak")
    assert phonetic("Bloxstrap") == phonetic("block strap")


def test_vocabulary_resolves_clear_cases_and_asks_on_ties(tmp_path):
    from modules.desktop.vocabulary import Vocabulary
    v = Vocabulary(str(tmp_path / "v.json"))
    assert v.resolve("clad", APPS) == "claude"
    assert v.resolve("clawed", APPS) == "claude"
    assert v.resolve("rocket leak", APPS) == "rocket league"
    assert v.resolve("opra", APPS) == "opera browser"
    assert v.resolve("cloud", APPS) is None and v.ambiguous("cloud", APPS)     # Claude or iCloud: ask
    assert [m.name for m in v.rank("cloud", APPS)][:2] == ["claude", "icloud"]
    v.learn("cloud", "claude")                                                 # the user picked Claude
    assert v.resolve("cloud", APPS) == "claude" and not v.ambiguous("cloud", APPS)
    assert Vocabulary(str(tmp_path / "v.json")).resolve("cloud", APPS) == "claude"   # kept on disk
    assert v.resolve("spreadsheet", APPS) is None and v.rank("spreadsheet", APPS) == []


def test_app_catalog_uses_the_vocabulary(monkeypatch, tmp_path):
    from modules.desktop import apps, vocabulary as voc
    monkeypatch.setattr(voc, "vocabulary", voc.Vocabulary(str(tmp_path / "v.json")))
    cat = apps.AppCatalog()
    cat._entries = {k: apps.AppEntry(k.title(), "path", f"C:/x/{k}.lnk", "start_menu") for k in APPS}
    assert cat.resolve("clad").name == "Claude"
    assert cat.resolve("a rocket leak").name == "Rocket League"
    assert cat.resolve("cloud") is None
    assert cat.suggestions("cloud")[:2] == ["Claude", "Icloud"]
    assert cat.resolve("the clock").name == "Clock"                       # a real app still wins


@pytest.mark.skipif(os.name != "nt", reason="Windows shortcuts")
def test_broken_shortcut_is_detected(tmp_path):
    import sys
    pytest.importorskip("win32com.client")
    import pythoncom
    import win32com.client
    from modules.desktop.apps import broken_shortcut
    pythoncom.CoInitialize()
    shell = win32com.client.Dispatch("WScript.Shell")
    for name, target in (("gone", str(tmp_path / "Gone" / "Gone.exe")), ("ok", sys.executable)):
        lnk = shell.CreateShortcut(str(tmp_path / f"{name}.lnk"))
        lnk.TargetPath = target
        lnk.Save()
    assert broken_shortcut(str(tmp_path / "gone.lnk")) is True
    assert broken_shortcut(str(tmp_path / "ok.lnk")) is False


# ---------------------------------------------------------------------- #
# Voice volume
# ---------------------------------------------------------------------- #
def test_voice_volume_commands(monkeypatch):
    from modules.agent.meta import match_meta, run_meta
    from modules.voice.output_policy import output_policy, voice_volume
    monkeypatch.setitem(config._data.setdefault("voice", {}), "volume", 100)
    assert match_meta("turn it up") is None                       # that's Spotify / Windows
    assert run_meta(match_meta("talk louder")) == "My voice is at 120% now."
    assert run_meta(match_meta("your voice is too loud")) == "My voice is at 100% now."
    assert run_meta(match_meta("Hey SAINT, voice volume 40.")) == "My voice is at 40% now."
    assert voice_volume() == pytest.approx(0.4)
    assert output_policy.gain() == pytest.approx(0.4)
    assert "as loud as it goes" in run_meta(match_meta("set your voice to 300%")) or voice_volume() == 1.5


# ---------------------------------------------------------------------- #
# TTS never goes silent
# ---------------------------------------------------------------------- #
class _Engine:
    def __init__(self, fail=False, **kw):
        self._fail = fail
        self._interrupt_event = threading.Event()
        self.closed = False

    def _load(self):
        if self._fail:
            raise RuntimeError("No module named 'kokoro'")

    def close(self):
        self.closed = True

    device_info = {"fell_back": True, "resolved_device": "Windows voice"}

    def warm_up(self):
        pass

    def speak(self, text, turn_id=0, on_chunk_start=None):
        return True

    def interrupt(self, turn_id=None):
        pass

    def is_speaking(self):
        return False


def test_tts_falls_back_to_the_windows_voice(monkeypatch):
    import modules.voice.tts as tts_mod
    from modules.voice import kokoro_onnx, sapi
    from modules.voice.tts_service import TTSState, get_tts_service, reset_tts_service
    reset_tts_service()
    failed = []
    monkeypatch.setattr(tts_mod, "_torch_available", lambda: True)
    monkeypatch.setattr(tts_mod, "KokoroTTS", lambda **kw: failed.append(_Engine(fail=True)) or failed[-1])
    monkeypatch.setattr(kokoro_onnx, "available", lambda: False)
    monkeypatch.setattr(sapi, "available", lambda: True)
    monkeypatch.setattr(sapi, "SapiTTS", lambda **kw: _Engine())
    monkeypatch.setitem(config._data.setdefault("voice", {}), "auto_download_models", False)
    svc = get_tts_service()
    try:
        assert svc.initialize(backend="kokoro", blocking=True) is True
        assert svc.state == TTSState.FALLBACK and svc.get_diagnostics()["engine_type"] == "windows"
        assert failed and failed[0].closed                       # the broken engine let go of the speaker
        assert svc.synthesize("hello", turn_id=1) is True
    finally:
        reset_tts_service()


# ---------------------------------------------------------------------- #
# Spotify: "skip 3 songs", and a new queue clears SAINT's old one
# ---------------------------------------------------------------------- #
def test_skip_n_is_routed():
    from modules.agent.router import spotify_intent
    assert spotify_intent("skip 3 songs").kwargs == {"count": 3}
    assert spotify_intent("Skip the next two.").kwargs == {"count": 2}
    assert spotify_intent("skip one song").tool == "spotify.next"
    assert spotify_intent("skip ahead 30 seconds").tool == "spotify.seek"


@pytest.fixture
def sp(tmp_path, monkeypatch):
    from tests.test_spotify_0929 import FakeClient
    from modules.spotify.memory import SpotifyMemory
    from modules.spotify.tools import SpotifyTools

    class QueueClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.queue_ids = []

        def get_queue(self):
            return {"queue": [{"id": t} for t in self.queue_ids]}

        def volume(self, percent, device_id=None):
            self.calls.append(("volume", percent))

        def next(self, device_id=None):
            super().next(device_id)
            if self.queue_ids:
                self.queue_ids.pop(0)

    t = SpotifyTools(QueueClient())
    t._memory = SpotifyMemory(str(tmp_path / "sp.db"))
    monkeypatch.setattr(t, "_refresh_soon", lambda *a, **k: None)
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)
    return t


import time as time_mod  # noqa: E402


def test_skip_n_skips_that_many(sp):
    r = sp.skip(3)
    assert r["count"] == 3 and sp.client.calls.count(("next",)) == 3


def test_new_queue_clears_saints_old_one_quietly(sp):
    sp._radio_orphans.clear()
    sp._radio = {"gen": 1, "queued": ["old1", "old2", "old3"], "last_index": 0, "seed": None, "mood": "",
                 "context": "", "novel": False, "similar": True, "played": None, "pool": [], "seen": set(),
                 "known": set(), "started": 0, "filling": False, "basis": "", "drift": None}
    sp.client.queue_ids = ["old2", "old3", "users-own-pick", "ctx1"]    # old1 already played
    sp.play_query("Stand By Me")
    calls = sp.client.calls
    first_play = calls.index(next(c for c in calls if c[0] == "play"))
    before = calls[:first_play]
    assert before.count(("next",)) == 2                    # only SAINT's two leftovers, not the user's pick
    assert ("volume", 0) in before                         # skipped past with the sound down
    assert ("volume", 50) in calls[first_play:]            # and the volume is back once the new song plays
    assert sp.client.queue_ids[0] == "users-own-pick"
    assert "old2" not in sp._radio_orphans and "old3" not in sp._radio_orphans


# ---------------------------------------------------------------------- #
# Mini player: shows what's really playing
# ---------------------------------------------------------------------- #
def test_now_playing_prefers_the_live_media_session_when_the_poll_is_behind():
    from ui.reactive import UIBus
    bus = UIBus()
    bus.spotify = {"track": "Old Song", "artists": "A", "is_playing": True, "duration_ms": 1000}
    bus.media = {"title": "Old Song", "artist": "A", "is_playing": True, "is_spotify": True, "app": "Spotify"}
    assert bus.now_playing()["source"] == "spotify"
    bus.media = dict(bus.media, title="New Song", artist="B")
    np = bus.now_playing()
    assert np["title"] == "New Song" and np["source"] == "spotify_media"


# ---------------------------------------------------------------------- #
# Test profiles: SAINT without your real data
# ---------------------------------------------------------------------- #
def test_profiles_keep_test_data_apart(monkeypatch, tmp_path):
    import json
    from core import paths, profiles
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("SAINT_DATA_DIR", raising=False)
    monkeypatch.delenv("SAINT_PROFILE", raising=False)
    monkeypatch.setattr(paths, "installed_exe", lambda: tmp_path / "SAINT.exe")
    real = paths.base_data_dir()
    assert real == tmp_path / "SAINT" and paths.data_dir() == real and paths.keyring_service() == "SAINT"
    (real / "scenes.json").write_text('[{"name": "mine", "steps": ["x"]}]', encoding="utf-8")
    (real / "tts" / "kokoro-onnx").mkdir(parents=True)
    (real / "models").mkdir()

    monkeypatch.setenv("SAINT_PROFILE", "demo")
    demo = profiles.ensure("demo")
    assert paths.data_dir() == demo == tmp_path / "SAINT-profiles" / "demo"
    assert paths.keyring_service() == "SAINT-profile-demo"            # its own Spotify login / Link key
    names = [s["name"] for s in json.loads((demo / "scenes.json").read_text(encoding="utf-8"))]
    assert "write me an email" in names and "mine" not in names
    assert paths.resolve_project_path("data/tts/kokoro-onnx") == real / "tts" / "kokoro-onnx"   # models shared

    from modules.automation.scene_plan import plan
    email = next(s for s in json.loads((demo / "scenes.json").read_text(encoding="utf-8"))
                 if s["name"] == "write me an email")
    assert "(personal = alex.demo@gmail.com; work = alex@example.com)" in plan(email["name"], email["steps"]).lines[0]

    snap = profiles.create("snap", "real")
    assert (snap / "scenes.json").exists() and not (snap / "models").exists() and not (snap / "tts").exists()
    assert "scenes.json" in profiles.user_data_in(snap)
    assert {p["name"] for p in profiles.list_profiles()} == {"demo", "snap"}
    profiles.delete("snap")
    assert (real / "scenes.json").read_text(encoding="utf-8").startswith('[{"name": "mine"')    # untouched


def test_quoted_url_step_opens_the_site():
    """'go to "https://..."' (how scenes write it) opened nothing: it looked for a window with that name."""
    from modules.agent.router import route
    assert route('go to "https://mail.google.com/mail/u/0/#inbox"').name == "browser.open_url"
    assert route('go to "https://mail.google.com/mail/?authuser=me@example.com"').name == "browser.open_url"

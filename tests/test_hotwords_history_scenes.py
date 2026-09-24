"""Music hot-words (no wake word while Spotify plays), local history log, scenes."""

import json

from core.config import config
from core.events import Event, EventType
from modules.voice.module import VoiceModule, ListenPhase
from tests.test_wake_and_voice import _finals, _run, _silence, _speech, _voice, _wait


# ---------------------------------------------------------------- hot-words
def _music(v, playing=True, track="Song"):
    v._on_bus_event(Event(EventType.SPOTIFY_PLAYBACK_CHANGED, {"is_playing": playing, "track": track}))


def test_hotword_matcher_accepts_playback_commands_only_while_music_plays():
    v = VoiceModule.__new__(VoiceModule)
    _music(v, playing=True)
    for said in ("Skip.", "skip this song", "Skip this track!", "next song", "go back", "pause the music",
                 "Resume.", "louder", "turn it down", "volume up", "I love this song"):
        assert v._is_music_hotword(said, 0.9), said
    for said in ("skip to the good part of the movie", "I want to pause for a sec", "next time maybe",
                 "One time for you, yo, say hi to you", "play", "up"):
        assert not v._is_music_hotword(said, 0.9), said
    # Whisper scores single words near zero (live: "Skip." at 0.02): the exact
    # whole-utterance match decides, not the confidence.
    assert v._is_music_hotword("Skip.", 0.02) and v._is_music_hotword("pause", 0.0)
    assert not v._is_music_hotword("turn the music down", 0.05)   # longer phrases still need confidence
    _music(v, playing=False)
    config.set("widgets.spotify", False, persist=False)
    assert not v._is_music_hotword("resume", 0.9)          # paused, widget hidden → inactive
    config.set("widgets.spotify", True, persist=False)
    try:
        assert v._is_music_hotword("resume", 0.9)          # paused, widget on screen → active
    finally:
        config.set("widgets.spotify", False, persist=False)


def test_hotword_becomes_a_command_without_wake_word():
    finals, done = _finals()
    try:
        config.set("voice.wake_word_chime", False, persist=False)
        v = _voice("Skip.")
        _music(v, playing=True)
        _run(v, _speech(20) + _silence(40))
        _wait(lambda: bool(finals))
        assert finals and finals[-1]["text"] == "Skip." and finals[-1]["source"] == "hotword"
        assert finals[-1]["wake"] is True and v.phase == ListenPhase.WAKE
    finally:
        done()


def test_hotword_ignored_when_music_is_off():
    finals, done = _finals()
    try:
        config.set("voice.wake_word_chime", False, persist=False)
        v = _voice("Skip.")
        _music(v, playing=False, track=None)
        _run(v, _speech(20) + _silence(40))
        _wait(lambda: v._stt.calls >= 1, 1.0)
        assert not finals
    finally:
        done()


# ---------------------------------------------------------------- history
def test_history_records_turn_with_tools_and_source(tmp_path):
    from core.history import History
    h = History(tmp_path / "h.jsonl")
    emit = lambda t, p: h._on_event(Event(t, p))
    emit(EventType.VOICE_STT_FINAL, {"text": "skip", "session_id": 7, "source": "hotword"})
    emit(EventType.CONVERSATION_TURN_START, {"text": "skip", "turn_id": 1, "session_id": 7})
    emit(EventType.TOOL_COMPLETED, {"tool": "spotify.next", "duration_ms": 120})
    emit(EventType.TOOL_FAILED, {"tool": "spotify.volume", "duration_ms": 30})
    emit(EventType.CONVERSATION_TURN_END, {"turn_id": 1, "response": "Skipped it."})
    emit(EventType.AUTOMATION_TRIGGERED, {"title": "Focus mode", "kind": "scene"})
    rows = h.read()
    assert [r["source"] for r in rows] == ["hotword", "scene"]
    assert rows[0]["user"] == "skip" and rows[0]["reply"] == "Skipped it."
    assert rows[0]["tools"] == [{"tool": "spotify.next", "ok": True, "ms": 120},
                                {"tool": "spotify.volume", "ok": False, "ms": 30}]
    h.clear()
    assert h.read() == []


def test_history_trims_to_max_entries(tmp_path):
    from core.history import History
    h = History(tmp_path / "h.jsonl")
    config.set("history.max_entries", 10, persist=False)
    try:
        for i in range(520):
            h.append({"source": "typed", "user": str(i)})
        rows = h.read()
        assert len(rows) <= 510 and rows[-1]["user"] == "519"
    finally:
        config.set("history.max_entries", 5000, persist=False)


def test_history_respects_disabled(tmp_path):
    from core.history import History
    h = History(tmp_path / "h.jsonl")
    config.set("history.enabled", False, persist=False)
    try:
        h._on_event(Event(EventType.AUTOMATION_TRIGGERED, {"title": "x", "kind": "scene"}))
        assert h.read() == []
    finally:
        config.set("history.enabled", True, persist=False)


# ---------------------------------------------------------------- scenes
def test_scene_match_run_and_schedule(tmp_path):
    from modules.automation.scenes import Scene, SceneStore
    from modules.automation.scheduler import scheduler
    store = SceneStore(tmp_path / "scenes.json")
    s = store.save(Scene("Focus mode", ["play lofi", "set volume to 30"], phrase="deep work",
                         schedule="every weekday at 8"))
    assert s.automation_id and scheduler.store.get(s.automation_id).message == "run Focus mode"
    for said in ("focus mode", "Run focus mode.", "start the focus mode scene", "deep work"):
        assert store.match(said).id == s.id, said
    assert store.match("play my focus mode playlist") is None
    assert store.run(s, lambda step: f"ok {step}") == ["ok play lofi", "ok set volume to 30"]
    assert json.loads((tmp_path / "scenes.json").read_text())[0]["last_run"] > 0
    store.delete(s.id)
    assert store.all() == [] and scheduler.store.get(s.automation_id) is None


def test_scene_validation(tmp_path):
    import pytest
    from modules.automation.scenes import Scene, SceneStore
    store = SceneStore(tmp_path / "scenes.json")
    with pytest.raises(ValueError):
        store.save(Scene("Empty", []))
    store.save(Scene("Wind down", ["pause"]))
    with pytest.raises(ValueError):
        store.save(Scene("wind down", ["pause"]))


def test_agent_runs_scene_by_voice(tmp_path, monkeypatch):
    from modules.agent.agent import agent
    from modules.automation import scenes as scenes_mod
    store = scenes_mod.SceneStore(tmp_path / "scenes.json")
    store.save(scenes_mod.Scene("Movie night", ["pause"], phrase="movie time"))
    ran = []
    monkeypatch.setattr(scenes_mod, "scenes", store)
    monkeypatch.setattr(store, "run_in_background", lambda scene, run_command: ran.append(scene.name))
    res = agent.handle("movie time")
    assert res.intent == "scene.run" and ran == ["Movie night"]

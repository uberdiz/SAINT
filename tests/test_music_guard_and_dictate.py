"""Music guard for follow-ups + dictation-to-prompt pipeline."""

import re

import pytest

from modules.agent.dictate import DictationManager
from modules.voice.module import VoiceModule


# ---------------------------------------------------------------- music guard
def _gate(vm, text, follow_up=True, conf=0.85):
    return vm._passes_activation_gate(text, conf, wake_initiated=False, follow_up=follow_up)


def test_music_guard_blocks_lyrics_but_allows_short_commands(monkeypatch):
    vm = VoiceModule.__new__(VoiceModule)
    vm._music_playing = True
    monkeypatch.setattr(vm, "_MUSIC_FOLLOWUP_OK", VoiceModule._MUSIC_FOLLOWUP_OK)
    # Real transcribed lyrics from the log
    for lyric in ("One time for you, yo, say hi to you, show love me",
                  "SUSPENSEFUL MUSIC PLAYING",
                  "Today is my day of office",
                  "A No cranes like the other we sned deep down"):
        ok, reason = _gate(vm, lyric)
        assert not ok, (lyric, ok, reason)
        assert reason == "music_playing_needs_wake_word", reason
    # Real short commands from the log still get through
    for cmd in ("Skip.", "skip it", "pause", "next", "louder", "volume up", "shuffle"):
        ok, reason = _gate(vm, cmd)
        assert ok, (cmd, reason)


def test_music_guard_off_when_no_music(monkeypatch):
    vm = VoiceModule.__new__(VoiceModule)
    vm._music_playing = False
    ok, _ = _gate(vm, "One time for you, yo, say hi to you, show love me")
    assert ok      # not addressed to SAINT during silence — normal gates only


# ---------------------------------------------------------------- dictation
def test_dictate_start_captures_topic():
    d = DictationManager()
    reply = d.maybe_start("help me write a prompt about a chess app")
    assert reply and "chess app" in reply and d.active


def test_dictate_ignores_wake_word_and_captures_content(monkeypatch):
    d = DictationManager()
    d.maybe_start("write a note about my morning routine")
    assert d.feed("It should include coffee, journaling, and a walk.") is None
    assert d.feed("Also mention hydration.") is None
    # 'done' should try to write the file — stub the LLM + file open
    monkeypatch.setattr("modules.agent.llm.complete", lambda *a, **k: "Morning routine: coffee, journaling, walk, hydration.")
    reply = d.feed("done")
    assert reply and "Saved" in reply
    assert not d.active


def test_dictate_cancel_throws_it_out():
    d = DictationManager()
    d.maybe_start("write a note about groceries")
    d.feed("milk eggs bread")
    reply = d.feed("cancel")
    assert not d.active and "thrown out" in reply.lower()


def test_dictate_start_recognises_multiple_kinds():
    for start in ("write me a message about the meeting",
                  "draft an email about the launch",
                  "take a note about the bug",
                  "let me dictate a prompt about game ideas",
                  "take this down: my thoughts on tea"):
        d = DictationManager()
        assert d.maybe_start(start) is not None, start
        assert d.active


# ---------------------------------------------------------------- follow-ups SAINT acts on
def test_music_guard_lets_real_commands_through_but_not_chatter():
    """While music plays, follow-ups SAINT recognises as commands (or short
    questions to it) pass; chatter and lyrics from the live log don't."""
    vm = VoiceModule.__new__(VoiceModule)
    vm._music_playing = True
    for cmd in ("Skip that.", "Can you skip that?", "Click it.", "Click the first link.",
                "Double click the recycling bin.", "Yes, open my browser.",
                "Open a new tab in my browser and search YouTube.", "What's that reminder for?"):
        ok, reason = _gate(vm, cmd, conf=0.45)
        assert ok, (cmd, reason)
    for chatter in ("that's actually amazing oh my gosh", "I think it's good", "Grove literally just put me on.",
                    "I'm just not though", "That was completely wrong. What kind of one do you get there?"):
        ok, reason = _gate(vm, chatter, conf=0.6)
        assert not ok and reason == "music_playing_needs_wake_word", (chatter, reason)


def test_recognised_follow_up_command_tolerates_low_whisper_confidence():
    vm = VoiceModule.__new__(VoiceModule)
    vm._music_playing = True
    assert _gate(vm, "Click it.", conf=0.13)[0]              # Whisper scored this 0.13 live
    assert not _gate(vm, "I think it's good", conf=0.13)[0]


def test_lyrics_never_become_memories_or_typing():
    from modules.agent.agent import agent
    assert not agent.accepts_followup("my name is Slim Shady")
    assert not agent.accepts_followup("type your name in the stars")

"""Stop / silent mode / "what are you doing?", output policy, cancellation,
Whisper repetition filter and personal aliases."""

import pytest

from core.activity import Activity
from core.cancel import CancelScope, Cancelled
from modules.agent.meta import match_meta, run_meta
from modules.voice.output_policy import OutputPolicy
from modules.voice.stt_filters import clean_transcript, repetition_ratio


# ---------------------------------------------------------------- meta commands
@pytest.mark.parametrize("text,kind", [
    ("stop", "stop"), ("Stop.", "stop"), ("okay stop", "stop"), ("shut up", "stop"), ("be quiet", "stop"),
    ("Hey SAINT, cancel", "stop"), ("abort", "stop"),
    ("stop everything", "stop_all"), ("Cancel everything.", "stop_all"),
    ("what are you doing?", "status"), ("Hey SAINT, what are you doing", "status"),
    ("silent mode", "silent_on"), ("be quiet for 30 minutes", "silent_on"),
    ("don't talk for 10 minutes", "silent_on"), ("you can talk again", "silent_on_off"),
])
def test_meta_phrases(text, kind):
    m = match_meta(text)
    expected = "silent_off" if kind == "silent_on_off" else kind
    assert m is not None and m.kind == expected, (text, m)


@pytest.mark.parametrize("text", ["stop the music", "I'm done gaming", "cancel the shutdown",
                                  "stop sharing my screen", "what are you doing tomorrow at noon"])
def test_meta_leaves_real_commands_alone(text):
    assert match_meta(text) is None


def test_silent_minutes():
    assert match_meta("be quiet for 30 minutes").minutes == 30
    assert match_meta("silent mode for an hour").minutes == 60
    assert match_meta("silent mode").minutes == 60


def test_stop_words_only_as_whole_utterance():
    from core.conversation import ConversationController
    c = ConversationController.__new__(ConversationController)
    assert c._is_stop_command("stop")
    assert c._is_stop_command("okay, never mind")
    assert c._is_stop_command("be quiet")
    assert not c._is_stop_command("I'm done gaming")
    assert not c._is_stop_command("be quiet for 30 minutes")
    assert not c._is_stop_command("stop the music")


def test_stop_through_router_still_reaches_spotify():
    from modules.agent.router import route
    it = route("stop the music")
    assert it is not None and it.name.startswith("spotify"), it and it.name


# ---------------------------------------------------------------- output policy
def test_silent_mode_speaks_only_what_matters():
    p = OutputPolicy()
    assert p.should_speak("Skipped.")
    p.set_silent(30)
    assert p.silent and 29 <= p.silent_remaining_min() <= 30
    assert not p.should_speak("Skipped. Now playing Otis.")
    assert p.should_speak("Which window should I use?")
    assert p.should_speak("I couldn't find Steam.")
    assert p.should_speak("Time to leave.", source="reminder")
    p.clear_silent()
    assert p.should_speak("Skipped.")


def test_whisper_gain():
    p = OutputPolicy()
    assert p.gain() == 1.0
    p.note_input(0.005)
    assert p.gain() < 1.0
    p.note_input(0.2)
    assert p.gain() == 1.0


def test_run_meta_silent_and_status():
    from modules.voice.output_policy import output_policy
    assert "Silent mode for 30 minutes" in run_meta(match_meta("be quiet for 30 minutes"))
    assert output_policy.silent
    assert run_meta(match_meta("you can talk again")) == "I'm back."
    assert not output_policy.silent
    assert "listening" in run_meta(match_meta("what are you doing?"))


# ---------------------------------------------------------------- cancel + activity
def test_cancel_tokens_only_see_later_stops():
    scope = CancelScope()
    tok = scope.token()
    assert not tok.cancelled
    scope.trip()
    assert tok.cancelled
    with pytest.raises(Cancelled):
        tok.check()
    assert not scope.token().cancelled       # work started after the stop is unaffected


def test_run_plan_stops_between_steps():
    from core.cancel import cancel
    from modules.agent.router import Intent, Reply, run_plan
    ran = []

    def first():
        ran.append(1)
        cancel.trip()                        # user said "stop" during step 1
        return Reply("One.")

    def second():
        ran.append(2)
        return Reply("Two.")
    r = run_plan([Intent("test.one", first), Intent("test.two", second)])
    assert ran == [1] and not r.ok and "Stopped" in r.text


def test_activity_describes_steps_and_background():
    a = Activity()
    assert "listening" in a.describe()
    a.begin("a plan", ["opening your browser", "searching YouTube"])
    a.step(0)
    assert a.describe() == "I'm opening your browser, then searching YouTube."
    a.begin("inner", ["clicking"])            # nested plan inside a scene step
    a.step(0)
    assert a.describe().startswith("I'm clicking")
    a.end()
    assert "opening your browser" in a.describe()
    a.end()
    a.add_background("t1", "scanning D:")
    assert a.describe() == "I'm scanning D:."


def test_step_labels():
    from modules.agent.router import step_label
    assert step_label("desktop.open_app") == "opening app"
    assert step_label("browser.search") == "searching"


# ---------------------------------------------------------------- whisper repetition
def test_repetition_loop_is_dropped():
    loop = "No, " * 60 + "no."
    assert repetition_ratio(loop) > 0.9
    assert clean_transcript(loop) is None
    assert clean_transcript("la la la la la la la la la la") is None


def test_normal_sentences_survive():
    for text in ("Go to my Downloads folder, click the first download, and extract it using WinRAR to my games folder.",
                 "Open a new tab in my browser and search fmhy.net", "Skip.", "no no no"):
        assert clean_transcript(text) is not None
    assert clean_transcript("skip skip skip skip that song") == "skip that song"


# ---------------------------------------------------------------- aliases
def test_aliases_define_expand_forget(tmp_path, monkeypatch):
    from modules.agent import aliases as mod
    store = mod.AliasStore(str(tmp_path / "aliases.json"))
    monkeypatch.setattr(mod, "aliases", store)
    assert "means open D:\\Games" in mod.parse_alias_command("when I say the games folder, I mean D:\\Games") \
        or store.all()
    assert "the lab" in mod.parse_alias_command("When I say the lab, I mean open my SAINT project in VS Code")
    assert store.expand("the lab") == "open my SAINT project in VS Code"
    assert store.expand("open the lab") == "open my SAINT project in VS Code"
    store.set("my coding playlist", "Lo-fi Beats")
    assert store.expand("play my coding playlist") == "play Lo-fi Beats"
    assert store.expand("what time is it") == "what time is it"
    assert "the lab" in mod.parse_alias_command("what aliases do I have")
    assert mod.parse_alias_command("forget the alias the lab").startswith("Forgot")
    assert store.expand("the lab") == "the lab"


# ---------------------------------------------------------------- permissions
def test_always_confirm_beats_allow_override(monkeypatch):
    from core.config import config
    from core.permissions import permission_manager
    monkeypatch.setitem(config._data, "permissions", {"overrides": {"files": "allow", "files.recycle": "allow"}})
    assert permission_manager.policy_for_tool("files.recycle", "high") == "confirm"
    assert permission_manager.policy_for_tool("files.drive_overview", "low") == "allow"
    monkeypatch.setitem(config._data, "permissions", {"overrides": {"files.recycle": "deny"}})
    assert permission_manager.policy_for_tool("files.recycle", "high") == "deny"

"""Self-learning (modules/learning) and the failures from the 2026-09-25 log:
the planner trying harder, skills, "no, I meant ...", watching the user,
closing games, desktop clicks, Voicemeeter, "my X playlist", random speech."""

import time

import pytest

from core.config import config
from modules.agent.router import Intent, Reply, route
from modules.learning import corrections as corr_mod
from modules.learning import demonstration, intents as learning_intents, planner
from modules.learning.demonstration import Event, summarize
from modules.learning.skills import SkillStore, norm


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    from modules.desktop import youtube
    monkeypatch.setattr(youtube, "is_watching", lambda *a, **k: False)
    from modules.agent.context import desktop_context
    desktop_context.note_domain("")
    store = SkillStore(str(tmp_path / "skills.json"))
    import modules.learning.skills as sk
    for mod in (sk, planner, learning_intents):
        monkeypatch.setattr(mod, "skills", store)
    corr_mod.corrections.clear()
    yield store
    corr_mod.corrections.clear()


@pytest.fixture
def calls(monkeypatch):
    """Tool calls made while routing/running, without touching the desktop."""
    from modules.automation.tools import ToolResult, get_tool_registry
    made = []

    class Result(dict):
        def __missing__(self, key):
            return ""

    def fake(_tool_name, **kw):
        made.append((_tool_name, kw))
        return ToolResult(success=True, result=Result(clicked=kw.get("name", ""), changed=True,
                                                      muted=kw.get("state") == "on", device=kw.get("name", "")))
    monkeypatch.setattr(get_tool_registry(), "execute", fake)
    return made


# ---------------------------------------------------------------- the catalog is real
def test_every_catalog_command_is_understood():
    from modules.learning.catalog import examples
    missing = [c for c in examples() if not planner.understood(c)]
    assert not missing, missing


# ---------------------------------------------------------------- skills
def test_skill_store_learns_matches_and_forgets(_isolated):
    s = _isolated
    assert s.learn("Minimize all my windows", ["show the desktop"]).phrase == "minimize all my windows"
    assert s.match("Hey SAINT, could you minimize all my windows, please?").steps == ["show the desktop"]
    assert s.match("minimise all my windows").phrase == "minimize all my windows"      # STT variation
    assert s.match("open chrome") is None
    assert s.learn("yes", ["open chrome"]) is None                                       # too generic
    assert s.learn("open chrome", ["open chrome"]) is None                               # teaches nothing
    sk = s.match("minimize all my windows")
    for _ in range(3):
        s.note_result(sk, False)
    assert s.match("minimize all my windows") is None                                    # broken skills go


def test_norm_strips_politeness():
    assert norm("SAINT, can you please open disk clean up for me?") == "open disk clean up"


# ---------------------------------------------------------------- planner
def test_worth_planning_is_for_instructions_not_questions():
    assert planner.worth_planning("Minimize all my windows and double left click the recycling bin")
    assert planner.worth_planning("Oh, oh my gosh, open disk, clean up")
    assert not planner.worth_planning("what's the capital of France")
    assert not planner.worth_planning("They say, launched her area.")
    assert not planner.worth_planning("CHOP CHOP in my games folder.")


def test_planner_validates_replans_runs_and_learns(monkeypatch, _isolated):
    monkeypatch.setitem(config._data.setdefault("learning", {}), "planner", True)
    asked = []

    def fake_plan(text, failure="", rejected=None):
        asked.append(rejected)
        return ["wiggle the thingamajig"] if rejected is None else ["show the desktop", "double click the recycle bin"]
    monkeypatch.setattr(planner, "plan", fake_plan)
    ran = []
    import modules.agent.router as router
    monkeypatch.setattr(router, "run_plan", lambda intents: ran.append([i.name for i in intents]) or Reply("Done."))
    out = planner.attempt("tidy my screen and open the bin")
    assert asked == [None, ["wiggle the thingamajig"]]           # a made-up step is rejected, then re-planned
    assert ran == [["desktop.keys", "desktop.click_element"]]
    assert out.learned and _isolated.match("tidy my screen and open the bin").steps[0] == "show the desktop"


def test_planner_never_repeats_what_just_failed(monkeypatch):
    monkeypatch.setitem(config._data.setdefault("learning", {}), "planner", True)
    monkeypatch.setattr(planner, "plan", lambda *a, **k: ["close finals"])
    assert planner.attempt("close finals", failure="I couldn't find finals open.") is None


def test_planner_is_off_in_tests_by_default():
    assert planner.attempt("minimize all my windows") is None


# ---------------------------------------------------------------- the agent tries harder
def test_agent_plans_an_unknown_request_then_uses_the_skill(monkeypatch, _isolated):
    from modules.agent.agent import agent
    import modules.agent.router as router
    n = []

    def fake_attempt(text, failure=""):
        n.append(text)
        _isolated.learn(text, ["open disk cleanup"])
        return planner.Outcome(Reply("Opened Disk Cleanup."), ["open disk cleanup"], True)
    monkeypatch.setattr(planner, "attempt", fake_attempt)
    monkeypatch.setattr(router, "run_plan", lambda intents: Reply("Opened Disk Cleanup."))
    res = agent.handle("defragment the cleaner thing")
    assert res.ok and "I'll remember how to do that" in res.text and n == ["defragment the cleaner thing"]
    monkeypatch.setattr("modules.agent.router.parse_files", lambda t: None)
    ran = []
    monkeypatch.setattr("modules.learning.skills.run_steps", lambda steps: ran.append(steps) or Reply("Done."))
    res = agent.handle("defragment the cleaner thing")
    assert res.intent == "learning.skill" and ran == [["open disk cleanup"]] and n == ["defragment the cleaner thing"]


def test_agent_tries_harder_after_a_not_found_failure(monkeypatch):
    from modules.agent import agent as agent_mod
    seen = []
    monkeypatch.setattr(agent_mod, "route", lambda t: Intent(
        "desktop.arrange_window", lambda: Reply("I couldn't find a window for all my windows.", ok=False), "desktop"))
    monkeypatch.setattr(planner, "attempt", lambda text, failure="": seen.append(failure) or planner.Outcome(
        Reply("Minimized everything."), ["minimize all my windows"], True))
    res = agent_mod.agent.handle("zap all my windows")
    assert seen == ["I couldn't find a window for all my windows."] and res.ok


def test_agent_offers_to_learn_by_watching_when_it_cannot_plan(monkeypatch):
    from modules.agent import agent as agent_mod
    monkeypatch.setattr(agent_mod, "route", lambda t: None)
    monkeypatch.setattr(planner, "attempt", lambda text, failure="": None)
    started = []
    monkeypatch.setitem(config._data.setdefault("learning", {}), "watch_after_failure", True)
    monkeypatch.setitem(config._data.setdefault("learning", {}), "watch_and_learn", True)
    monkeypatch.setattr(demonstration, "watch_for", lambda phrase: started.append(phrase) or True)
    res = agent_mod.agent.handle("open the thing with the gears")
    # It asks first — nothing is recorded until the user says yes.
    assert not res.ok and res.expects_reply and "Want to show me?" in res.text and started == []
    assert "watching" in agent_mod.agent.handle("yes").text
    assert started == ["open the thing with the gears"]


def test_spotify_not_connected_is_not_retried(monkeypatch):
    from modules.agent import agent as agent_mod
    monkeypatch.setattr(agent_mod, "route", lambda t: Intent(
        "spotify.resume", lambda: Reply("Spotify isn't connected. Connect it in Settings > Spotify.", ok=False),
        "spotify"))
    monkeypatch.setattr(planner, "attempt", lambda *a, **k: pytest.fail("should not plan"))
    assert not agent_mod.agent.handle("play spotify").ok


# ---------------------------------------------------------------- corrections
@pytest.mark.parametrize("said,cmd", [
    ('i dont like that song, i meant for you to play my "moe" playlist on spotify',
     "play my moe playlist on spotify"),
    ("No, I meant open disk cleanup", "open disk cleanup"),
    ("no, close the finals", "close the finals"),
    ("that's not what I asked for, switch to spotify", "switch to spotify"),
])
def test_corrections_are_detected(said, cmd):
    corr_mod.corrections.note("play my party playlist", "spotify.play_playlist", True)
    assert corr_mod.corrections.detect(said) == cmd


def test_not_a_correction():
    corr_mod.corrections.note("play my party playlist", "spotify.play_playlist", True)
    assert corr_mod.corrections.detect("no, it's fine") is None
    assert corr_mod.corrections.detect("play some jazz") is None
    corr_mod.corrections.clear()
    assert corr_mod.corrections.detect("no, I meant open chrome") is None      # nothing to correct


def test_correction_teaches_the_previous_request(monkeypatch, _isolated):
    from modules.agent import agent as agent_mod
    played = []

    def fake_route(text):
        q = text.lower()
        if "playlist" in q:
            return Intent("spotify.play_playlist", lambda: played.append(q) or Reply(f"Playing {q}."), "spotify")
        return None
    monkeypatch.setattr(agent_mod, "route", fake_route)
    agent_mod.agent.handle("play my party playlist")
    res = agent_mod.agent.handle('i dont like that song, i meant for you to play my "moe" playlist on spotify')
    assert "next time you say “play my party playlist”" in res.text
    assert _isolated.match("play my party playlist").steps == ["play my moe playlist on spotify"]


def test_no_plus_command_answers_a_pending_question(monkeypatch):
    from modules.agent import agent as agent_mod
    from modules.agent.confirm import PendingAction, confirmations
    closed = []
    monkeypatch.setattr(agent_mod, "route", lambda t: Intent(
        "desktop.close_app", lambda: closed.append(t) or Reply("Closed THE FINALS."), "desktop"))
    agent_mod.agent.handle("close the finers")
    confirmations.ask(PendingAction("close finers", lambda: "Closed finers."))
    res = agent_mod.agent.handle("No. No, close the finals.")
    assert confirmations.pending is None and closed[-1] == "close the finals" and res.ok


# ---------------------------------------------------------------- talking about learning
def test_learning_commands(_isolated, monkeypatch):
    assert learning_intents.parse("what have you learned")[0] == "list"
    assert "haven't learned" in learning_intents.run("list", "")
    _isolated.learn("open disk clean up", ["open disk cleanup"])
    assert "open disk clean up" in learning_intents.run("list", "")
    assert learning_intents.parse("forget that")[0] == "forget_last"
    assert "back to how it was" in learning_intents.run("forget_last", "")
    assert _isolated.match("open disk clean up") is None
    assert learning_intents.parse("forget that") is None            # nothing learned recently: memory's "forget"
    assert learning_intents.parse("let me show you how to open disk cleanup") == ("watch", "open disk cleanup")
    assert learning_intents.parse("watch me", last_failed="open the gears thing") == ("watch", "open the gears thing")
    assert learning_intents.is_done("that's it") and learning_intents.is_done("Done.")
    assert not learning_intents.is_done("done gaming")


# ---------------------------------------------------------------- watching the user
def _ev(kind, t, **kw):
    return Event(kind, t, **kw)


def test_demonstration_start_menu_launch_becomes_open():
    events = [_ev("keys", 0, keys="win+s", exe="explorer"),
              _ev("keys", 2, keys="enter", exe="searchhost"),
              _ev("launch", 3, app="Disk Cleanup", exe="cleanmgr"),
              _ev("click", 6, name="OK", where="window", app="Disk Cleanup"),
              _ev("focus", 7, app="Disk Cleanup")]
    assert summarize(events) == ["open Disk Cleanup", "click OK", "switch to Disk Cleanup"]


def test_demonstration_start_menu_click_names_the_app():
    events = [_ev("click", 0, name="Start", where="taskbar"),
              _ev("click", 1, name="Disk Cleanup", where="start"),
              _ev("launch", 2, app="cleanmgr", exe="cleanmgr")]
    assert summarize(events) == ["open Disk Cleanup"]


def test_demonstration_desktop_taskbar_and_keys():
    events = [_ev("click", 0, name="Recycle Bin", where="desktop", clicks=2),
              _ev("click", 2, name="Spotify - 1 running window", where="taskbar"),
              _ev("keys", 3, keys="ctrl+shift+esc"),
              _ev("click", 4, name="", where="desktop", button="right"),
              _ev("click", 5, name="", where="window")]              # unnamed clicks can't be repeated
    assert summarize(events) == ["double click Recycle Bin on the desktop", "switch to Spotify",
                                 "press ctrl+shift+esc", "right click the desktop"]


def test_demonstration_steps_are_commands_saint_understands(calls):
    for step in ("double click Recycle Bin on the desktop", "switch to Spotify", "press ctrl+shift+esc",
                 "right click the desktop", "open Disk Cleanup", "click Clean up system files"):
        assert route(step) is not None, step


def test_learn_from_keeps_only_repeatable_steps(_isolated, monkeypatch):
    monkeypatch.setattr(planner, "understood", lambda s: not s.startswith("frobnicate"))
    text = demonstration.learn_from("open disk clean up", ["open Disk Cleanup", "frobnicate"])
    assert "Next time you say “open disk clean up”, I'll open Disk Cleanup" in text and "left it out" in text
    assert _isolated.match("open disk clean up").how == "shown"
    assert "didn't see anything" in demonstration.learn_from("open x", [])


def test_recorder_respects_the_setting():
    assert config.get("learning.watch_and_learn") is False
    assert demonstration.recorder.start("anything") is False


# ---------------------------------------------------------------- today's log, routed
@pytest.mark.parametrize("text,tool,args", [
    ("Minimize all my windows.", "desktop.minimize_all", {}),
    ("minimize the screen.", "desktop.minimize_all", {}),
    ("Minimize my browser and everything else.", "desktop.minimize_all", {}),
    ("Double click the recycling bin.", "desktop.click_element", {"name": "recycle bin", "action": "double_click"}),
    ("click the recycling bin on my main screen.", "desktop.click_element",
     {"name": "recycle bin", "action": "click", "monitor": "main"}),
    ("right click the desktop on my main screen.", "desktop.click_element",
     {"name": "desktop", "action": "right_click", "monitor": "main"}),
    ("double left click the recycle bin", "desktop.click_element", {"name": "recycle bin", "action": "double_click"}),
    ("Oh, oh my gosh, click the top left video", "desktop.click_element", {"name": "first video", "action": "click"}),
    ("switch my audio to my headphones.", "audio.output_device", {"name": "my headphones"}),
    ("mute my mic on voice meter.", "audio.mic_mute", {"state": "on"}),
    ("mute", "audio.mic_mute", {"state": "on"}),
    ("Unmute.", "audio.mic_mute", {"state": "off"}),
    ("Take a screenshot of Claude.", "system.screenshot", {"target": "claude"}),
    ("open disk, clean up.", "files.disk_cleanup", {"drive": "C"}),
])
def test_logged_requests_now_route(calls, text, tool, args):
    it = route(text)
    assert it is not None, text
    it.run()
    assert calls and calls[0] == (tool, args), (text, calls)


def test_minimize_all_and_click_is_one_plan():
    it = route("Minimize all my windows and double left click the recycling bin")
    assert it.name == "composite:desktop.minimize_all+desktop.click_element"


def test_open_youtube_and_fullscreen_it():
    assert route("open YouTube and fullscreen it.").name == "composite:browser.open_url+desktop.fullscreen"


def test_turn_spotify_up_and_down():
    assert route("turn down Spotify.").name == "spotify.volume_down"
    assert route("turn up spotify").name == "spotify.volume_up"


def test_never_play_that_playlist_again():
    assert route("Never play that playlist ever again.").name == "spotify.ban_playlist"
    assert route("don't play this playlist again").name == "spotify.ban_playlist"


def test_right_is_not_filler_before_click():
    from modules.agent.router import _clean
    assert _clean("right click the desktop") == "right click the desktop"
    assert _clean("Right, open chrome") == "open chrome"


def test_bare_mute_can_mean_the_system(monkeypatch, calls):
    monkeypatch.setitem(config._data.setdefault("audio", {}), "bare_mute", "system")
    assert route("mute").name != "audio.mic_mute"


# ---------------------------------------------------------------- closing games / misheard names
def test_fuzzy_window_names():
    from modules.desktop.window_match import best_match, score, skeleton
    assert skeleton("to area") == skeleton("Terraria") == "tr"
    assert score("to area", "Terraria") >= 0.62
    assert score("finers", "THE FINALS") >= 0.62
    assert score("discord", "Spotify") < 0.4

    class W:
        def __init__(self, n):
            self.n = n
    t, s = W("Terraria"), W("Spotify")
    assert best_match("to area", [("Terraria", t), ("Spotify", s)]) is t
    assert best_match("zzz", [("Terraria", t)]) is None


def test_close_asks_with_the_real_name(monkeypatch, calls):
    from modules.desktop.controller import WindowInfo, desktop
    from modules.agent.confirm import confirmations
    w = WindowInfo(hwnd=77, title="THE FINALS", process="Discovery.exe", left=0, top=0, width=800, height=600,
                   monitor=1, minimized=False, maximized=False, foreground=True)
    monkeypatch.setattr(desktop, "find_for_close", lambda name: w)
    monkeypatch.setattr(desktop, "_is_own", lambda win: False)
    from modules.automation.tools import ToolResult, get_tool_registry
    monkeypatch.setattr(get_tool_registry(), "execute", lambda _t, **kw: ToolResult(
        success=False, error="confirm", error_code="CONFIRM_REQUIRED"))
    r = route("close the finers").run()
    assert r.text == "Do you want me to close THE FINALS?" and r.expects_reply
    confirmations.clear()


def test_close_not_found_lists_whats_open(monkeypatch):
    from modules.desktop.controller import desktop
    from modules.automation.tools import ToolError

    def nope(name):
        raise ToolError("I couldn't find finals open. Open right now: Opera, Spotify.", "NOT_FOUND")
    monkeypatch.setattr(desktop, "find_for_close", nope)
    r = route("close the finals").run()
    assert not r.ok and "Open right now: Opera, Spotify" in r.text


def test_app_names_come_from_titles_for_unknown_apps():
    from modules.vision.screen import app_name
    assert app_name("Discovery.exe", "THE FINALS") == "THE FINALS"
    assert app_name("cleanmgr.exe", "Disk Cleanup : Drive Selection") == "Disk Cleanup"
    assert app_name("spotify.exe", "Otis") == "Spotify"


# ---------------------------------------------------------------- Voicemeeter
class FakeVM:
    def __init__(self):
        self.p = {"Strip[0].Mute": 0.0}
        for s in range(5):
            for b in ("A1", "A2", "A3"):
                self.p[f"Strip[{s}].{b}"] = 0.0
        self.p["Strip[3].A2"] = 1.0              # Windows audio on the speakers
        self.p["Strip[1].A1"] = 1.0
        self.devices = {"Bus[0].device.name": "Speakers (HyperX Cloud Core Wireless)",
                        "Bus[1].device.name": "XB273U V3 (NVIDIA High Definiti", "Bus[2].device.name": "",
                        "Strip[0].device.name": "Microphone (LCS USB Audio)"}


@pytest.fixture
def vm(monkeypatch):
    from modules.desktop.voicemeeter import Voicemeeter
    v, fake = Voicemeeter(), FakeVM()
    monkeypatch.setattr(v, "get", lambda p: fake.p[p])
    monkeypatch.setattr(v, "set", lambda p, val: fake.p.__setitem__(p, val))
    monkeypatch.setattr(v, "get_str", lambda p: fake.devices.get(p, ""))
    monkeypatch.setattr(v, "layout", lambda: (5, 3))
    v.fake = fake
    return v


def test_voicemeeter_headphones_and_speakers(vm):
    assert vm.pick_bus("my headphones")["bus"] == "A1"
    assert vm.pick_bus("speakers")["bus"] == "A2"
    r = vm.route_to("headphones")
    assert vm.fake.p["Strip[3].A1"] == 1.0 and vm.fake.p["Strip[3].A2"] == 0.0 and "A1" in r["device"]
    assert vm.fake.p["Strip[0].A1"] == 0.0                 # the mic isn't routed anywhere new
    vm.route_to("speakers")
    assert vm.fake.p["Strip[1].A2"] == 1.0 and vm.fake.p["Strip[1].A1"] == 0.0


def test_voicemeeter_mic_mute(vm):
    assert vm.mute_mic("on")["muted"] and vm.fake.p["Strip[0].Mute"] == 1.0
    assert not vm.mute_mic("toggle")["muted"]


# ---------------------------------------------------------------- Spotify: "my X playlist"
@pytest.fixture
def sp(tmp_path):
    from tests.test_spotify_agent import FakeClient
    from modules.spotify.memory import SpotifyMemory
    from modules.spotify.tools import SpotifyTools

    class Client(FakeClient):
        def playlists(self, limit=50, offset=0):
            return {"items": [{"id": "moe", "uri": "spotify:playlist:moe", "name": "moe", "owner": {"id": "me"}},
                              {"id": "gym", "uri": "spotify:playlist:gym", "name": "Gym Hits", "owner": {"id": "me"}}]}

        def me(self):
            return {"id": "me"}
    t = SpotifyTools(Client())
    t._memory = SpotifyMemory(str(tmp_path / "spotify.db"))
    return t


def test_my_playlist_is_never_a_strangers(sp):
    from modules.spotify.client import SpotifyAPIError
    with pytest.raises(SpotifyAPIError) as e:
        sp.play_query("party", "playlist", own_only=True)
    assert e.value.code == "NOT_MINE"


def test_my_playlist_found_through_memory_and_remembered(sp, monkeypatch):
    from modules.memory import service

    class R:
        def __init__(self, c):
            self.entry = type("E", (), {"content": c})()
    monkeypatch.setattr(service.memory_service, "recall", lambda *a, **k: [R("moe playlist is my PARTY playlist")])
    r = sp.play_query("party", "playlist", own_only=True)
    assert r["name"] == "moe" and r["owned"]
    assert sp.memory.resolve_playlist_alias("party")["playlist_name"] == "moe"


def test_quoted_names_resolve(sp):
    assert sp.play_query("'moe'", "playlist", own_only=True)["name"] == "moe"


def test_banned_playlists_are_skipped(sp):
    sp.client.state = dict(sp.client.state, context={"uri": "spotify:playlist:pj"})
    sp.client.request = lambda *a, **k: {"name": "Jazz Classics"}
    assert sp.ban_playlist()["banned"] == "Jazz Classics"
    assert "spotify:playlist:pj" in sp.memory.banned_playlists()
    assert sp.resolve("jazz", "playlist") is None


def test_llm_cannot_ban_or_give_feedback():
    from modules.automation.tools import get_tool_registry
    names = {t.name for t in get_tool_registry().llm_tools()}
    assert not names & {"spotify.ban_artist", "spotify.feedback", "spotify.ban_playlist", "desktop.force_quit"}


# ---------------------------------------------------------------- random speech, repeats, honesty
def test_follow_up_windows_after_actions_and_barge_in_are_gated():
    from modules.voice.module import VoiceModule
    assert VoiceModule.unaddressed_window("follow-up")
    assert VoiceModule.unaddressed_window("follow-up (action)")
    assert VoiceModule.unaddressed_window("interruption")
    assert not VoiceModule.unaddressed_window("wake word")
    assert not VoiceModule.unaddressed_window("awaiting reply")


def test_repeated_request_with_wake_word_is_kept():
    from modules.voice.stt_filters import clean_transcript
    assert clean_transcript("Turn off the mini-player Hey SAINT, turn off the mini-player") == \
        "Hey SAINT, turn off the mini-player"
    assert clean_transcript("turn off the mini player turn off the mini player") == "turn off the mini player"
    assert clean_transcript("skip skip skip skip") == "skip"
    assert clean_transcript("No, no, no, no, no, no, no, no, no, no, no, no") is None


def test_stop_talking_thank_you_is_a_stop():
    from modules.agent.meta import match_meta
    assert match_meta("Stop talking, please. Thank you.").kind == "stop"


def test_code_style_tool_calls_are_parsed_and_never_shown():
    from modules.agent.output import clean_reply, extract_tool_calls
    assert extract_tool_calls('`desktop__click_element(name="top left video", action="click")`') == \
        [("desktop__click_element", {"name": "top left video", "action": "click"})]
    assert clean_reply("`screen__locate(name='open disk')` followed by `desktop__scroll(amount=1)`") == ""
    assert clean_reply("Here's how you could ask that:\n```\nautomation__schedule_command(command=\"x\")\n```") == ""


@pytest.mark.parametrize("text,claim", [
    ("Playing your 'moe' playlist on Spotify.", True),
    ("Disk Cleanup is open.", True),
    ("Spotify volume increased.", True),
    ("Nothing is playing right now.", False),
    ("It's 9:08 PM.", False),
    ("Your E drive is almost full.", False),
])
def test_unbacked_action_claims(text, claim):
    from modules.agent.output import unverified_action_claim
    assert unverified_action_claim(text) is claim


def test_activity_reports_task_progress():
    from core.activity import Activity
    a = Activity()
    a.add_background("t1", "checking E: for junk")
    a.set_progress("t1", 0.42)
    assert "about 42% done" in a.describe()
    a.remove_background("t1")
    assert "Nothing right now" in a.describe()


def test_whisper_is_relative_to_how_loud_the_user_talks():
    from modules.voice.output_policy import OutputPolicy
    p = OutputPolicy()
    for r in (0.06, 0.07, 0.058, 0.065, 0.061, 0.066):
        p.note_input(r)
    p.note_input(0.032)                    # above the old fixed 0.02 floor, but half their normal level
    assert p.gain() < 1.0
    p.note_input(0.06)
    assert p.gain() == 1.0


def test_no_plan_hands_over_to_the_model_then_offers_to_learn(monkeypatch):
    from modules.agent import agent as agent_mod
    monkeypatch.setattr(agent_mod, "route", lambda t: None)
    monkeypatch.setattr(planner, "attempt", lambda text, failure="": None)
    monkeypatch.setitem(config._data["ai"], "provider", "ollama")
    monkeypatch.setitem(config._data["ai"], "tool_calling", True)
    assert agent_mod.agent.handle("add this song to my party playlist") is None      # the model's tools try next
    from modules.ai.module import AIModule
    ai = AIModule.__new__(AIModule)
    import threading
    ai._cancel_flag = threading.Event()
    said = []
    monkeypatch.setitem(config._data.setdefault("learning", {}), "watch_after_failure", True)
    monkeypatch.setitem(config._data.setdefault("learning", {}), "watch_and_learn", True)
    monkeypatch.setattr(demonstration, "watch_for", lambda p: True)
    extra = ai._offer_to_learn("add this song to my party playlist", {"tool_calls": []}, said.append)
    assert "Want to show me?" in extra and said == [extra] and ai.expects_reply
    from modules.agent.confirm import confirmations
    confirmations.clear()
    assert ai._offer_to_learn("add this song to my party playlist",
                              {"tool_calls": [{"tool": "spotify.add_current_to_playlist", "success": True}]},
                              said.append) == ""

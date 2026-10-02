"""
Short-term memory and the fixes from the 2026-09-25 evening log:
"delete it" / "open that folder" / "what did you find?", corrections that
name only the thing, spelled-out letters, watch-and-learn that asks first,
real scrolling, the Storage page's junk rules, and the voice gate for
one-word answers.
"""

import os
import time

import pytest

from core.config import config
from modules.agent.confirm import choices, confirmations
from modules.agent.recent import recent
from modules.agent.router import route, spell_out


@pytest.fixture(autouse=True)
def _clean():
    recent.clear()
    confirmations.clear()
    choices.clear()
    yield
    recent.clear()
    confirmations.clear()
    choices.clear()


def _shot(tmp_path):
    p = tmp_path / "Screenshot Claude.png"
    p.write_bytes(b"png")
    recent.note_tool("system.screenshot", {}, {"path": str(p), "what": "Claude"})
    return str(p)


# ---------------------------------------------------------------- recent things
def test_references_resolve_to_what_saint_just_did(tmp_path):
    shot = _shot(tmp_path)
    assert recent.find("that screenshot", {"screenshot", "file"}).path == shot
    assert recent.find("it", {"file", "screenshot"}).path == shot
    assert recent.find("the loop folder", {"folder"}) is None          # a name that isn't here: no guess


def test_gone_and_old_things_are_skipped(tmp_path):
    shot = _shot(tmp_path)
    os.remove(shot)
    assert recent.find("that screenshot", {"screenshot"}) is None
    p = tmp_path / "old.png"
    p.write_bytes(b"x")
    th = recent.note("screenshot", "an old one", str(p), "took")
    th.at = time.time() - 3600
    assert recent.find("that screenshot", {"screenshot"}) is None


def test_extracted_folder_and_archive_are_remembered(tmp_path):
    folder = tmp_path / "TL B25328668~AG"
    folder.mkdir()
    (tmp_path / "TL.zip").write_bytes(b"x")
    recent.note_task({"status": "completed", "result": {"archive": str(tmp_path / "TL.zip"), "dest": str(tmp_path),
                                                         "folder": str(folder)}})
    assert recent.find("the folder you just extracted", {"folder"}).path == str(folder)
    assert "extracted" in recent.describe()


def test_delete_that_screenshot_asks_first(tmp_path):
    _shot(tmp_path)
    it = route("delete that screenshot")
    assert it.name == "files.recycle"
    r = it.run()
    assert r.expects_reply and confirmations.pending.tool == "files.recycle"


def test_open_that_folder_and_move_it(tmp_path, monkeypatch):
    folder = tmp_path / "TL"
    folder.mkdir()
    games = tmp_path / "Games"
    games.mkdir()
    monkeypatch.setitem(config._data.setdefault("files", {}), "games_dir", str(games))
    recent.note("folder", "TL", str(folder), "extracted")
    assert route("Open that folder for me.").name == "files.open_recent"
    assert route("move it to my games folder").name == "files.move"
    assert route("rename it to TheLoop").name == "files.rename"


def test_nothing_to_point_at_asks_instead_of_opening_an_app():
    it = route("open that folder")
    assert it.name == "recent.unknown" and "which one" in it.run().text


def test_delete_never_means_forget_a_memory():
    for t in ("Delete the other old installers too.", "Delete the 1.35 gigabytes of junk."):
        it = route(t)
        assert it is None or not it.name.startswith("memory.")
    assert route("forget my birthday").name == "memory.forget"


def test_cleanup_follow_ups_use_the_last_check(tmp_path):
    inst = tmp_path / "setup.exe"
    inst.write_bytes(b"x" * 20)
    dup = tmp_path / "a (1).zip"
    dup.write_bytes(b"x" * 10)
    recent.note_tool("files.cleanup_plan", {}, {"scope": "downloads", "summary": "", "findings": [
        {"category": "old_installer", "path": str(inst), "size": 20, "action": "review", "reason": ""},
        {"category": "duplicate", "path": str(dup), "size": 10, "action": "recycle", "reason": ""}]})
    r = route("Delete the other old installers too.").run()
    assert r.expects_reply and "1 item (20 bytes)" in r.text
    confirmations.clear()
    r = route("All right, so delete it").run()
    assert r.expects_reply and "1 item (10 bytes)" in r.text


def test_make_a_folder(tmp_path, monkeypatch):
    games = tmp_path / "Games"
    games.mkdir()
    monkeypatch.setitem(config._data.setdefault("files", {}), "games_dir", str(games))
    r = route("Make a new folder in my games folder for the loop.").run()
    assert (games / "The Loop").is_dir() and "The Loop" in r.text
    route("make a new folder called TheLoop2 in my games folder").run()
    assert (games / "TheLoop2").is_dir()


def test_spoken_folder_on_a_drive(tmp_path, monkeypatch):
    from modules.files.paths import _existing_child, resolve_folder
    (tmp_path / "TheLoop").mkdir()
    assert _existing_child(str(tmp_path), "the loop") == str(tmp_path / "TheLoop")
    monkeypatch.setitem(config._data.setdefault("files", {}), "games_dir", str(tmp_path))
    assert resolve_folder("games/the loop") == str(tmp_path / "TheLoop")


def test_the_model_is_told_what_really_happened(tmp_path):
    _shot(tmp_path)
    assert "screenshot of Claude" in recent.describe()


# ---------------------------------------------------------------- corrections
@pytest.mark.parametrize("prev,said,fixed", [
    ("Play my party playlist", "No, I meant my MO playlist.", "Play my MO playlist"),
    ("play my party playlist", 'I meant my "moe" playlisy', "play my moe playlist"),
    ("play my party playlist", "no I meant open my MO playlist. Pronounced spelled M-O-E.", "open my moe playlist"),
    ("Switch to Glod.", 'I said "Claude" btw', "Switch to Claude"),
    ("put clawed on my right screen", "I said Claude", "put Claude on my right screen"),
    ("Open that folder for me.", "No, I meant the folder that you just created.",
     "Open the folder that you just created for me"),
    ("open blockstrap", "I meant Roblox", "open Roblox"),
    ("Play my party playlist", "no I meant my moe one", "Play my moe playlist"),
    ("play my party playlist", "no, it's fine", None),
    ("open disk cleanup", "no no no", None),
    ("what time is it", "I meant in London", None),
])
def test_corrections_that_name_only_the_thing(prev, said, fixed):
    from modules.learning.corrections import corrections
    corrections.note(prev, "x.y", True)
    assert corrections.detect(said) == fixed


def test_a_correction_answers_an_open_did_you_mean_question(monkeypatch):
    from modules.agent import agent as agent_mod
    from modules.agent.confirm import ChoiceOption, PendingChoice
    from modules.learning.corrections import corrections
    corrections.note("open that folder for me", "desktop.open_app", False)
    choices.ask(PendingChoice("Did you mean A, B?", [ChoiceOption("A", "A", "A"), ChoiceOption("B", "B", "B")],
                              lambda o: "picked"))
    seen = []
    monkeypatch.setattr(agent_mod.Agent, "_corrected", lambda self, text, fixed: seen.append(fixed) or
                        agent_mod.AgentResult("ok", "x"))
    agent_mod.agent.handle("No, I meant the folder you just made")
    assert seen == ["open the folder you just made for me"] and choices.pending is None


def test_spelled_letters():
    assert spell_out("play my M-O-E playlist") == "play my moe playlist"
    assert spell_out("open my MO playlist, spelled m o e") == "open my moe playlist"
    assert spell_out("turn on wi-fi") == "turn on wi-fi"


# ---------------------------------------------------------------- learning
def test_pointing_requests_are_never_learned():
    from modules.learning.skills import SkillStore
    for phrase in ("switch back", "delete it", "open that folder", "do it again"):
        assert not SkillStore.learnable(phrase, ["switch to SAINT"])
    assert SkillStore.learnable("minimize all my wonders", ["show the desktop"])


def test_skills_can_be_edited_and_added(tmp_path):
    from modules.learning.skills import SkillStore
    store = SkillStore(str(tmp_path / "skills.json"))
    s = store.update(None, "gaming time", ["open Steam", "open Discord"])
    assert s is not None and store.match("gaming time").steps == ["open Steam", "open Discord"]
    s2 = store.update(s.id, "game night", ["open Steam"])
    assert s2.id == s.id and store.match("gaming time") is None and store.match("game night").how == "edited"
    store.forget(s.id)


@pytest.mark.parametrize("text,done", [("I'm all done.", True), ("All done.", True), ("i am done now", True),
                                       ("that's it", True), ("I'm done gaming", False)])
def test_done_phrases(text, done):
    from modules.learning import intents
    assert intents.is_done(text) == done


@pytest.mark.parametrize("text", ["Watch what I wanted you to do.", "watch me do it", "can you watch me",
                                  "I'm going to do the action now. Can you watch me do it so you learn?"])
def test_watch_phrases(text):
    from modules.learning import intents
    assert intents.parse(text, last_failed="open bloxstrap") == ("watch", "open bloxstrap")


def test_make_a_shortcut_by_voice(monkeypatch):
    from modules.learning import intents
    from modules.learning.skills import skills
    kind, arg = intents.parse("make a shortcut called gaming time that opens steam and discord")
    assert kind == "make"
    name, steps = arg.split("\n", 1)
    assert intents._split_steps(steps) == ["open steam", "open discord"]
    learned = []
    monkeypatch.setattr(skills, "learn", lambda p, s, how: learned.append((p, s)) or object())
    assert "gaming time" in intents.run(kind, arg) and learned == [("gaming time", ["open steam", "open discord"])]


def test_save_that_as(monkeypatch):
    from modules.learning import intents
    from modules.learning.corrections import corrections
    from modules.learning.skills import skills
    corrections.note("open steam", "desktop.open_app", True)
    learned = []
    monkeypatch.setattr(skills, "learn", lambda p, s, how: learned.append((p, s, how)) or object())
    kind, arg = intents.parse("save that as game time")
    assert "Saved" in intents.run(kind, arg) and learned == [("game time", ["open steam"], "saved")]


@pytest.mark.parametrize("step,said,ok", [
    ("click Settings", "click on the loop layer", False),
    ("click The Loopler", "click on the loop layer", True),
    ("press delete", "go to my most recent download and delete it", False),
    ("press f", "press f to fullscreen", True),
])
def test_planner_only_clicks_and_presses_what_was_said(step, said, ok):
    from modules.learning.planner import grounded
    assert grounded(step, said) == ok


def test_demonstration_drops_what_apps_did_by_themselves():
    from modules.learning.demonstration import Event, summarize
    t = 100.0
    events = [Event("click", t, where="taskbar", name="Bloxstrap pinned"),
              Event("launch", t + 1, app="Bloxstrap", exe="bloxstrap-v2.10.0"),
              Event("click", t + 2, app="Bloxstrap", exe="bloxstrap-v2.10.0", name=""),
              Event("launch", t + 5, app="Bloxstrap", exe="bloxstrap-v2.11.4"),
              Event("launch", t + 13, app="Roblox", exe="robloxplayerbeta")]
    assert summarize(events) == ["open Bloxstrap"]


def test_demonstration_sees_moves_and_ignores_title_clicks():
    from modules.learning.demonstration import Event, summarize
    events = [Event("click", 1.0, app="Chrome", name="AstralGames ~ The Loopler", title="AstralGames ~ The Loopler"),
              Event("click", 2.0, app="Explorer", name="Items View", button="right"),
              Event("moved", 3.0, app="Claude", where="left"),
              Event("moved", 4.0, app="Claude", where="right")]
    assert summarize(events) == ["move Claude to my right screen"]


def test_learning_by_watching_asks_first(monkeypatch):
    from modules.learning import demonstration
    monkeypatch.setitem(config._data.setdefault("learning", {}), "watch_and_learn", True)
    started = []
    monkeypatch.setattr(demonstration, "watch_for", lambda p: started.append(p) or True)
    assert demonstration.offer("open the thing")
    assert started == [] and confirmations.pending.tool == "learning.watch"
    assert "watching" in confirmations.resolve("yes") and started == ["open the thing"]


# ---------------------------------------------------------------- scrolling
def test_scroll_uses_real_wheel_notches(monkeypatch):
    import pyautogui
    from modules.desktop.controller import desktop
    got = []
    monkeypatch.setattr(pyautogui, "scroll", lambda n: got.append(n))
    monkeypatch.setitem(config._data.setdefault("desktop", {}), "enabled", True)
    monkeypatch.setattr(desktop, "target_window", lambda *a, **k: None)
    desktop.scroll(-5)
    assert got == [-600]


def test_scroll_more_goes_further(monkeypatch):
    from modules.agent import desktop_intents as di
    amounts = []
    monkeypatch.setattr(di, "run_tool", lambda tool, d, ok, amount: amounts.append(amount) or
                        __import__("modules.agent.router", fromlist=["Reply"]).Reply("ok"))
    route("scroll down").run()
    route("scroll more").run()
    route("keep scrolling").run()
    assert amounts[0] == -5 and amounts[1] < amounts[0] and amounts[2] < amounts[1]


# ---------------------------------------------------------------- misc fixes
def test_loop_only_means_the_video():
    from modules.desktop import youtube
    assert youtube.parse("click on the loop layer", True) is None
    assert youtube.parse("make a new folder in my games folder for the loop", True) is None
    assert youtube.parse("loop this video", True) == ("loop_on", None)


def test_split_app_names_resolve(monkeypatch):
    from modules.desktop.apps import AppCatalog, AppEntry
    cat = AppCatalog()
    cat._entries = {"bloxstrap": AppEntry("Bloxstrap", "path", "x.lnk", "shortcut")}
    assert cat.resolve("block strap").name == "Bloxstrap"
    assert cat.resolve("blockstrap").name == "Bloxstrap"


def test_fake_action_claims():
    from modules.agent.output import unverified_action_claim as claim
    assert claim('Made a new folder named "x" in the Games folder.')
    assert claim("The screenshot was deleted.")
    assert not claim("I made a mistake earlier.")
    assert not claim("I wrote a short poem for you:")


def test_recycle_bin_is_shown_but_never_recycled():
    from modules.files.tools import ask_to_recycle_findings
    assert ask_to_recycle_findings([{"category": "recycle_bin", "path": "shell:RecycleBinFolder", "size": 5,
                                     "action": "info"}]) is None


# ---------------------------------------------------------------- voice gate
def test_one_word_answers_pass_when_saint_asked(monkeypatch):
    from modules.agent.confirm import PendingAction
    from modules.voice.module import VoiceModule
    vm = VoiceModule.__new__(VoiceModule)
    vm._music_playing = False
    monkeypatch.setattr(VoiceModule, "_ai_expects_reply", staticmethod(lambda: False))
    assert not vm._passes_activation_gate("Yeah.", 0.1, follow_up=True)[0]
    confirmations.ask(PendingAction("close Disk Cleanup", lambda: "ok"))
    assert vm._passes_activation_gate("Yeah.", 0.1, follow_up=True)[0]


# ---------------------------------------------------------------- Spotify login
class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _auth(monkeypatch, resp):
    import requests
    from modules.spotify.auth import SpotifyAuth, SpotifyToken
    monkeypatch.setitem(config._data.setdefault("spotify", {}), "client_id", "abc")
    a = SpotifyAuth()
    a._token = SpotifyToken("old", "refresh", time.time() - 10)
    cleared = []
    monkeypatch.setattr(a, "clear", lambda: cleared.append(True))
    monkeypatch.setattr(requests, "post", lambda *a_, **k: resp)
    return a, cleared


def test_a_spotify_outage_keeps_the_login(monkeypatch):
    a, cleared = _auth(monkeypatch, _Resp(503, {}))
    with pytest.raises(RuntimeError):
        a.get_access_token()
    assert cleared == []


def test_a_revoked_login_is_forgotten(monkeypatch):
    a, cleared = _auth(monkeypatch, _Resp(400, {"error": "invalid_grant"}))
    with pytest.raises(RuntimeError):
        a.get_access_token()
    assert cleared == [True]


def test_no_client_id_never_touches_the_saved_login(monkeypatch):
    import requests
    from modules.spotify.auth import SpotifyAuth, SpotifyToken
    monkeypatch.setitem(config._data.setdefault("spotify", {}), "client_id", "")
    a = SpotifyAuth()
    a._token = SpotifyToken("old", "refresh", time.time() - 10)
    monkeypatch.setattr(requests, "post", lambda *a_, **k: pytest.fail("must not refresh"))
    assert a.get_access_token() is None


def test_a_text_file_in_the_folder_just_made(tmp_path, monkeypatch):
    """2026-10-02: "make a new folder in downloads" worked, then "add a text file in that folder" went to the
    model, which answered with os.system(...) code and offered to learn; the taught steps were then dropped."""
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setitem(config._data.setdefault("files", {}), "games_dir", str(dl))
    route("make a new folder in my games folder").run()
    assert (dl / "New folder").is_dir()
    r = route("add a text file in that folder").run()
    assert r.ok and (dl / "New folder" / "New Text Document.txt").is_file(), r.text
    route("make a text file in that folder").run()                 # said again: a second file, never overwritten
    assert (dl / "New folder" / "New Text Document (2).txt").is_file()
    route("create a text file called notes in that folder").run()
    assert (dl / "New folder" / "Notes.txt").is_file()


def test_a_file_in_a_folder_nobody_made_asks_which():
    r = route("add a text file in that folder").run()
    assert not r.ok and "Which folder" in r.text


def test_leaked_shell_commands_are_never_said():
    from modules.agent.output import clean_reply
    assert clean_reply("os.system('echo \"Hello World!\" > newfile.txt')", "add a text file in that folder") == ""
    assert clean_reply("The command you're looking for is:\n\ntouch filename.txt", "make a text file") == ""
    assert clean_reply("touch filename.txt", "write a bash script that makes a file") == "touch filename.txt"

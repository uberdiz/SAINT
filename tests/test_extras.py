"""Hands-free extras: Windows controls, clipboard, workspaces, watchers,
'what changed?', developer mode and 'handle this' — without touching the
real desktop."""

import os
import time

import pytest

from modules.agent.router import route


def _name(text):
    it = route(text)
    return it and it.name


# ---------------------------------------------------------------- Windows controls
@pytest.mark.parametrize("text,intent", [
    ("lock my pc", "system.lock"), ("lock", "system.lock"),
    ("put the computer to sleep", "system.power"), ("restart my pc", "system.power"),
    ("shut down the computer", "system.power"), ("cancel the shutdown", "system.cancel_shutdown"),
    ("mute my mic", "audio.mic_mute"), ("turn on my microphone", "audio.mic_mute"),
    ("set the system volume to 40", "audio.system_volume"), ("set discord to 30 percent", "audio.app_volume"),
    ("mute the game", "audio.app_volume"), ("turn discord down", "audio.app_volume"),
    ("switch audio to my headphones", "audio.output_device"), ("use my speakers", "audio.output_device"),
    ("brightness 60", "display.brightness"), ("set brightness to 60", "display.brightness"),
    ("dim the screen", "display.brightness"), ("do not disturb", "system.do_not_disturb"),
    ("take a screenshot", "system.screenshot"),
])
def test_windows_control_phrases(text, intent):
    assert _name(text) == intent, (text, _name(text))


@pytest.mark.parametrize("text", ["set a timer for 10 minutes", "turn it down", "set the theme to dark",
                                  "turn off the halo", "mute the video"])
def test_windows_controls_leave_other_commands(text):
    n = _name(text)
    assert not (n or "").startswith(("audio.", "system.", "display.")), (text, n)


def test_power_actions_always_ask():
    from modules.automation.tools import get_tool_registry
    reg = get_tool_registry()
    tool = reg.get("system.power")
    assert tool is not None and not tool.llm_exposed
    assert reg.execute("system.power", action="shutdown").error_code == "CONFIRM_REQUIRED"


def test_output_device_matching():
    from modules.desktop.system_controls import match_output
    names = ["Voicemeeter In 4 (VB-Audio Voicemeeter VAIO)", "XB273U V3 (NVIDIA High Definition Audio)",
             "MSI G242P (NVIDIA High Definition Audio)", "Speakers (HyperX Cloud Core Wireless)",
             "Headphones (Oculus Virtual Audio Device)", "Realtek Digital Output (Realtek(R) Audio)"]
    assert match_output("headphones", names)[0] == "Speakers (HyperX Cloud Core Wireless)"
    assert match_output("my headset", names)[0] == "Speakers (HyperX Cloud Core Wireless)"
    assert match_output("monitor", names)[0].startswith("XB273U")
    assert match_output("msi", names) == ["MSI G242P (NVIDIA High Definition Audio)"]
    assert match_output("speakers", names)[0].startswith("Realtek")


# ---------------------------------------------------------------- clipboard
def test_secret_detection():
    from modules.desktop.clipboard import looks_secret
    for s in ("sk-abc123DEF456ghi789JKL", "ghp_16C7e42F292c6912E7710c838347Ae178B4a", "Tr0ub4dor&3xyzQ!",
              "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc"):
        assert looks_secret(s), s
    for s in ("hello world", "https://github.com/some/repo", "C:\\Users\\me\\file.txt", "short", ""):
        assert not looks_secret(s), s


@pytest.mark.parametrize("text,intent", [
    ("read my clipboard", "clipboard.read"), ("what did I copy", "clipboard.read"),
    ("summarize what I copied", "clipboard.summarize"),
    ("translate what I copied into spanish", "clipboard.translate"),
    ("fix the code I copied", "clipboard.fix_code"), ("turn what I copied into an email", "clipboard.email"),
    ("explain the error I copied", "clipboard.explain_error"),
])
def test_clipboard_phrases(text, intent):
    assert _name(text) == intent


@pytest.fixture
def fake_clipboard(monkeypatch):
    box = {"text": ""}
    import modules.desktop.clipboard as cb
    monkeypatch.setattr(cb, "read_text", lambda: box["text"])
    monkeypatch.setattr(cb, "write_text", lambda t: box.__setitem__("text", t))
    return box


def test_clipboard_read_hides_secrets(fake_clipboard):
    fake_clipboard["text"] = "sk-abc123DEF456ghi789JKL"
    assert "won't read it" in route("read my clipboard").run().text
    fake_clipboard["text"] = "Buy milk and eggs."
    assert route("read my clipboard").run().text == "Buy milk and eggs."


def test_fixed_code_goes_back_on_the_clipboard(fake_clipboard, monkeypatch):
    fake_clipboard["text"] = "def add(a, b):\n    return a - b"
    monkeypatch.setattr("modules.agent.llm.complete",
                        lambda prompt, **k: "```python\ndef add(a, b):\n    return a + b\n```")
    r = route("fix the code I copied").run()
    assert "clipboard" in r.text and fake_clipboard["text"] == "def add(a, b):\n    return a + b"


# ---------------------------------------------------------------- workspaces
def test_workspace_store_and_matching(tmp_path):
    from modules.workspace.store import WorkspaceStore, plan_matches
    store = WorkspaceStore(str(tmp_path / "ws.json"))
    snap = {"windows": [{"process": "Code.exe", "title": "main.py - SAINT - Visual Studio Code", "monitor": 1,
                         "normal": [0, 0, 800, 600], "state": "max", "exe": "C:\\code.exe"},
                        {"process": "opera.exe", "title": "GitHub - Opera", "monitor": 2, "normal": [1920, 0, 2900, 800],
                         "state": "normal", "exe": "C:\\opera.exe"},
                        {"process": "opera.exe", "title": "YouTube - Opera", "monitor": 2,
                         "normal": [2000, 0, 2900, 800], "state": "normal", "exe": "C:\\opera.exe"}],
            "spotify": None}
    store.save("Coding", snap)
    assert store.get("coding")["name"] == "Coding" and store.get("codng") is not None
    W = type("W", (), {})

    def win(hwnd, proc, title):
        w = W()
        w.hwnd, w.process, w.title = hwnd, proc, title
        return w
    current = [win(1, "opera.exe", "YouTube - Opera"), win(2, "opera.exe", "Reddit - Opera"),
               win(3, "Discord.exe", "Discord")]
    pairs = plan_matches(snap["windows"], current)
    assert pairs[0][1] is None                                   # VS Code isn't open -> launch
    assert pairs[2][1].hwnd == 1                                 # the YouTube window keeps its place
    assert pairs[1][1].hwnd == 2                                 # the other Opera window takes the rest
    assert store.delete("coding") and store.get("coding") is None


@pytest.mark.parametrize("text,intent", [
    ("save this workspace as Coding", "workspace.save"), ("restore coding workspace", "workspace.restore"),
    ("what workspaces do I have", "workspace.list"), ("forget the gaming workspace", "workspace.forget"),
])
def test_workspace_phrases(text, intent):
    assert _name(text) == intent


# ---------------------------------------------------------------- watchers
class FakeWorld:
    def __init__(self):
        self.now = 1000.0
        self.pics = {}
        self.titles = {}
        self.files = {}
        self.alive = {1: True}


@pytest.fixture
def world(monkeypatch):
    from modules.watch.watchers import WatchManager
    w = FakeWorld()
    fired = []
    mgr = WatchManager(clock=lambda: w.now, grab=lambda h: w.pics.get(h), title=lambda h: w.titles.get(h),
                       files=lambda folder: dict(w.files), alive=lambda watch: w.alive.get(watch.hwnd, True))
    mgr.start = lambda: None                  # tick by hand
    mgr.on_fire = lambda watch, msg: fired.append(msg)
    return w, mgr, fired


def test_finished_means_changed_then_still(world):
    from modules.watch.watchers import Watch
    w, mgr, fired = world
    w.pics[1] = [10] * 2304
    mgr.add(Watch("finished", "Claude", hwnd=1))
    for _ in range(5):                       # nothing moving yet: not finished
        w.now += 1
        mgr.tick()
    assert fired == []
    for i in range(4):                       # it's writing an answer
        w.now += 1
        w.pics[1] = [10 + 20 * (i + 1)] * 2304
        mgr.tick()
    assert fired == []
    for _ in range(9):                       # stopped changing for 8+ s
        w.now += 1
        mgr.tick()
    assert fired == ["Claude looks finished."] and mgr.last_fired.label == "Claude"


def test_download_and_close_watches(world):
    from modules.watch.watchers import Watch
    w, mgr, fired = world
    w.files = {"old.zip": 10}
    mgr.add(Watch("download", "your download", folder="x"))
    w.files["game.rar"] = 500
    mgr.tick()
    w.now += 1
    w.files["game.rar"] = 900                # still growing
    mgr.tick()
    w.now += 4
    mgr.tick()
    assert fired == ["Your download finished: game.rar."]
    mgr.add(Watch("closed", "Steam", hwnd=1))
    mgr.tick()
    w.alive[1] = False
    mgr.tick()
    assert fired[-1] == "Steam closed."


@pytest.mark.parametrize("text,intent", [
    ("tell me when Claude finishes", "watch.add"), ("tell me when this download finishes", "watch.add"),
    ("tell me when steam closes", "watch.add"), ("what changed", "watch.what_changed"),
    ("what changed while I was away", "watch.what_changed"), ("watch my left screen", "watch.follow_screen"),
    ("use all screens", "watch.follow_screen"), ("stop watching", "watch.cancel"),
])
def test_watch_phrases(text, intent):
    assert _name(text) == intent


def test_what_changed_description():
    from modules.watch.snapshots import Snapshot, SnapshotLog, describe_changes
    old = Snapshot(0, {1: ("Code.exe", "main.py - VS Code", 1), 2: ("Discord.exe", "Discord", 2)}, {1: 0, 2: 0})
    new = Snapshot(600, {1: ("Code.exe", "Build succeeded - VS Code", 1), 3: ("Spotify.exe", "Otis", 2)},
                   {1: 0, 2: 0xFFFF})
    text = describe_changes(old, new, label=lambda e: e[0].split(".")[0])
    assert text.startswith("In the last 10 minutes") and "Spotify opened" in text and "Discord closed" in text
    assert "Build succeeded" in text
    log = SnapshotLog(taker=lambda: None)
    for i, idle in enumerate([0, 0, 0, 120, 300, 5]):
        log.add(Snapshot(i * 60.0, {}, {}, idle))
    base = log.baseline(since_left=True)
    assert base is not None and base.at <= 60 * 4 - 300 + 60


# ---------------------------------------------------------------- developer mode
def test_error_locations(tmp_path):
    from modules.dev.tools import best_location, error_locations
    mine = tmp_path / "app" / "main.py"
    mine.parent.mkdir()
    mine.write_text("x")
    lib = tmp_path / "venv" / "Lib" / "site-packages" / "requests" / "api.py"
    lib.parent.mkdir(parents=True)
    lib.write_text("x")
    tb = (f'Traceback (most recent call last):\n  File "{mine}", line 12, in <module>\n'
          f'  File "{lib}", line 59, in get\nValueError: nope')
    locs = error_locations(tb, base=str(tmp_path))
    assert (str(mine), 12) in locs and (str(lib), 59) in locs
    assert best_location(locs, base=str(tmp_path)) == (str(mine), 12)
    assert error_locations("app/main.py:7: error: bad", base=str(tmp_path)) == [(str(mine), 7)]
    assert error_locations("nothing here", base=str(tmp_path)) == []


@pytest.mark.parametrize("text,intent", [
    ("run the tests", "dev.run_tests"), ("open the file causing the error", "dev.open_error_file"),
    ("handle this", "handle_this"), ("take care of this for me", "handle_this"),
])
def test_dev_phrases(text, intent):
    assert _name(text) == intent


def test_run_tests_is_never_offered_to_the_llm():
    from modules.automation.tools import get_tool_registry
    assert not get_tool_registry().get("dev.run_tests").llm_exposed


# ---------------------------------------------------------------- handle this
def test_handle_this_validation():
    from modules.agent.handle_this import parse_proposal, validate
    screen = "Update available\nInstall now\nRemind me later"
    p = parse_proposal('Sure! {"action": "click", "target": "Remind me later", "why": "An update is ready"}')
    assert p and validate(p, screen) is None
    assert validate({"action": "click", "target": "Delete account"}, screen) == "that button isn't on screen"
    assert validate({"action": "press", "target": "alt+f4"}, screen)
    assert validate({"action": "open_url", "target": "file:///c:/x"}, screen)
    assert validate({"action": "none", "target": ""}, screen) == "nothing to do"
    assert parse_proposal("no json here") is None


def test_handle_this_always_asks(monkeypatch):
    from modules.agent import handle_this as h
    from modules.agent.confirm import confirmations

    class R:
        success, error = True, None
        result = {"window": "Updater", "text": ["Update available", "Install now", "Remind me later"]}
    clicks = []
    monkeypatch.setattr(h, "call", lambda tool, **kw: clicks.append((tool, kw)) or R())
    monkeypatch.setattr("modules.agent.llm.complete",
                        lambda *a, **k: '{"action": "click", "target": "Install now", "why": "An update is ready"}')
    try:
        r = h.handle_this()
        assert r.expects_reply and "Should I click" in r.text
        assert [c for c in clicks if c[0] == "desktop.click_element"] == []       # nothing clicked yet
        assert confirmations.pending is not None
    finally:
        confirmations.clear()

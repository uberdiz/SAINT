"""Game Mode (core/game_mode.py): SAINT's overlays stay off games."""

import os

import pytest

from core import game_mode as gm
from core.events import event_bus, EventType


STEAM = gm._norm(r"D:\SteamLibrary\steamapps\common")


def test_classifies_steam_games_known_names_and_ignores_tools():
    dirs = [STEAM]
    assert gm.is_game_process("TheFinals.exe", r"D:\SteamLibrary\steamapps\common\THE FINALS\Discovery.exe", dirs)
    assert gm.is_game_process("RobloxPlayerBeta.exe", r"C:\Users\x\AppData\Local\Roblox\RobloxPlayerBeta.exe", [])
    assert gm.is_game_process("EasyAntiCheat_EOS.exe", "", [])
    assert gm.is_game_process("mygame.exe", r"C:\Elsewhere\mygame.exe", [], extra=["MyGame.exe"])
    # Always-on tools installed through Steam never hold Game Mode on.
    assert not gm.is_game_process("wallpaper64.exe", r"D:\SteamLibrary\steamapps\common\wallpaper_engine\wallpaper64.exe", dirs)
    assert not gm.is_game_process("winrtutil64.exe",
                                  r"D:\SteamLibrary\steamapps\common\wallpaper_engine\bin\winrtutil64.exe", dirs)
    assert not gm.is_game_process("notepad.exe", r"C:\Windows\notepad.exe", dirs)
    assert not gm.is_game_process("cs2.exe", "", [], ignore=["cs2.exe"])
    # Vanguard's always-running service is not a game.
    assert not gm.is_game_process("vgc.exe", r"C:\Program Files\Riot Vanguard\vgc.exe", dirs)


class _FakeProc:
    def __init__(self, table, pid):
        if pid not in table:
            raise ProcessLookupError(pid)
        self._n, self._e = table[pid]

    def name(self):
        return self._n

    def exe(self):
        return self._e


class _FakePsutil:
    def __init__(self):
        self.table = {}

    def pids(self):
        return list(self.table)

    def Process(self, pid):
        return _FakeProc(self.table, pid)


@pytest.fixture
def mode(monkeypatch):
    from core.config import config
    config.set("game_mode.enabled", True, persist=False)     # conftest turns detection off
    monkeypatch.setattr(gm.GameMode, "start", lambda self: None)
    m = gm.GameMode()
    monkeypatch.setattr(m, "_game_dirs", lambda: [STEAM])
    monkeypatch.setattr(gm, "_fullscreen_in_front", lambda: False)
    events = []
    handler = lambda ev: events.append(ev.payload) if ev.type == EventType.GAME_MODE else None
    event_bus.subscribe(handler)
    yield m, events
    event_bus.unsubscribe(handler)
    config.set("game_mode.enabled", False, persist=False)


def test_turns_on_while_the_game_runs_and_restores_after(mode):
    m, events = mode
    ps = _FakePsutil()
    ps.table = {10: ("explorer.exe", r"C:\Windows\explorer.exe")}
    m._scan(ps)
    assert not m.active and not m.overlays_blocked
    ps.table[20] = ("Discovery.exe", r"D:\SteamLibrary\steamapps\common\THE FINALS\Discovery.exe")
    m._scan(ps)
    assert m.active and m.overlays_blocked and m.game == "Discovery"
    assert m.refuse_capture()
    assert any(e["active"] and e["changed"] for e in events)
    del ps.table[20]
    m._scan(ps)
    assert not m.active and not m.overlays_blocked
    assert m.refuse_capture() is None
    assert events[-1]["active"] is False and events[-1]["changed"]


def test_launch_is_armed_before_the_process_exists(mode):
    m, _ = mode
    m.expect_launch("Counter-Strike 2")
    assert m.active and m.game == "Counter-Strike 2"
    m._armed_until = 0.0
    m._scan(_FakePsutil())
    assert not m.active


def test_manual_on_off(mode):
    m, _ = mode
    m.set_manual(True)
    assert m.active
    m.set_manual(False)
    assert not m.active
    # Switched off by hand while a game runs: stays off until that game exits.
    ps = _FakePsutil()
    ps.table = {20: ("cs2.exe", "")}
    m._scan(ps)
    assert m.active
    m.set_manual(False)
    m._scan(ps)
    assert not m.active
    del ps.table[20]
    m._scan(ps)
    ps.table[21] = ("cs2.exe", "")
    m._scan(ps)
    assert m.active                       # the next game is detected automatically again


def test_fullscreen_hides_overlays_without_full_game_mode(mode, monkeypatch):
    m, events = mode
    monkeypatch.setattr(gm, "_fullscreen_in_front", lambda: True)
    monkeypatch.setattr(gm, "_foreground_pid", lambda: 999)
    m._scan(_FakePsutil())
    assert m.overlays_blocked and not m.active
    assert events[-1]["overlays_blocked"] and not events[-1]["active"]


def test_input_is_refused_only_when_the_game_is_in_front(mode, monkeypatch):
    m, _ = mode
    ps = _FakePsutil()
    ps.table = {20: ("cs2.exe", "")}
    m._scan(ps)
    monkeypatch.setattr(gm, "_foreground_pid", lambda: 20)
    assert m.refuse_input()
    monkeypatch.setattr(gm, "_foreground_pid", lambda: 5)
    assert m.refuse_input() is None


def test_controller_refuses_clicks_into_a_game(monkeypatch):
    from modules.automation.tools import ToolError
    from modules.desktop import controller
    monkeypatch.setattr(gm.game_mode, "refuse_input", lambda: "cs2 is in front")
    monkeypatch.setattr(controller, "_require", lambda *a: None)
    with pytest.raises(ToolError) as e:
        controller.desktop.mouse_click(10, 10)
    assert e.value.code == "GAME_MODE"


def test_voice_game_mode_intent(monkeypatch):
    from modules.agent.saint_intents import parse_saint_ui
    calls = []
    monkeypatch.setattr(gm.game_mode, "set_manual", lambda on: calls.append(on))
    intent = parse_saint_ui("turn on game mode")
    assert intent and intent.name == "ui.game_mode"
    assert "Game Mode on" in intent.run().text and calls == [True]
    assert parse_saint_ui("game mode off").run().text == "Game Mode off." and calls[-1] is False
    monkeypatch.setattr(gm.game_mode, "active", False)
    assert parse_saint_ui("is game mode on").run().text == "Game Mode is off."


def test_detection_switched_off_suspends_nothing(mode):
    from core.config import config
    m, _ = mode
    ps = _FakePsutil()
    ps.table = {20: ("cs2.exe", "")}
    config.set("game_mode.enabled", False, persist=False)
    m._scan(ps)
    assert not m.active and not m.overlays_blocked

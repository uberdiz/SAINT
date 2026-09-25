"""Steam: reading Valve's files, matching spoken game names, and routing."""

import os

import pytest

from modules.steam import vdf
from modules.steam.library import SteamLibrary, libraries, read_manifest

LIBRARYFOLDERS = r'''
"libraryfolders"
{
	"0"
	{
		"path"		"%s"
		"label"		""
		"apps"
		{
			"730"		"74000000000"
		}
	}
}
'''


def _manifest(appid, name, size, installdir=None):
    return f'''"AppState"
{{
	"appid"		"{appid}"
	"name"		"{name}"
	"installdir"		"{installdir or name}"
	"SizeOnDisk"		"{size}"
	"LastPlayed"		"1786467995"
}}
'''


@pytest.fixture
def steam_dirs(tmp_path, monkeypatch):
    root = tmp_path / "Steam"
    extra = tmp_path / "SteamLibrary"
    (root / "steamapps").mkdir(parents=True)
    (extra / "steamapps").mkdir(parents=True)
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        LIBRARYFOLDERS % str(root).replace("\\", "\\\\"), encoding="utf-8")
    (root / "steamapps" / "appmanifest_730.acf").write_text(_manifest(730, "Counter-Strike 2", 74_000_000_000),
                                                            encoding="utf-8")
    (root / "steamapps" / "appmanifest_105600.acf").write_text(_manifest(105600, "Terraria", 803_964_902),
                                                               encoding="utf-8")
    (root / "steamapps" / "appmanifest_228980.acf").write_text(
        _manifest(228980, "Steamworks Common Redistributables", 1), encoding="utf-8")
    (extra / "steamapps" / "appmanifest_1172470.acf").write_text(_manifest(1172470, "Apex Legends", 94_000_000_000),
                                                                 encoding="utf-8")
    from modules.steam import library
    monkeypatch.setattr(library, "steam_path", lambda: str(root))
    from core.config import config
    monkeypatch.setitem(config._data, "steam", {"extra_libraries": [str(extra)]})
    return root, extra


def test_vdf_parses_nested_and_escapes():
    data = vdf.loads(LIBRARYFOLDERS % "C:\\\\Program Files (x86)\\\\Steam")
    assert data["libraryfolders"]["0"]["path"] == "C:\\Program Files (x86)\\Steam"
    assert vdf.get_ci(data["libraryfolders"]["0"], "LABEL") == ""
    assert vdf.loads('"A" { // comment\n "b" "c" }') == {"A": {"b": "c"}}


def test_libraries_flag_unregistered(steam_dirs):
    root, extra = steam_dirs
    libs = libraries()
    assert {"path": os.path.normpath(str(root)), "registered": True} in libs
    assert {"path": os.path.normpath(str(extra)), "registered": False} in libs


def test_games_skip_redistributables_and_know_their_library(steam_dirs):
    games = {g.name: g for g in SteamLibrary().games()}
    assert set(games) == {"Counter-Strike 2", "Terraria", "Apex Legends"}
    assert games["Apex Legends"].registered is False
    assert games["Terraria"].size == 803_964_902


@pytest.mark.parametrize("said,name", [
    ("counter strike 2", "Counter-Strike 2"), ("cs2", "Counter-Strike 2"), ("terraria", "Terraria"),
    ("apex", "Apex Legends"), ("Apex Legends", "Apex Legends"), ("how many dudes", None), ("steam", None),
])
def test_match_spoken_names(steam_dirs, said, name):
    g = SteamLibrary().match(said)
    assert (g.name if g else None) == name


def test_launch_refuses_unregistered_library(steam_dirs, monkeypatch):
    from modules.steam import tools, library
    monkeypatch.setattr(tools, "steam_library", SteamLibrary())
    opened = []
    monkeypatch.setattr(tools.os, "startfile", lambda uri: opened.append(uri))
    assert tools.launch("terraria") == {"launched": "Terraria", "appid": "105600"}
    assert opened == ["steam://rungameid/105600"]
    from modules.automation.tools import ToolError
    with pytest.raises(ToolError) as e:
        tools.launch("apex legends")
    assert e.value.code == "UNREGISTERED_LIBRARY" and "Settings > Storage" in str(e.value)


def test_read_manifest_handles_missing(tmp_path):
    assert read_manifest(str(tmp_path / "nope.acf"), str(tmp_path), True) is None


@pytest.fixture
def steam_route(steam_dirs, monkeypatch):
    from modules.steam import library
    fresh = SteamLibrary()
    monkeypatch.setattr(library, "steam_library", fresh)
    from modules.agent import steam_intents
    monkeypatch.setattr(steam_intents, "_music_context", lambda: False)   # background Spotify events can't leak in
    from modules.agent.context import desktop_context
    desktop_context.clear()
    yield
    desktop_context.clear()


@pytest.mark.parametrize("text,intent", [
    ("Open my Steam library and search how many dudes.", "composite:steam.open_library+steam.search"),
    ("open my steam library", "steam.open_library"),
    ("search steam for hades", "steam.store_search"),
    ("search hades on steam", "steam.store_search"),
    ("launch counter strike 2", "steam.launch"),
    ("start cs2", "steam.launch"),
    ("play terraria", "steam.launch"),
    ("what games do I have", "steam.list_games"),
    ("what are my biggest games", "steam.list_games"),
    ("how big is apex legends", "steam.game_info"),
    ("uninstall terraria", "steam.uninstall"),
])
def test_steam_routing(steam_route, text, intent):
    from modules.agent.router import route
    it = route(text)
    assert it is not None and it.name == intent, (text, it and it.name)


@pytest.mark.parametrize("text", ["open steam", "play some music", "play Drake", "search for cats"])
def test_steam_leaves_other_commands(steam_route, text):
    from modules.agent.router import route
    it = route(text)
    assert it is None or not it.name.startswith("steam"), (text, it and it.name)


def test_custom_uri_alias_is_a_uri(monkeypatch):
    from core.config import config
    from modules.desktop.apps import AppCatalog
    monkeypatch.setitem(config._data["desktop"], "apps", {"game library": "steam://nav/games",
                                                          "notes": "C:\\Tools\\Obsidian.exe"})
    cat = AppCatalog()
    e = cat.resolve("game library")
    assert e.kind == "uri" and e.process_hint == "steam"
    n = cat.resolve("notes")
    assert n.kind == "path" and n.process_hint == "obsidian"


def test_play_a_game_title_while_music_plays_stays_music(steam_route, monkeypatch):
    from modules.agent import steam_intents
    from modules.agent.router import route
    monkeypatch.setattr(steam_intents, "_music_context", lambda: True)
    assert not route("play terraria").name.startswith("steam")
    assert route("play terraria on steam").name == "steam.launch"

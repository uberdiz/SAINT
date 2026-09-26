"""Files & storage: scanning, junk rules, archives, safe recycling/moving and
the voice commands for them. Nothing here touches real user folders."""

import os
import re
import subprocess
import time
import zipfile
from pathlib import Path

import pytest

from modules.automation.tools import ToolError
from modules.files import archives, junk, ops, scan
from modules.files.paths import denied, protected, resolve_folder

ROOT = Path(__file__).resolve().parents[1]


def _write(p: Path, size: int = 10, data: bytes = None):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if data is not None else os.urandom(size))
    return p


# ---------------------------------------------------------------- never delete permanently
def test_files_package_never_deletes_permanently():
    banned = re.compile(r"\bos\.(remove|unlink|rmdir|removedirs)\(|shutil\.rmtree|\.unlink\(|send2trash|"
                        r"\bdel\s+/|\brd\s+/s|Remove-Item|FO_DELETE,\s*[^\n]*~FOF_ALLOWUNDO")
    for f in (ROOT / "modules" / "files").glob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert not banned.search(text), f"{f.name} must not delete files permanently"
    ops_src = (ROOT / "modules" / "files" / "ops.py").read_text(encoding="utf-8")
    assert "FOF_ALLOWUNDO | FOF_NOCONFIRMATION" in ops_src and "FOF_WANTNUKEWARNING" in ops_src


def test_destructive_tools_always_ask_and_are_hidden_from_the_llm(monkeypatch):
    from core.config import config
    from modules.automation.tools import get_tool_registry
    reg = get_tool_registry()
    for name in ("files.recycle", "files.move", "steam.uninstall"):
        tool = reg.get(name)
        assert tool is not None and not tool.llm_exposed, name
    monkeypatch.setitem(config._data, "permissions", {"overrides": {"files": "allow"}})
    called = []
    monkeypatch.setattr("modules.files.tools.ops.recycle", lambda paths: called.append(paths) or
                        {"recycled": [], "freed": 0, "refused": [], "failed": []})
    res = reg.execute("files.recycle", paths=["C:\\nothing"])
    assert res.error_code == "CONFIRM_REQUIRED" and called == []


# ---------------------------------------------------------------- paths
def test_denied_and_protected(tmp_path, monkeypatch):
    from core.config import config
    assert denied(os.environ.get("SystemRoot", r"C:\Windows") + r"\System32")
    assert denied("C:\\")
    assert denied(str(Path.home()))
    assert denied(str(ROOT / "core"))                     # SAINT itself
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    assert denied(str(repo))
    assert denied(str(tmp_path / "x" / "pagefile.sys"))
    assert denied(str(tmp_path / "file.txt")) is None
    games = tmp_path / "Games"
    games.mkdir()
    monkeypatch.setitem(config._data["files"], "games_dir", str(games))
    assert protected(str(games / "Elden Ring"))
    assert protected(str(tmp_path / "Emulation" / "RetroArch"))
    assert protected(str(tmp_path / "stuff" / "zelda.nsp"))
    assert protected(str(tmp_path / "stuff" / "notes.txt")) is None


def test_resolve_folder(tmp_path, monkeypatch):
    from core.config import config
    monkeypatch.setitem(config._data["files"], "games_dir", str(tmp_path))
    monkeypatch.setitem(config._data["files"], "known", {"emulators": str(tmp_path)})
    assert resolve_folder("my games folder") == str(tmp_path)
    assert resolve_folder("the emulators folder") == str(tmp_path)
    assert resolve_folder("C drive") == "C:\\"
    assert resolve_folder(str(tmp_path)) == str(tmp_path)
    assert resolve_folder("my imaginary folder") is None


# ---------------------------------------------------------------- scanning
def test_scan_tree_totals_and_skips_junctions(tmp_path):
    _write(tmp_path / "a" / "big.bin", 5000)
    _write(tmp_path / "a" / "deep" / "x" / "y.bin", 3000)
    _write(tmp_path / "b" / "small.bin", 100)
    link = tmp_path / "loop"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tmp_path / "a")],
                          capture_output=True).returncode == 0
    res = scan.scan_tree(str(tmp_path), depth=2)
    assert res["total"] == 8100 and res["files"] == 3          # the junction isn't counted twice
    sizes = {os.path.relpath(f["path"], tmp_path): f["size"] for f in res["folders"]}
    assert sizes["a"] == 8000 and sizes[os.path.join("a", "deep")] == 3000 and sizes["b"] == 100
    assert os.path.join("a", "deep", "x") not in sizes          # deeper than depth=2
    top = scan.top_folders(res, 3)
    assert top[0]["path"].endswith("a") or top[0]["path"].endswith("big.bin") is False
    if made:
        assert "loop" not in sizes


def test_scan_cancel(tmp_path):
    import threading
    _write(tmp_path / "f.bin", 10)
    ev = threading.Event()
    ev.set()
    assert scan.scan_tree(str(tmp_path), cancel=ev)["complete"] is False


def test_human():
    assert scan.human(0) == "0 bytes" and scan.human(1.5e9) == "1.5 GB" and scan.human(250e9) == "250 GB"


# ---------------------------------------------------------------- junk
def test_downloads_duplicates_extracted_and_installers(tmp_path, monkeypatch):
    from core.config import config
    monkeypatch.setitem(config._data["files"], "games_dir", str(tmp_path / "Games"))
    dl = tmp_path / "Downloads"
    data = os.urandom(3000)
    _write(dl / "setup.exe", data=data)
    _write(dl / "setup (1).exe", data=data)                  # identical copy
    _write(dl / "notes (1).txt", data=b"different")
    _write(dl / "notes.txt", data=b"original")
    # an archive that's already extracted into the games folder
    with zipfile.ZipFile(dl / "Cool Game.zip", "w") as z:
        z.writestr("Cool Game/game.exe", b"x" * 100)
        z.writestr("Cool Game/data/level1.dat", b"y" * 100)
    _write(tmp_path / "Games" / "Cool Game" / "game.exe", 100)
    _write(tmp_path / "Games" / "Cool Game" / "data" / "level1.dat", 100)
    old = _write(dl / "old_installer.msi", 50)
    t = time.time() - 90 * 86400
    os.utime(old, (t, t))
    found = {os.path.basename(f["path"]): f for f in junk.downloads_findings(str(dl))}
    assert found["setup (1).exe"]["category"] == "duplicate" and found["setup (1).exe"]["action"] == "recycle"
    assert "notes (1).txt" not in found
    assert found["Cool Game.zip"]["category"] == "extracted"
    assert found["old_installer.msi"]["action"] == "review"
    assert "setup.exe" not in found


def test_report_speech_and_plan(tmp_path, monkeypatch):
    dl = tmp_path / "Downloads"
    data = os.urandom(2000)
    _write(dl / "a.zip", data=data)
    _write(dl / "a (1).zip", data=data)
    rep = junk.report("downloads", downloads=str(dl))
    text = junk.summarize(rep)
    assert "duplicate downloads" in text and "Recycle Bin" in text and rep["recyclable"] == 2000


# ---------------------------------------------------------------- archives
def test_newest_archive_skips_partial_and_later_parts(tmp_path):
    a = _write(tmp_path / "old.zip", 10)
    b = _write(tmp_path / "game.part1.rar", 10)
    _write(tmp_path / "game.part2.rar", 10)
    _write(tmp_path / "loading.zip.crdownload", 10)
    past = time.time() - 1000
    os.utime(a, (past, past))
    assert archives.newest_archive(str(tmp_path)) == str(b)
    assert archives.find_archive(str(tmp_path), "the old zip") == str(a)


UNRAR_LISTING = """
Archive: game.rar
Details: RAR 5

 Attributes       Size     Date    Time   Name
----------- ----------  ---------- -----  ----
    ..A....    3000000  2026-09-24 23:15  Game Folder\\data.bin
    ..A....          3  2026-09-24 23:15  Game Folder\\sub\\readme.txt
    ...D...          0  2026-09-24 23:15  Game Folder\\sub
    ...D...          0  2026-09-24 23:15  Game Folder
----------- ----------  ---------- -----  ----
               3000003                    4
"""


def test_parse_unrar_listing_and_destination(tmp_path):
    rows = archives.parse_unrar_listing(UNRAR_LISTING)
    assert len(rows) == 4 and rows[0] == {"name": "Game Folder\\data.bin", "size": 3000000, "dir": False}
    assert archives.destination_for("x\\game.rar", "D:\\Games", ["Game Folder"], rows) == "D:\\Games"
    loose = [{"name": "setup.exe", "size": 1, "dir": False}, {"name": "readme.txt", "size": 1, "dir": False}]
    assert archives.destination_for("x\\Tool v2.part1.rar", "D:\\Games", ["readme.txt", "setup.exe"], loose) == \
        os.path.join("D:\\Games", "Tool v2")


def test_zip_extract_and_zip_slip(tmp_path, monkeypatch):
    monkeypatch.setattr(archives, "find_winrar", lambda: {"winrar": None, "unrar": None, "rar": None})
    good = tmp_path / "pack.zip"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("a.txt", "hello")
        z.writestr("b/c.txt", "world")
    dest = tmp_path / "out"
    dest.mkdir()
    res = archives.extract(str(good), str(dest))
    assert (dest / "pack" / "a.txt").read_text() == "hello" and (dest / "pack" / "b" / "c.txt").exists()
    assert "Extracted pack.zip" in res["summary"]
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../../escape.txt", "nope")
    with pytest.raises(ToolError) as e:
        archives.extract(str(evil), str(dest))
    assert e.value.code == "UNSAFE" and not (tmp_path / "escape.txt").exists()


def test_extract_checks_free_space(tmp_path, monkeypatch):
    z = tmp_path / "big.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("x.bin", b"0" * 1000)
    monkeypatch.setattr(archives.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    with pytest.raises(ToolError) as e:
        archives.extract(str(z), str(tmp_path))
    assert e.value.code == "NO_SPACE"


# ---------------------------------------------------------------- recycle / move
@pytest.fixture
def fake_shell(monkeypatch, tmp_path):
    """Stand-in for Windows' file operation: 'recycling' moves into a fake bin."""
    import shutil
    bin_dir = tmp_path / "_bin"
    bin_dir.mkdir()
    calls = []

    def op(func, sources, dest, flags):
        calls.append((func, list(sources), dest, flags))
        for s in sources:
            if func == ops.FO_DELETE:
                assert flags & ops.FOF_ALLOWUNDO
                shutil.move(s, bin_dir / os.path.basename(s))
            elif func == ops.FO_MOVE:
                shutil.move(s, os.path.join(dest, os.path.basename(s)))
        return 0, False
    monkeypatch.setattr(ops, "_shfileop", op)
    monkeypatch.setattr(ops, "bin_settings", lambda p: {"nuke": False, "max_bytes": 10_000})
    return calls


def test_recycle_refuses_what_it_cannot_undo(tmp_path, monkeypatch, fake_shell):
    small = _write(tmp_path / "small.bin", 100)
    big = _write(tmp_path / "huge.bin", 20_000)             # bigger than the fake bin
    rom = _write(tmp_path / "mario.nes", 10)
    res = ops.recycle([str(small), str(big), str(rom), str(ROOT / "core")])
    assert [os.path.basename(r["path"]) for r in res["recycled"]] == ["small.bin"]
    reasons = {os.path.basename(r["path"]): r["reason"] for r in res["refused"]}
    assert "bigger than" in reasons["huge.bin"] and "ROM" in reasons["mario.nes"] and "SAINT" in reasons["core"]
    assert big.exists() and rom.exists() and not small.exists()
    monkeypatch.setattr(ops, "bin_settings", lambda p: {"nuke": True, "max_bytes": 10_000})
    again = _write(tmp_path / "again.bin", 10)
    res = ops.recycle([str(again)])
    assert not res["recycled"] and "delete files immediately" in res["refused"][0]["reason"] and again.exists()


def test_move_rules(tmp_path, monkeypatch, fake_shell):
    src = _write(tmp_path / "Clips" / "clip.mp4", 100).parent
    dest = tmp_path / "D"
    dest.mkdir()
    res = ops.move(str(src), str(dest))
    assert (dest / "Clips" / "clip.mp4").exists() and res["size"] == 100
    other = _write(tmp_path / "Other" / "x.bin", 10).parent
    _write(dest / "Other" / "y.bin", 10)
    with pytest.raises(ToolError) as e:
        ops.move(str(other), str(dest))
    assert e.value.code == "EXISTS"
    monkeypatch.setattr("modules.files.paths.steam_library_roots", lambda: [str(tmp_path / "SteamLibrary")])
    game = _write(tmp_path / "SteamLibrary" / "steamapps" / "common" / "Game" / "g.exe", 10).parent
    with pytest.raises(ToolError) as e:
        ops.move(str(game), str(dest))
    assert e.value.code == "USE_STEAM"


# ---------------------------------------------------------------- voice commands
@pytest.fixture
def captured(monkeypatch):
    import modules.agent.files_intents as fi
    calls = []

    class R:
        success, error = True, None
        result = {"summary": "ok"}
    monkeypatch.setattr(fi, "call", lambda tool, **kw: calls.append((tool, kw)) or R())
    return calls


def test_the_logged_winrar_request_is_one_extraction(captured, monkeypatch, tmp_path):
    from core.config import config
    from modules.agent.router import route
    monkeypatch.setitem(config._data["files"], "games_dir", str(tmp_path))
    it = route("Go to my Downloads folder, click the first download, and extract it using WinRAR "
               "to my games folder.")
    assert it.name == "files.extract"
    it.run()
    assert captured[-1] == ("files.extract", {"archive": "latest", "delete_after": False, "source": "downloads",
                                              "destination": "games"})
    route("unzip the latest download and delete it afterwards").run()
    assert captured[-1][1]["delete_after"] is True and captured[-1][1]["archive"] == "latest"
    route("extract it here").run()
    assert captured[-1][1]["archive"] == "latest" and "destination" not in captured[-1][1]


@pytest.mark.parametrize("text,intent", [
    ("how much space is left on E", "files.drive_overview"),
    ("how full are my drives", "files.drive_overview"),
    ("what's taking up space on D", "files.biggest"),
    ("what are the biggest folders on C", "files.biggest"),
    ("clean up my downloads", "files.cleanup_plan"),
    ("clean up my E drive", "files.cleanup_plan"),
    ("what can I delete", "files.cleanup_plan"),
    ("find my emulators", "files.find"),
    ("where are my roms", "files.find"),
    ("open my downloads", "files.open_folder"),
    ("go to my downloads folder", "files.open_folder"),
    ("open the C drive", "files.open_folder"),
    ("empty the recycle bin", "files.recycle_bin"),
    ("open disk cleanup", "files.disk_cleanup"),
])
def test_file_phrases(text, intent):
    from modules.agent.router import route
    it = route(text)
    assert it is not None and it.name == intent, (text, it and it.name)


@pytest.mark.parametrize("text", ["move spotify to my second monitor", "delete the reminder", "open settings",
                                  "open steam", "extract the key points from this page"])
def test_file_parsers_leave_other_commands(text):
    from modules.agent.router import route
    it = route(text)
    assert it is None or not it.name.startswith("files."), \
        (text, it and it.name)


def test_empty_recycle_bin_is_refused(monkeypatch):
    from modules.agent.router import route
    monkeypatch.setattr(os, "startfile", lambda p: None, raising=False)
    r = route("empty the recycle bin").run()
    assert "never delete" in r.text


def test_cleanup_subset_after_a_plan(monkeypatch, tmp_path):
    from modules.agent.confirm import confirmations
    from modules.agent.router import route
    from modules.files import tools as ft
    (tmp_path / "a (1).zip").write_bytes(b"x" * 10)
    (tmp_path / "setup.exe").write_bytes(b"x" * 20)
    ft._remember_plan({"findings": [
        {"category": "duplicate", "path": str(tmp_path / "a (1).zip"), "size": 10, "action": "recycle", "id": 1},
        {"category": "old_installer", "path": str(tmp_path / "setup.exe"), "size": 20, "action": "review", "id": 2}],
        "recyclable": 10, "total": 30, "scope": "downloads"})
    try:
        r = route("only the installers").run()
        assert "1 item (20 bytes)" in r.text and r.expects_reply
        assert confirmations.pending is not None and confirmations.pending.tool == "files.recycle"
    finally:
        confirmations.clear()
        ft._last_plan.clear()

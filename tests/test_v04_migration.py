"""v0.4 data migration (core/migration.py): backed up first, idempotent, safe if interrupted,
never deletes anything, older versions can still read the folder."""

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from core import migration


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def v03(tmp_path):
    """A SAINT 2.2.x / v0.3 data folder like %LOCALAPPDATA%\\SAINT."""
    root = tmp_path / "SAINT"
    (root / "memory").mkdir(parents=True)
    (root / "link").mkdir()
    (root / "models" / "hf").mkdir(parents=True)
    (root / "tts" / "kokoro-onnx").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "config.json").write_text(json.dumps({
        "config_version": 7, "ai": {"model": "llama3.1:8b"}, "appearance": {"accent": "#16a34a", "theme": "Light"},
        "voice": {"mic_device": "Voicemeeter Out B1", "wake_word_threshold": 0.31},
        "spotify": {"client_id": "abc123"}, "automation": {"permission_mode": "autonomous"}}))
    db = sqlite3.connect(root / "memory" / "saint_memory.db")
    db.execute("create table memory (k text, v text)")
    db.executemany("insert into memory values (?, ?)", [("sister", "Mia"), ("gym playlist", "moe")])
    db.commit()
    db.close()
    (root / "memory" / "spotify_memory.db").write_bytes(b"SQLite format 3\x00" + b"\x01" * 4096)
    (root / "skills.json").write_text(json.dumps([
        {"phrase": "minimize all my windows", "steps": ["show the desktop"], "how": "planned", "id": "s1",
         "created": 1.0, "uses": 4},
        {"phrase": "gaming time", "steps": ["open steam", "open discord", "pause the music"], "how": "edited",
         "id": "s2", "created": 2.0, "uses": 7},
        {"phrase": "write an email", "steps": ["open browser", "ask who it's to", "click send"], "how": "lesson",
         "id": "s3", "created": 3.0}]))
    (root / "tasks.json").write_text(json.dumps({"tasks": [
        {"id": "t1", "title": "Open spotify and discord", "request": "open spotify and discord", "status": "done",
         "started": time.time() - 600, "updated": time.time() - 590,
         "steps": [{"label": "opening an app", "text": "open spotify", "status": "done"},
                   {"label": "opening an app", "text": "open discord", "status": "done"}]},
        {"id": "t2", "title": "Set up gaming", "request": "set up my gaming workspace", "status": "stopped",
         "started": time.time() - 300, "updated": time.time() - 290,
         "steps": [{"label": "moving", "text": "move saint to my second monitor", "status": "pending"}]}]}))
    (root / "history.jsonl").write_text('{"t": 1, "text": "play my gym playlist"}\n' * 50)
    (root / "scenes.json").write_text(json.dumps([{"name": "Gaming mode", "steps": ["pause the music"]}]))
    (root / "link" / "identity.json").write_text(json.dumps({"device_id": "pc-1", "name": "HOME-PC1"}))
    (root / "link" / "peers.json").write_text(json.dumps([{"id": "iphone", "name": "iPhone", "public_key": "ab"}]))
    (root / "models" / "hf" / "big.bin").write_bytes(b"\x00" * 100_000)
    (root / "saint.lock").write_text("123")
    return root


def test_migration_backs_up_and_keeps_everything(v03):
    before = {p.relative_to(v03): _sha(p) for p in migration.user_files(v03)}
    report = migration.run(v03)
    assert not report["error"]
    backup = Path(report["backup"])
    assert backup.is_dir() and backup.parent.name == "SAINT-backups"
    for rel, digest in before.items():                     # every user file is in the backup, intact
        assert _sha(backup / rel) == digest
    assert not (backup / "models").exists() and not (backup / "saint.lock").exists()
    for rel, digest in before.items():                     # and nothing the user had was changed or removed
        if rel.name not in ("migrations.json",):
            assert _sha(v03 / rel) == digest, rel
    applied = [m for m, _r in report["applied"]]
    assert applied == [m for m, _f in migration.STEPS]


def test_settings_survive_the_config_upgrade(v03):
    migration.run(v03)
    from core.config import Config
    cfg = Config(v03 / "config.json")
    assert cfg.get("config_version") == 8
    assert cfg.get("appearance.accent") == "#16a34a" and cfg.get("appearance.theme") == "Light"
    assert cfg.get("voice.mic_device") == "Voicemeeter Out B1" and cfg.get("voice.wake_word_threshold") == 0.31
    assert cfg.get("spotify.client_id") == "abc123" and cfg.get("automation.permission_mode") == "autonomous"
    assert cfg.get("layout.status_bar") is True                       # new v0.4 defaults added


def test_old_tasks_and_skills_carry_over(v03):
    migration.run(v03)
    tasks = json.loads((v03 / "agent_tasks.json").read_text())["tasks"]
    assert {t["id"] for t in tasks} == {"old-t1", "old-t2"}
    from modules.agent.autonomy.model import AgentTask
    t2 = AgentTask.from_dict(next(t for t in tasks if t["id"] == "old-t2"))
    assert t2.status == "paused" and t2.plan[0].action == "move saint to my second monitor"
    procs = json.loads((v03 / "procedures.json").read_text())["procedures"]
    assert set(procs) == {"s2"}                                       # 3+ steps; lessons and 1-step skills stay as they are
    assert [s["action"] for s in procs["s2"]["steps"]] == ["open steam", "open discord", "pause the music"]
    assert procs["s2"]["uses"] == 7
    # skills.json is untouched: an older SAINT still runs these.
    assert json.loads((v03 / "skills.json").read_text())[1]["steps"][0] == "open steam"


def test_running_twice_changes_nothing_more(v03):
    migration.run(v03)
    snapshot = {p.relative_to(v03): _sha(p) for p in migration.user_files(v03)}
    report = migration.run(v03)
    assert report["applied"] == [] and report["backup"] == ""
    assert {p.relative_to(v03): _sha(p) for p in migration.user_files(v03)} == snapshot
    assert len(list((v03.parent / "SAINT-backups").iterdir())) == 1


def test_an_interrupted_migration_resumes(v03, monkeypatch):
    calls = []
    original = migration.STEPS[3]

    def crash(root):
        calls.append(root)
        raise RuntimeError("power cut")
    monkeypatch.setattr(migration, "STEPS", migration.STEPS[:3] + [(original[0], crash)] + migration.STEPS[4:])
    report = migration.run(v03)
    assert report["error"] == "power cut"
    state = json.loads((v03 / "migrations.json").read_text())
    assert len(state["applied"]) == 3                               # what finished is recorded
    monkeypatch.undo()
    report = migration.run(v03)
    assert [m for m, _r in report["applied"]] == [m for m, _f in migration.STEPS[3:]]
    tasks = json.loads((v03 / "agent_tasks.json").read_text())["tasks"]
    assert len(tasks) == 2                                          # no duplicates from the first attempt


def test_a_failed_backup_migrates_nothing(v03, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    report = migration.run(v03)
    assert "disk full" in report["error"] and report["applied"] == []
    assert not (v03 / "procedures.json").exists() and not (v03 / "migrations.json").exists()


def test_legacy_source_data_is_copied_into_a_new_folder(tmp_path, monkeypatch, v03):
    fresh = tmp_path / "new" / "SAINT"
    fresh.mkdir(parents=True)
    monkeypatch.setenv("SAINT_LEGACY_DATA", str(v03))
    report = migration.run(fresh)
    assert not report["error"]
    assert (fresh / "memory" / "saint_memory.db").is_file() and (fresh / "skills.json").is_file()
    assert not (fresh / "models").exists()                          # models aren't user data
    assert (v03 / "config.json").is_file()                           # the source is left in place


def test_a_brand_new_install_needs_no_backup(tmp_path):
    root = tmp_path / "empty" / "SAINT"
    root.mkdir(parents=True)
    report = migration.run(root)
    assert report["backup"] == "" and not report["error"]
    assert json.loads((root / "version.json").read_text())["last_version"]

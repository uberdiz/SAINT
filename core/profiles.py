"""
core/profiles.py

Test profiles: run SAINT on a separate data folder, so testing never touches —
or shows — your real memory, history, scenes, settings, Spotify login or paired
devices (core/paths.py keeps each profile's secrets apart too).

    run.bat --profile clean            a brand-new SAINT: first-run setup, nothing learned
    run.bat --profile demo             sample scenes, answers and settings (screenshots, demos)
    python tools/profiles.py snapshot  a copy of your real data to test against without risking it

Profiles live in %LOCALAPPDATA%/SAINT-profiles/<name>: outside the project (never
committed) and outside the installer (never shipped). Your real data stays where
it is (%LOCALAPPDATA%/SAINT) and is what a build-time check keeps out of releases
(packaging/windows/build.py).
"""

import json
import shutil
import time
from pathlib import Path
from typing import Dict, List

from core import paths

# Never copied into a snapshot: big shared models, caches, logs, the running instance's lock.
_SKIP = {"models", "tts", "cache", "logs", "screens", "saint.lock", "SAINT-backups"}

# Files that only ever hold the user's own data. A release must contain none of them.
USER_DATA_FILES = ("config.json", "history.jsonl", "history_daily.json", "analytics.json", "skills.json",
                   "scenes.json", "tasks.json", "lesson_answers.json", "vocabulary.json", "voice_profile.json",
                   "learning_journal.jsonl", "storage_cache.json", "identity.json", "peers.json", ".setup_done")
USER_DATA_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm")

_DEMO_SCENES = [
    {"name": "Focus mode", "steps": ["play something chill", "set volume to 30", "silent mode for 1 hour"],
     "phrase": "help me focus", "schedule": "", "automation_id": "", "id": "demo0001", "last_run": 0.0},
    {"name": "write me an email", "id": "demo0002", "phrase": "", "schedule": "", "automation_id": "",
     "last_run": 0.0, "steps": [
         "open browser", "make a new tab", 'go to "https://mail.google.com/mail/u/0/#inbox"',
         "ask which email to send from, personal or work",
         "if need change click on the profile and click the other email (alex.demo@gmail.com or alex@example.com)",
         "Click Compose", "ask for email if not saved", "ask what to write about",
         "make title based on desc and create contents of the email and ask to send or edit",
         "Click send if yes."]},
    {"name": "Dev environment", "steps": ["open visual studio code", "open terminal", "open my browser"],
     "phrase": "get my dev environment ready", "schedule": "", "automation_id": "", "id": "demo0003",
     "last_run": 0.0},
]
_DEMO_ANSWERS = {"what's sam's email address?": "sam@example.com",
                 "what's jordan's email address?": "jordan@example.com"}


def path(name: str) -> Path:
    return paths.profiles_root() / _safe(name)


def _safe(name: str) -> str:
    import re
    safe = re.sub(r"[^\w.-]", "-", (name or "").strip())[:40]
    if not safe:
        raise ValueError("Give the profile a name.")
    return safe


def exists(name: str) -> bool:
    return path(name).is_dir()


def _write(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1), encoding="utf-8")


def _seed_demo(dest: Path):
    _write(dest / "scenes.json", _DEMO_SCENES)
    _write(dest / "lesson_answers.json", _DEMO_ANSWERS)
    # Starter scenes are already in scenes.json; the first-run check isn't repeated.
    _write(dest / "config.json", {"config_version": 2, "scenes": {"seeded": ["gaming mode", "done gaming",
                                                                             "dev environment"]}})
    (dest / ".setup_done").write_text("1", encoding="utf-8")


def create(name: str, source: str = "empty", overwrite: bool = False) -> Path:
    """source: "empty" (a fresh install), "demo" (sample data), "real" (a copy of your data),
    or the name of another profile to copy."""
    dest = path(name)
    if dest.exists():
        if not overwrite:
            raise FileExistsError(f"There's already a profile called {dest.name}.")
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if source == "demo":
        _seed_demo(dest)
    elif source != "empty":
        src = paths.base_data_dir() if source == "real" else path(source)
        if not src.is_dir():
            raise FileNotFoundError(f"There's no profile called {source}.")
        for item in src.iterdir():
            if item.name in _SKIP:
                continue
            if item.is_dir():
                shutil.copytree(item, dest / item.name)
            else:
                shutil.copy2(item, dest / item.name)
    _write(dest / "profile.json", {"name": dest.name, "source": source, "created": time.time()})
    return dest


def ensure(name: str) -> Path:
    """The profile's folder, made on first use: "demo" starts with sample data, any other name empty."""
    if not exists(name):
        create(name, "demo" if _safe(name) == "demo" else "empty")
    return path(name)


def delete(name: str):
    p = path(name)
    if p.is_dir():
        shutil.rmtree(p)


def list_profiles() -> List[Dict]:
    root = paths.profiles_root()
    out = []
    for p in sorted(root.iterdir()) if root.is_dir() else []:
        if not p.is_dir():
            continue
        try:
            meta = json.loads((p / "profile.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        out.append({"name": p.name, "source": meta.get("source", "?"), "created": meta.get("created", 0),
                    "size_mb": round(size / 1e6, 1), "path": str(p)})
    return out


def user_data_in(folder: Path) -> List[str]:
    """Files in ``folder`` that only the user's own data folder should have (a release must have none)."""
    hits = []
    for f in Path(folder).rglob("*"):
        if f.is_file() and (f.name in USER_DATA_FILES or f.name.lower().endswith(USER_DATA_SUFFIXES)):
            hits.append(str(f.relative_to(folder)))
    return hits

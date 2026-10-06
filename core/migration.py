"""
core/migration.py

Bringing a user's SAINT data forward to this version — run once at startup, before the settings
are loaded (they upgrade themselves in place, so the backup has to come first).

    1. Find what's pending in ``migrations.json`` (the record of what already ran).
    2. Back up every user file (settings, memory databases, history, skills, scenes, tasks, Link
       identity and peers…) to %LOCALAPPDATA%\\SAINT-backups\\before-<version>-<time>\\, and check
       the copy. If the backup fails, nothing is migrated (SAINT runs on the data as it is).
    3. Run each pending step. Every step is idempotent (running it twice changes nothing more),
       writes its files atomically (temp file + rename), and is recorded as soon as it finishes —
       an interrupted migration simply carries on next start.

Nothing is ever deleted: older versions keep working on the same folder (new files are ones they
ignore; config.json keeps every key they know).

Steps:
    import_legacy_data   a new data folder, older SAINT data in a source checkout's data/ folder:
                         copy it in (the source folder is left as it was)
    config_v8            the settings upgrade (core/config.py does it on load; recorded here)
    task_history         old tasks (tasks.json) appear in the v0.4 Tasks page (agent_tasks.json)
    skill_procedures     learned skills with several steps become procedures (checks, versions)
    version_marker       version.json: which SAINT last ran on this data
"""

import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger("saint.migration")

STATE_FILE = "migrations.json"
_SKIP_DIRS = {"models", "tts", "cache", "logs", "screens"}            # big / regenerable: not backed up
_SKIP_FILES = {"saint.lock"}


def _version() -> str:
    from core.version import VERSION
    return VERSION


def _atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".migrating")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------------- #
# Backup
# ---------------------------------------------------------------------- #
def user_files(root: Path) -> List[Path]:
    """Every file that is the user's data (models, caches, logs and the lock left out)."""
    out = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, files in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if rel.parts and rel.parts[0] in _SKIP_DIRS:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not (not rel.parts and d in _SKIP_DIRS)]
        for f in files:
            if f in _SKIP_FILES or f.endswith((".migrating", ".tmp")):
                continue
            out.append(Path(dirpath) / f)
    return out


def backup(root: Path, label: str) -> Path:
    """Copy the user's files to a dated folder next to ``root`` and check the copy."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = root.parent / f"{root.name}-backups" / f"before-{label}-{stamp}"
    files = user_files(root)
    for src in files:
        target = dest / src.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    missing = [str(s.relative_to(root)) for s in files
               if not (dest / s.relative_to(root)).is_file()
               or (dest / s.relative_to(root)).stat().st_size != s.stat().st_size]
    if missing:
        raise OSError(f"backup incomplete: {missing[:5]}")
    (dest / "BACKUP.txt").write_text(
        f"SAINT data from {root}, copied {time.ctime()} before upgrading to {label}.\n"
        f"{len(files)} files. To go back: quit SAINT and copy these files over the folder above.\n",
        encoding="utf-8")
    return dest


# ---------------------------------------------------------------------- #
# Steps
# ---------------------------------------------------------------------- #
def _legacy_sources(root: Path) -> List[Path]:
    """Older SAINT data folders: a source checkout's data/ (this one and, when frozen, none)."""
    from core import paths
    cands = []
    extra = os.environ.get("SAINT_LEGACY_DATA", "").strip()
    if extra:
        cands.append(Path(extra))
    if not paths.FROZEN:
        cands.append(paths.PROJECT_ROOT / "data")

    def substantial(c: Path) -> bool:
        # Settings alone (a default config.json written by a quick run) isn't a user's SAINT.
        return any((c / n).exists() for n in ("memory", "skills.json", "history.jsonl", "scenes.json"))
    return [c for c in cands if c.is_dir() and c.resolve() != root.resolve() and substantial(c)]


def step_import_legacy_data(root: Path) -> str:
    from core import paths
    if paths.has_user_data(root):
        return "the data folder already has SAINT data — nothing to import"
    for src in _legacy_sources(root):
        n = 0
        for f in user_files(src):
            target = root / f.relative_to(src)
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            n += 1
        return f"copied {n} files from {src} (left in place)"
    return "no older data found"


def step_config_v8(root: Path) -> str:
    cfg = _read_json(root / "config.json")
    if not isinstance(cfg, dict):
        return "no settings yet"
    return f"settings version {cfg.get('config_version', 1)} — upgraded when loaded, every value kept"


def step_task_history(root: Path) -> str:
    """Old multi-step tasks (task_memory, tasks.json) shown in the Tasks page. tasks.json stays."""
    old = _read_json(root / "tasks.json", {}) or {}
    tasks = [t for t in old.get("tasks", []) if isinstance(t, dict)]
    if not tasks:
        return "no older tasks"
    path = root / "agent_tasks.json"
    current = _read_json(path, {"version": 1, "tasks": []}) or {"version": 1, "tasks": []}
    have = {t.get("id") for t in current.get("tasks", [])}
    status_map = {"done": "completed", "failed": "failed", "stopped": "paused", "running": "paused",
                  "waiting": "paused"}
    step_map = {"done": "done", "failed": "failed", "pending": "pending", "running": "pending",
                "skipped": "skipped"}
    added = 0
    for t in tasks:
        tid = f"old-{t.get('id', '')}"
        if not t.get("id") or tid in have:
            continue
        started = float(t.get("started") or t.get("updated") or time.time())
        current.setdefault("tasks", []).append({
            "request": t.get("request", ""), "goal": t.get("title", "") or t.get("request", ""), "id": tid,
            "status": status_map.get(t.get("status", ""), "completed"), "source": "composite",
            "context": {"imported": True}, "result": t.get("note", ""),
            "plan": [{"action": s.get("text", ""), "label": (s.get("label") or s.get("text") or "")[:1].upper()
                      + (s.get("label") or s.get("text") or "")[1:], "status": step_map.get(s.get("status"), "pending"),
                      "note": s.get("note", "")} for s in t.get("steps", []) if isinstance(s, dict)],
            "trail": [{"at": started, "kind": "NOTE", "text": "From an earlier version of SAINT"}],
            "created": started, "updated": float(t.get("updated") or started), "background": False,
            "announce": False})
        added += 1
    current["tasks"] = sorted(current.get("tasks", []), key=lambda d: d.get("updated", 0))[-30:]
    _atomic_json(path, current)
    return f"{added} older task(s) added to the Tasks page"


def step_skill_procedures(root: Path) -> str:
    """Skills with three or more steps get procedure records (labels, inferred checks, version 1),
    so they run as observed, verified tasks. skills.json itself is not changed."""
    skills = _read_json(root / "skills.json", []) or []
    path = root / "procedures.json"
    procs = _read_json(path, {"version": 1, "procedures": {}}) or {"version": 1, "procedures": {}}
    have = procs.setdefault("procedures", {})
    added = 0
    for s in skills if isinstance(skills, list) else []:
        if not isinstance(s, dict) or not s.get("id") or s["id"] in have:
            continue
        steps = [x for x in (s.get("steps") or []) if isinstance(x, str) and x.strip()]
        if len(steps) < 3 or s.get("how") == "lesson" or any(x.lower().startswith(("ask ", "if ")) for x in steps):
            continue                  # short skills stay plain skills; taught lessons keep their own runner
        try:
            from modules.agent.autonomy.goals import label
            from modules.agent.autonomy.verify import infer
        except Exception:
            def label(c):
                return c[:1].upper() + c[1:]

            def infer(_c):
                return {"kind": "reply"}
        have[s["id"]] = {"id": s["id"], "phrase": s.get("phrase", ""), "goal": s.get("said") or s.get("phrase", ""),
                         "steps": [{"action": x, "label": label(x), "verify": infer(x), "fallback": [], "risk": "low",
                                    "optional": False, "tool": "", "args": {}} for x in steps],
                         "version": 1, "uses": int(s.get("uses", 0) or 0), "fails": 0,
                         "created": float(s.get("created") or time.time()), "updated": time.time(),
                         "source": s.get("how", "planned"),
                         "history": [{"version": 1, "at": time.time(), "change": "brought over from an earlier version"}]}
        added += 1
    if added:
        _atomic_json(path, procs)
    return f"{added} learned skill(s) now run as procedures"


def step_version_marker(root: Path) -> str:
    path = root / "version.json"
    data = _read_json(path, {}) or {}
    data.setdefault("first_run", {}).setdefault(_version(), time.time())
    data["last_version"] = _version()
    _atomic_json(path, data)
    return f"SAINT {_version()}"


STEPS: List[Tuple[str, Callable[[Path], str]]] = [
    ("0.4.0/import_legacy_data", step_import_legacy_data),
    ("0.4.0/config_v8", step_config_v8),
    ("0.4.0/task_history", step_task_history),
    ("0.4.0/skill_procedures", step_skill_procedures),
    ("0.4.0/version_marker", step_version_marker),
]


# ---------------------------------------------------------------------- #
def pending(root: Path) -> List[str]:
    state = _read_json(root / STATE_FILE, {}) or {}
    done = state.get("applied", {})
    return [mid for mid, _fn in STEPS if mid not in done]


def run(root: Optional[Path] = None) -> Dict:
    """Bring the data folder up to date. Never raises: SAINT always starts."""
    from core import paths
    root = Path(root) if root is not None else paths.data_dir()
    report = {"root": str(root), "applied": [], "backup": "", "error": ""}
    try:
        todo = pending(root)
        if not todo:
            return report
        state_path = root / STATE_FILE
        state = _read_json(state_path, {}) or {}
        state.setdefault("applied", {})
        if paths.has_user_data(root) or _legacy_sources(root):
            if user_files(root):
                dest = backup(root, f"v{_version()}")
                report["backup"] = str(dest)
                state.setdefault("backups", []).append({"at": time.time(), "path": str(dest), "for": _version()})
                log.info("migration.backup %s", dest)
        for mid, fn in STEPS:
            if mid not in todo:
                continue
            result = fn(root)
            state["applied"][mid] = {"at": time.time(), "result": result}
            state["version"] = _version()
            _atomic_json(state_path, state)            # recorded as it goes: an interruption resumes here
            report["applied"].append((mid, result))
            log.info("migration.step %s: %s", mid, result)
    except Exception as e:                                 # a failed migration never stops SAINT
        report["error"] = str(e)
        log.exception("migration.failed")
    return report


def main(argv=None) -> int:
    """python -m core.migration [folder]  — run (or just report) the migration on a folder."""
    args = list(argv if argv is not None else sys.argv[1:])
    dry = "--check" in args
    args = [a for a in args if a != "--check"]
    root = Path(args[0]) if args else None
    if dry:
        from core import paths
        print(json.dumps({"root": str(root or paths.data_dir()), "pending": pending(root or paths.data_dir())},
                         indent=1))
        return 0
    print(json.dumps(run(root), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

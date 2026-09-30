"""
core/paths.py

Project-relative path resolution.

Every runtime file SAINT reads or writes (config, logs, memory databases,
automations, the wake-word model) is resolved against the project root rather
than the current working directory, so SAINT behaves the same no matter where
it is launched from and never depends on a machine-specific absolute path.

Packaged as SAINT.exe (PyInstaller, ``sys.frozen``) the two kinds of files
are split: read-only assets shipped with the app (the logo, the wake-word
models) come from the bundle (``resource_path``), and everything SAINT writes
goes to %LOCALAPPDATA%/SAINT — Program Files isn't writable. Run from source,
both are the project folder, exactly as before.

Environment overrides:
    SAINT_DATA_DIR   - store runtime data somewhere other than <project>/data
                       (the test-suite uses this to isolate itself).
"""

import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
# Where SAINT is installed (the folder with SAINT.exe, or the repository).
PROJECT_ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
# Read-only files shipped with SAINT (PyInstaller unpacks them to _MEIPASS).
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT)) if FROZEN else PROJECT_ROOT


def data_dir() -> Path:
    override = os.environ.get("SAINT_DATA_DIR", "").strip()
    if override:
        path = Path(override)
    elif FROZEN:
        path = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "SAINT"
    else:
        path = PROJECT_ROOT / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resource_path(*parts: str) -> Path:
    """A read-only file shipped with SAINT (``resource_path("SAINT.png")``)."""
    return RESOURCE_ROOT.joinpath(*parts)


def data_path(*parts: str) -> Path:
    """Path inside the runtime data directory (parent dirs are created)."""
    path = data_dir().joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def resolve_project_path(value: str) -> Path:
    """Resolve a user-configurable path.

    Absolute paths are used as-is. Relative paths are resolved against the
    project root (``data/wake/hey_saint.onnx`` -> ``<project>/data/wake/...``).
    """
    p = Path(os.path.expandvars(os.path.expanduser(str(value or ""))))
    if p.is_absolute():
        return p
    if FROZEN:
        # "data/wake/hey_saint.onnx": the user's copy if they added one, else the shipped one.
        if p.parts and p.parts[0].lower() == "data":
            user = data_dir().joinpath(*p.parts[1:])
            if user.exists() or not resource_path(*p.parts).exists():
                return user
        return resource_path(*p.parts)
    return PROJECT_ROOT / p


def display_path(path) -> str:
    """Render a path relative to the project when possible (for logs/UI)."""
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except (ValueError, OSError):
        return str(path)

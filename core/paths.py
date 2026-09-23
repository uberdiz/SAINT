"""
core/paths.py

Project-relative path resolution.

Every runtime file SAINT reads or writes (config, logs, memory databases,
automations, the wake-word model) is resolved against the project root rather
than the current working directory, so SAINT behaves the same no matter where
it is launched from and never depends on a machine-specific absolute path.

Environment overrides:
    SAINT_DATA_DIR   - store runtime data somewhere other than <project>/data
                       (the test-suite uses this to isolate itself).
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    override = os.environ.get("SAINT_DATA_DIR", "").strip()
    path = Path(override) if override else PROJECT_ROOT / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


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
    return p if p.is_absolute() else PROJECT_ROOT / p


def display_path(path) -> str:
    """Render a path relative to the project when possible (for logs/UI)."""
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except (ValueError, OSError):
        return str(path)

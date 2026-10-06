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
both are the project folder — unless SAINT is also installed on this PC: then
the source run uses the installed app's data too, so however SAINT is started
it's one SAINT (one memory, one history, and one copy running: the lock file is
there). Two data folders made it look as if everything had been deleted.

Profiles (testing without your real data — ``run.bat --profile demo``,
tools/profiles.py): a profile is a separate data folder under
%LOCALAPPDATA%/SAINT-profiles/<name>, outside the project and outside any build,
with its own memory, history, scenes, settings and its own Windows Credential
Manager entries (Spotify login, Link key). Only the large model files are shared
with the real data folder, so a profile doesn't download them again.

Environment overrides:
    SAINT_DATA_DIR   - store runtime data somewhere other than <project>/data
                       (the test-suite uses this to isolate itself).
    SAINT_PROFILE    - use the named profile's data folder (set by ``--profile``).
"""

import os
import re
import sys
from pathlib import Path
from typing import Optional

FROZEN = bool(getattr(sys, "frozen", False))
# Where SAINT is installed (the folder with SAINT.exe, or the repository).
PROJECT_ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
# Read-only files shipped with SAINT (PyInstaller unpacks them to _MEIPASS).
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT)) if FROZEN else PROJECT_ROOT
# The folders under data/ that ship with SAINT (wake-word and voice models); the rest of data/ is the user's.
_SHIPPED_DATA = {"wake", "tts"}


def _local_app_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def installed_exe() -> Optional[Path]:
    """SAINT.exe from the installer (%LOCALAPPDATA%\\Programs\\SAINT), when it's installed."""
    exe = _local_app_data() / "Programs" / "SAINT" / "SAINT.exe"
    return exe if exe.exists() else None


def base_data_dir() -> Path:
    """The real data folder (no profile): where the big model files live for every profile."""
    override = os.environ.get("SAINT_DATA_DIR", "").strip()
    if override:
        path = Path(override)
    elif FROZEN or installed_exe() is not None:
        path = _local_app_data() / "SAINT"
    else:
        path = PROJECT_ROOT / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def profile_name() -> str:
    """The active test profile ("" = your real data)."""
    if os.environ.get("SAINT_DATA_DIR", "").strip():
        return ""                                  # an explicit folder (tests) wins
    return re.sub(r"[^\w.-]", "-", os.environ.get("SAINT_PROFILE", "").strip())[:40]


def profiles_root() -> Path:
    return _local_app_data() / "SAINT-profiles"


def data_dir() -> Path:
    name = profile_name()
    if not name:
        return base_data_dir()
    path = profiles_root() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_dir() -> Path:
    """Large downloaded models (Whisper, Kokoro): shared by every profile."""
    return base_data_dir()


def keyring_service() -> str:
    """Credential Manager service for secrets (Spotify login, Link key): one per profile."""
    name = profile_name()
    return f"SAINT-profile-{name}" if name else "SAINT"


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
    # "data/wake/hey_saint.onnx": the user's copy if they added one, else the shipped one. Everything else under
    # data/ is the user's (memory, history…), so it always lives in data_dir(). (Run from source with the data in
    # <project>/data both are the same file.)
    if p.parts and p.parts[0].lower() == "data":
        user = data_dir().joinpath(*p.parts[1:])
        shipped = resource_path(*p.parts)
        if len(p.parts) > 1 and p.parts[1].lower() in _SHIPPED_DATA and not user.exists():
            shared = models_dir().joinpath(*p.parts[1:])         # a profile uses the real folder's models
            if shared.exists():
                return shared
            if shipped.exists():
                return shipped
        return user
    return resource_path(*p.parts)


def display_path(path) -> str:
    """Render a path relative to the project when possible (for logs/UI)."""
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except (ValueError, OSError):
        return str(path)

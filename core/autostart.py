"""
core/autostart.py

"Start SAINT with Windows": a shortcut named SAINT in the user's Startup
folder (shell:startup) that runs SAINT hidden in the tray (``--background``).

* From source it points at the project's pythonw.exe (no console window) and
  app.py; packaged as SAINT.exe it points at the exe.
* A shortcut rather than a Run-registry value, so Task Manager's Startup apps
  lists it as "SAINT" with SAINT's icon (not "Python") and the user can switch
  it off there too.
* A Start-menu shortcut is made alongside it, so SAINT can be launched and
  pinned like any installed app.

Nothing here needs admin rights: both folders belong to the current user.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

from core.paths import FROZEN, data_path, installed_exe, resource_path

log = logging.getLogger("saint.autostart")

NAME = "SAINT"


def _programs_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / \
        "Microsoft" / "Windows" / "Start Menu" / "Programs"


def startup_shortcut() -> Path:
    return _programs_dir() / "Startup" / f"{NAME}.lnk"


def start_menu_shortcut() -> Path:
    return _programs_dir() / f"{NAME}.lnk"


def launch_command() -> Tuple[str, str, str]:
    """(target, arguments, working directory) that start SAINT hidden."""
    if FROZEN:
        exe = Path(sys.executable)
        return str(exe), "--background", str(exe.parent)
    installed = installed_exe()
    if installed is not None:
        # Installed SAINT is the one to start (a run from source shares its data): don't take its shortcuts.
        return str(installed), "--background", str(installed.parent)
    root = Path(__file__).resolve().parent.parent
    app_exe = root / ".venv" / "SAINT" / "SAINT.exe"      # core/app_exe.py: "SAINT" in Task Manager
    if app_exe.exists():
        return str(app_exe), f'"{root / "app.py"}" --background', str(root)
    venv = root / ".venv" / "Scripts" / "pythonw.exe"
    exe = venv if venv.exists() else Path(sys.executable).with_name("pythonw.exe")
    if not exe.exists():
        exe = Path(sys.executable)
    return str(exe), f'"{root / "app.py"}" --background', str(root)


def icon_path() -> Optional[str]:
    """SAINT's logo as an .ico (made once from SAINT.png)."""
    ico = data_path("SAINT.ico")
    if ico.exists():
        return str(ico)
    png = resource_path("SAINT.png")
    if not png.exists():
        return None
    try:
        from PIL import Image
        img = Image.open(png).convert("RGBA")
        img.save(ico, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        return str(ico)
    except Exception as e:
        log.info("autostart.icon_failed %s", e)
        return None


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _make_shortcut(path: Path, target: str, args: str, workdir: str, icon: Optional[str]) -> None:
    """Create a .lnk through the WScript.Shell COM object (built into Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(path) + ")",
        "$s.TargetPath = " + _ps_quote(target),
        "$s.Arguments = " + _ps_quote(args),
        "$s.WorkingDirectory = " + _ps_quote(workdir),
        "$s.Description = 'SAINT desktop assistant'",
        "$s.WindowStyle = 7",
    ]
    if icon:
        lines.append("$s.IconLocation = " + _ps_quote(icon + ",0"))
    lines.append("$s.Save()")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                        "; ".join(lines)], capture_output=True, text=True, timeout=20, creationflags=flags)
    if r.returncode != 0 or not path.exists():
        raise RuntimeError((r.stderr or r.stdout or "the shortcut wasn't created").strip()[:200])


def is_enabled() -> bool:
    return startup_shortcut().exists()


def set_enabled(on: bool) -> Tuple[bool, str]:
    """Add or remove SAINT from the apps Windows starts at sign-in."""
    from core.config import config
    link = startup_shortcut()
    try:
        if on:
            target, args, workdir = launch_command()
            icon = icon_path()
            _make_shortcut(link, target, args, workdir, icon)
            try:
                _make_shortcut(start_menu_shortcut(), target, args.replace(" --background", ""), workdir, icon)
            except Exception as e:                    # the Start-menu entry is a nicety
                log.info("autostart.start_menu_failed %s", e)
            log.info("autostart.enabled %s -> %s %s", link, target, args)
            message = "SAINT will start when you sign in to Windows."
        else:
            if link.exists():
                link.unlink()
            log.info("autostart.disabled")
            message = "SAINT won't start with Windows."
    except Exception as e:
        log.warning("autostart.failed on=%s %s", on, e)
        config.set("system.start_with_windows", is_enabled())
        return False, f"I couldn't change the Windows startup setting: {e}"
    config.set("system.start_with_windows", bool(on))
    return True, message


def sync() -> None:
    """At launch: keep the setting and the shortcut in step. If SAINT moved (a
    new folder or venv), a shortcut the user turned on is pointed at the new place."""
    from core.config import config
    try:
        want = bool(config.get("system.start_with_windows", False))
        if want and not is_enabled():
            config.set("system.start_with_windows", False)       # removed in Task Manager / Explorer
        elif not want and is_enabled():
            config.set("system.start_with_windows", True)        # made by hand or by an older SAINT
        elif want:
            target, args, workdir = launch_command()
            if not _points_at(startup_shortcut(), target):
                set_enabled(True)
    except Exception as e:
        log.debug("autostart.sync_failed %s", e)


def _points_at(link: Path, target: str) -> bool:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "(New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(link) + ").TargetPath"],
                           capture_output=True, text=True, timeout=15, creationflags=flags)
        return os.path.normcase(r.stdout.strip()) == os.path.normcase(target)
    except Exception:
        return True


def refresh_shortcuts(start_menu: bool = True) -> None:
    """Point SAINT's shortcuts at how it starts now (SAINT.exe once it's built):
    the Startup one if the user turned it on, and a Start-menu entry so SAINT
    can be launched and pinned like any installed app."""
    sync()
    if not start_menu or os.name != "nt":
        return
    target, args, workdir = launch_command()
    link = start_menu_shortcut()
    if link.exists() and _points_at(link, target):
        return
    try:
        _make_shortcut(link, target, args.replace(" --background", ""), workdir, icon_path())
        log.info("autostart.start_menu %s -> %s", link, target)
    except Exception as e:
        log.info("autostart.start_menu_failed %s", e)

"""
modules/dev/tools.py

Hands-free coding helpers:

    "run the tests"                     opens a visible terminal in dev.project_dir running
                                        dev.test_command (never a hidden shell)
    "open the file causing the error"   finds  File "x.py", line 12  /  path:line  in what
                                        you copied or what's on screen, opens it at that line
    "explain the error I copied"        the clipboard, explained in two sentences
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from core.config import config
from modules.automation.tools import P, PermissionLevel, Tool, ToolError

_PY_FRAME = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)')
_PATH_LINE = re.compile(r"(?P<path>(?:[A-Za-z]:[\\/]|\.{0,2}[\\/])?[\w .\\/-]+?\.(?:py|js|ts|tsx|jsx|cs|cpp|c|h|hpp|"
                        r"java|go|rs|rb|php|lua|json|yml|yaml|toml|ini|md|html|css|kt|swift))"
                        r"(?::|\(|, line |:line )(?P<line>\d+)")


def project_dir() -> str:
    d = config.get("dev.project_dir", "") or ""
    return d if d and os.path.isdir(d) else str(Path(__file__).resolve().parents[2])


def run_tests(command: str = ""):
    cmd = command or config.get("dev.test_command", "python -m pytest -q") or "python -m pytest -q"
    cwd = project_dir()
    venv_py = os.path.join(cwd, ".venv", "Scripts", "python.exe")
    if cmd.startswith("python ") and os.path.isfile(venv_py):
        cmd = f'"{venv_py}" ' + cmd[len("python "):]
    wt = shutil.which("wt")
    if wt:
        subprocess.Popen([wt, "-d", cwd, "cmd", "/k", cmd])
    else:
        subprocess.Popen(["cmd", "/k", cmd], cwd=cwd, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return {"running": cmd, "in": cwd}


def error_locations(text: str, base: Optional[str] = None) -> List[Tuple[str, int]]:
    """(path, line) pairs mentioned in an error / traceback, innermost last,
    only ones that exist on disk (relative paths resolve against ``base``)."""
    base = base or project_dir()
    found: List[Tuple[str, int]] = []
    for m in list(_PY_FRAME.finditer(text or "")) + list(_PATH_LINE.finditer(text or "")):
        raw = m.group("path").strip()
        path = raw if os.path.isabs(raw) else os.path.normpath(os.path.join(base, raw.lstrip("./\\")))
        if os.path.isfile(path):
            loc = (path, int(m.group("line")))
            if loc not in found:
                found.append(loc)
    return found


def best_location(locs: List[Tuple[str, int]], base: Optional[str] = None) -> Optional[Tuple[str, int]]:
    """The frame most likely yours: the innermost one inside the project,
    else the innermost that isn't in site-packages / the standard library."""
    if not locs:
        return None
    base = os.path.normcase(os.path.abspath(base or project_dir()))
    mine = [l for l in locs if os.path.normcase(os.path.abspath(l[0])).startswith(base)
            and "site-packages" not in l[0].lower()]
    if mine:
        return mine[-1]
    not_lib = [l for l in locs if "site-packages" not in l[0].lower() and "\\lib\\" not in l[0].lower()]
    return (not_lib or locs)[-1]


def open_at(path: str, line: int):
    editor = config.get("dev.editor", "code") or "code"
    exe = shutil.which(editor)
    if exe:
        subprocess.Popen([exe, "-g", f"{path}:{line}"], shell=exe.lower().endswith((".cmd", ".bat")))
    else:
        os.startfile(path)
    return {"opened": path, "line": line}


def open_error_file(text: str = ""):
    source = text
    if not source:
        from modules.desktop.clipboard import get_text_or_none
        source = get_text_or_none() or ""
    locs = error_locations(source)
    if not locs:
        try:
            from modules.desktop import uia
            source = "\n".join(uia.read_text(limit=200).get("text") or [])
            locs = error_locations(source)
        except Exception:
            locs = []
    loc = best_location(locs)
    if loc is None:
        raise ToolError("I couldn't find a file and line number in what you copied or what's on screen.",
                        "NOT_FOUND")
    return dict(open_at(*loc), candidates=len(locs))


def register_dev_tools(registry):
    tools = [
        # Runs a command, so it is never offered to the LLM; it always opens a visible terminal.
        Tool("dev.run_tests", "Run the project's tests in a visible terminal", {}, PermissionLevel.MEDIUM,
             run_tests, parameters={"command": P("string", required=False, default="")}, category="dev"),
        Tool("dev.open_error_file", "Open the file and line an error points at (from the clipboard or screen)",
             {}, PermissionLevel.LOW, open_error_file,
             parameters={"text": P("string", required=False, default="")}, category="dev"),
    ]
    for t in tools:
        registry.register(t)

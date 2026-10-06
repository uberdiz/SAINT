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


# ---------------------------------------------------------------------- #
# v0.4: projects, captured test runs, dev servers (modules/dev/project.py)
# ---------------------------------------------------------------------- #
def _project(project: str = ""):
    from modules.dev import project as proj
    p = proj.detect(project) if project and os.path.isdir(project) else proj.find_project(project)
    if p is None:
        raise ToolError(f"I couldn't find a project called {project}." if project else
                        "I don't know which project you mean — say its name, or set it in Settings › Advanced.",
                        "NOT_FOUND")
    return p


def open_project(name: str = ""):
    from modules.dev import project as proj
    p = _project(name)
    proj.open_in_editor(p.path)
    if name:
        proj.remember_project(name, p.path)
    config.set("dev.project_dir", p.path)          # "run the tests" / "start the server" mean this one now
    return {"path": p.path, "name": p.name, "kind": p.kind, "start": p.start, "test": p.test}


def test_run(project: str = "", cancel_event=None):
    from modules.dev import project as proj
    p = _project(project)
    if not p.test:
        raise ToolError(f"I don't see tests in {p.name} (no pytest setup or test script).", "NOT_FOUND")
    code, out = proj.run_capture(p.test, p.path, timeout=float(config.get("dev.test_timeout_sec", 900)),
                                 key="tests", cancel=cancel_event)
    summary, failures = proj.summarize_tests(out)
    return {"project": p.name, "path": p.path, "exit_code": code, "passed": code == 0,
            "summary": summary or ("all passed" if code == 0 else f"exit code {code}"),
            "failures": [{"test": t, "error": e} for t, e in failures], "tail": out[-1500:]}


def test_failures():
    from modules.dev import project as proj
    out = proj.LAST_OUTPUT.get("tests", "")
    if not out:
        raise ToolError("I haven't run the tests yet — say “run the tests” first.", "NOT_FOUND")
    summary, failures = proj.summarize_tests(out)
    locs = error_locations(out)
    diag = proj.diagnose(out, project_dir())
    return {"summary": summary, "failures": [{"test": t, "error": e} for t, e in failures],
            "location": best_location(locs), "diagnosis": diag.reason if diag else "",
            "diagnosis_kind": diag.kind if diag else "", "detail": diag.detail if diag else ""}


def start_server(project: str = "", cancel_event=None):
    from modules.dev import project as proj
    p = _project(project)
    s = proj.servers.start(p, cancel=cancel_event)
    if not s.alive():
        diag = proj.diagnose(s.tail(60), p.path)
        why = f" — {diag.reason}" if diag else ""
        raise ToolError(f"The {p.name} server exited with code {s.proc.returncode}{why}.\n{s.tail(12)}",
                        "PROCESS_EXITED")
    return {"project": p.name, "path": p.path, "url": s.url or p.url, "port": p.port, "pid": s.proc.pid}


def stop_server(project: str = ""):
    from modules.dev import project as proj
    p = _project(project)
    return {"project": p.name, "stopped": proj.servers.stop(p)}


def server_status(project: str = ""):
    from modules.dev import project as proj
    running = proj.servers.running()
    return {"running": [{"project": s.project.name, "url": s.url, "since": s.started} for s in running]}


def install_deps(project: str = "", cancel_event=None):
    from modules.dev import project as proj
    p = _project(project)
    if not p.install:
        raise ToolError(f"I don't know how to install {p.name}'s packages.", "NOT_FOUND")
    code, out = proj.run_capture(p.install, p.path, timeout=900, key="install", cancel=cancel_event)
    if code != 0:
        raise ToolError(f"Installing {p.name}'s packages failed (exit code {code}): {out.strip()[-300:]}",
                        "INSTALL_FAILED")
    return {"project": p.name, "command": p.install}


def pip_install(package: str, project: str = "", cancel_event=None):
    from modules.dev import project as proj
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?([<>=!~]=?[A-Za-z0-9.*]+)?$", package or ""):
        raise ToolError(f"“{package}” doesn't look like a package name.", "INVALID")
    p = _project(project)
    py = proj._python(p.path)
    code, out = proj.run_capture(f"{py} -m pip install {package}", p.path, timeout=600, key="install",
                                 cancel=cancel_event)
    if code != 0:
        raise ToolError(f"pip couldn't install {package}: {out.strip()[-300:]}", "INSTALL_FAILED")
    return {"package": package, "project": p.name}


def clone(url: str = "", cancel_event=None):
    """git clone a repository: the URL said, else one copied, else the browser's address."""
    from modules.dev import project as proj
    url = (url or "").strip()
    if not url:
        from modules.agent.autonomy.observe import observer
        for candidate in ((observer.clipboard().value or ""), ((observer.browser().value or {}).get("url") or "")):
            m = re.search(r"https?://(?:www\.)?(?:github\.com|gitlab\.com|bitbucket\.org)/[\w.-]+/[\w.-]+", candidate)
            if m:
                url = m.group(0)
                break
    if not re.match(r"^https?://[\w.-]+/[\w./-]+$", url):
        raise ToolError("I need the project's address — copy it, open it in the browser, or say it.", "NOT_FOUND")
    if not shutil.which("git"):
        raise ToolError("Git isn't installed, so I can't download the project.", "UNAVAILABLE")
    root = os.path.expanduser(config.get("dev.clone_root", "") or os.path.join("~", "source", "repos"))
    os.makedirs(root, exist_ok=True)
    name = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1])
    dest = os.path.join(root, name)
    if os.path.isdir(dest):
        return {"name": name, "path": dest, "existed": True}
    code, out = proj.run_capture(f'git clone --depth 1 "{url}" "{dest}"', root, timeout=600, cancel=cancel_event)
    if code != 0:
        raise ToolError(f"git couldn't download it: {out.strip()[-300:]}", "CLONE_FAILED")
    config.set("dev.project_dir", dest)
    return {"name": name, "path": dest, "existed": False}


def find_definition(symbol: str, project: str = ""):
    from modules.dev import project as proj
    p = _project(project)
    hits = proj.find_definition(symbol, p.path)
    if not hits:
        raise ToolError(f"I couldn't find where {symbol} is defined in {p.name}.", "NOT_FOUND")
    path, line, text = hits[0]
    return {"symbol": symbol, "path": path, "line": line, "text": text,
            "others": [{"path": h[0], "line": h[1]} for h in hits[1:]]}


def register_dev_tools(registry):
    tools = [
        # Runs a command, so it is never offered to the LLM; it always opens a visible terminal.
        Tool("dev.run_tests", "Run the project's tests in a visible terminal", {}, PermissionLevel.MEDIUM,
             run_tests, parameters={"command": P("string", required=False, default="")}, category="dev"),
        Tool("dev.open_error_file", "Open the file and line an error points at (from the clipboard or screen)",
             {}, PermissionLevel.LOW, open_error_file,
             parameters={"text": P("string", required=False, default="")}, category="dev"),
        Tool("dev.open_project", "Open a code project in the editor (finds it by name)", {}, PermissionLevel.LOW,
             open_project, parameters={"name": P("string", required=False, default="")}, category="dev"),
        # The rest run commands: MEDIUM, and never offered to the LLM.
        Tool("dev.test_run", "Run the project's tests and read the results", {}, PermissionLevel.MEDIUM,
             test_run, parameters={"project": P("string", required=False, default="")}, category="dev",
             cancellable=True),
        Tool("dev.test_failures", "Why the last test run failed", {}, PermissionLevel.LOW, test_failures,
             parameters={}, category="dev"),
        Tool("dev.start_server", "Start the project's dev server and wait until it answers", {},
             PermissionLevel.MEDIUM, start_server, parameters={"project": P("string", required=False, default="")},
             category="dev", cancellable=True),
        Tool("dev.stop_server", "Stop the project's dev server", {}, PermissionLevel.MEDIUM, stop_server,
             parameters={"project": P("string", required=False, default="")}, category="dev"),
        Tool("dev.server_status", "Dev servers SAINT is running", {}, PermissionLevel.LOW, server_status,
             parameters={"project": P("string", required=False, default="")}, category="dev"),
        Tool("dev.install_deps", "Install the project's packages (npm install / pip install -r)", {},
             PermissionLevel.MEDIUM, install_deps, parameters={"project": P("string", required=False, default="")},
             category="dev", cancellable=True),
        Tool("dev.pip_install", "Install one Python package into the project's environment", {},
             PermissionLevel.MEDIUM, pip_install, parameters={"package": P("string"),
                                                              "project": P("string", required=False, default="")},
             category="dev", cancellable=True),
        Tool("dev.clone", "Download (git clone) a code repository", {}, PermissionLevel.MEDIUM, clone,
             parameters={"url": P("string", required=False, default="")}, category="dev", cancellable=True),
        Tool("dev.find_definition", "Find where a function or class is defined in the project", {},
             PermissionLevel.LOW, find_definition, parameters={"symbol": P("string"),
                                                               "project": P("string", required=False, default="")},
             category="dev"),
    ]
    for t in tools:
        registry.register(t)

"""
modules/dev/project.py

The development side of the agent: find a project, understand how it runs, run it and its tests
with the output captured (so SAINT can read why something failed), and diagnose the failures it
can fix deterministically.

    find_project("saint")      -> Project(path, kind, start / test / install commands, port)
    servers.start(project)     -> a managed dev server: output kept, URL / port detected
    run_capture(cmd, cwd)      -> (exit code, output) for tests and installs
    diagnose(output)           -> missing packages, a busy port, a missing Python module, a moved name
    summarize_tests(output)    -> "12 passed, 2 failed" + the failing tests and their first error line
    find_definition("Foo")     -> where a function / class is defined in the project

Nothing here edits source files; changes go through dev tools that ask first (MEDIUM risk).
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.config import config

log = logging.getLogger("saint.dev")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "build", "dist", ".next",
              ".mypy_cache", ".pytest_cache", "site-packages", ".idea", ".vscode"}
_PORTS = {"vite": 5173, "next": 3000, "react-scripts": 3000, "nuxt": 3000, "astro": 4321, "svelte-kit": 5173,
          "webpack": 8080, "parcel": 1234, "gatsby": 8000, "django": 8000, "flask": 5000, "uvicorn": 8000,
          "streamlit": 8501}


@dataclass
class Project:
    path: str
    name: str
    kind: str = "unknown"            # node | python | unknown
    start: str = ""                  # command that runs it
    test: str = ""
    install: str = ""
    port: int = 0                    # expected local port (0 = unknown until it prints one)
    notes: List[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}" if self.port else ""


# ---------------------------------------------------------------------- #
# Finding and understanding a project
# ---------------------------------------------------------------------- #
def _roots() -> List[str]:
    home = os.path.expanduser("~")
    roots = list(config.get("dev.search_roots", []) or [])
    roots += [os.path.join(home, d) for d in ("source", "repos", "Projects", "projects", "code", "dev",
                                               os.path.join("Documents", "GitHub"), "Documents", "Desktop")]
    roots += ["C:\\python", "C:\\dev", "C:\\src", "C:\\projects", "D:\\dev", "D:\\projects"]
    return [r for r in dict.fromkeys(roots) if r and os.path.isdir(r)]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _is_project(path: str) -> bool:
    return any(os.path.exists(os.path.join(path, f)) for f in
               ("package.json", "pyproject.toml", "requirements.txt", "setup.py", "app.py", "main.py", "manage.py",
                ".git", "Cargo.toml", "go.mod", "pom.xml"))


def find_project(name: str = "") -> Optional[Project]:
    """``name`` "" = the configured / current project. Otherwise: SAINT's own registry
    (dev.projects), then project folders under the usual roots whose name matches."""
    name = re.sub(r"\b(?:my|the|project|repo|repository|folder|code)\b", "", (name or "").lower()).strip()
    registry = {(_norm(k)): v for k, v in (config.get("dev.projects", {}) or {}).items()}
    if not name:
        d = config.get("dev.project_dir", "") or ""
        return detect(d) if d and os.path.isdir(d) else None
    if _norm(name) in registry and os.path.isdir(registry[_norm(name)]):
        return detect(registry[_norm(name)])
    want = _norm(name)
    best: Optional[Tuple[int, float, str]] = None
    for root in _roots():
        try:
            level1 = [os.path.join(root, d) for d in os.listdir(root)]
        except OSError:
            continue
        for d in level1:
            if not os.path.isdir(d) or os.path.basename(d).lower() in _SKIP_DIRS:
                continue
            cands = [d]
            try:
                cands += [os.path.join(d, x) for x in os.listdir(d) if os.path.isdir(os.path.join(d, x))
                          and x.lower() not in _SKIP_DIRS][:40]
            except OSError:
                pass
            for c in cands:
                key = _norm(os.path.basename(c))
                if not key or want not in key:
                    continue
                score = (0 if key == want else 1, -os.path.getmtime(c) if os.path.exists(c) else 0, c)
                if _is_project(c) and (best is None or score < best):
                    best = score
    return detect(best[2]) if best else None


def remember_project(name: str, path: str):
    projects = dict(config.get("dev.projects", {}) or {})
    projects[name] = path
    config.set("dev.projects", projects)


def _python(path: str) -> str:
    for venv in (".venv", "venv", "env"):
        exe = os.path.join(path, venv, "Scripts", "python.exe")
        if os.path.isfile(exe):
            return f'"{exe}"'
    return "python"


def detect(path: str) -> Optional[Project]:
    if not path or not os.path.isdir(path):
        return None
    p = Project(path=os.path.abspath(path), name=os.path.basename(os.path.abspath(path)))
    pkg = os.path.join(path, "package.json")
    if os.path.isfile(pkg):
        p.kind = "node"
        try:
            data = json.loads(Path(pkg).read_text(encoding="utf-8", errors="ignore"))
        except ValueError:
            data = {}
        scripts = data.get("scripts") or {}
        pm = "pnpm" if os.path.exists(os.path.join(path, "pnpm-lock.yaml")) else \
             "yarn" if os.path.exists(os.path.join(path, "yarn.lock")) else "npm"
        for s in ("dev", "start", "serve"):
            if s in scripts:
                p.start = f"{pm} run {s}"
                cmd = scripts[s]
                m = re.search(r"(?:--port|-p)[ =](\d{2,5})", cmd)
                p.port = int(m.group(1)) if m else next((v for k, v in _PORTS.items() if k in cmd), 0)
                break
        if "test" in scripts and "no test specified" not in scripts["test"]:
            p.test = f"{pm} test"
        p.install = f"{pm} install"
        return p
    py = _python(path)
    if any(os.path.exists(os.path.join(path, f)) for f in ("pyproject.toml", "requirements.txt", "setup.py",
                                                          "app.py", "main.py", "manage.py")):
        p.kind = "python"
        if os.path.exists(os.path.join(path, "manage.py")):
            p.start, p.port = f"{py} manage.py runserver", 8000
        elif os.path.exists(os.path.join(path, "app.py")):
            p.start = f"{py} app.py"
        elif os.path.exists(os.path.join(path, "main.py")):
            p.start = f"{py} main.py"
        if os.path.isdir(os.path.join(path, "tests")) or os.path.exists(os.path.join(path, "pytest.ini")) or \
                os.path.exists(os.path.join(path, "conftest.py")):
            p.test = f"{py} -m pytest -q"
        if os.path.exists(os.path.join(path, "requirements.txt")):
            p.install = f"{py} -m pip install -r requirements.txt"
        elif os.path.exists(os.path.join(path, "pyproject.toml")):
            p.install = f"{py} -m pip install -e ."
    return p


# ---------------------------------------------------------------------- #
# Running things
# ---------------------------------------------------------------------- #
LAST_OUTPUT: Dict[str, str] = {}       # "tests" / "install" -> the most recent output (for "why did it fail?")


def run_capture(cmd: str, cwd: str, timeout: float = 600, key: str = "",
                cancel: Optional[threading.Event] = None) -> Tuple[int, str]:
    """Run ``cmd`` without a window, output captured. (exit code, output); -1 on timeout / cancel."""
    log.info("dev.run %s (in %s)", cmd, cwd)
    proc = subprocess.Popen(cmd, cwd=cwd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                            env=dict(os.environ, PYTHONUNBUFFERED="1", FORCE_COLOR="0", CI="1"))
    out: List[str] = []
    reader = threading.Thread(target=lambda: out.extend(
        line.decode("utf-8", "replace") for line in iter(proc.stdout.readline, b"")), daemon=True)
    reader.start()
    deadline = time.time() + timeout
    code = None
    while code is None:
        code = proc.poll()
        if code is None and (time.time() > deadline or (cancel is not None and cancel.is_set())):
            _kill_tree(proc.pid)
            code = -1
            out.append("\n[stopped: timed out]\n" if time.time() > deadline else "\n[stopped]\n")
        elif code is None:
            time.sleep(0.2)
    reader.join(2)
    text = "".join(out)
    if key:
        LAST_OUTPUT[key] = text
    return code, text


def _kill_tree(pid: int):
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=_NO_WINDOW)
    except Exception:
        pass


@dataclass
class Server:
    project: Project
    proc: subprocess.Popen
    output: deque = field(default_factory=lambda: deque(maxlen=400))
    url: str = ""
    started: float = field(default_factory=time.time)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def tail(self, n: int = 30) -> str:
        return "".join(list(self.output)[-n:])


_URL = re.compile(r"(https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\])(?::\d+)?[^\s\"'<>]*)", re.I)


class Servers:
    """Dev servers SAINT started (one per project). They stop with SAINT."""

    def __init__(self):
        self._servers: Dict[str, Server] = {}
        self._lock = threading.Lock()

    def get(self, project: Project) -> Optional[Server]:
        with self._lock:
            return self._servers.get(project.path)

    def running(self) -> List[Server]:
        with self._lock:
            return [s for s in self._servers.values() if s.alive()]

    def start(self, project: Project, wait: float = 45.0, cancel: Optional[threading.Event] = None) -> Server:
        """Start (or reuse) the server; return once it answers, prints its URL, or exits."""
        if not project.start:
            raise RuntimeError(f"I don't know how to run {project.name} — no dev/start script or app file.")
        existing = self.get(project)
        if existing and existing.alive():
            return existing
        proc = subprocess.Popen(project.start, cwd=project.path, shell=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                                env=dict(os.environ, PYTHONUNBUFFERED="1", FORCE_COLOR="0", BROWSER="none"))
        server = Server(project, proc)
        with self._lock:
            self._servers[project.path] = server
        threading.Thread(target=self._pump, args=(server,), daemon=True, name=f"dev-{project.name}").start()
        log.info("dev.server.start %s: %s", project.name, project.start)
        deadline = time.time() + wait
        from modules.agent.autonomy.observe import observer
        while time.time() < deadline and not (cancel is not None and cancel.is_set()):
            if not server.alive():
                break
            if server.url or (project.port and observer.port(project.port).ok):
                break
            time.sleep(0.4)
        if not server.url and project.port:
            server.url = project.url
        return server

    def _pump(self, server: Server):
        log_path = None
        try:
            from core.paths import data_path
            log_path = data_path("logs", "dev", f"{re.sub(r'[^A-Za-z0-9_.-]', '_', server.project.name)}.log")
        except Exception:
            pass
        fh = open(log_path, "a", encoding="utf-8") if log_path else None
        try:
            for raw in iter(server.proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace")
                server.output.append(line)
                if fh:
                    fh.write(line)
                    fh.flush()
                if not server.url:
                    m = _URL.search(line)
                    if m:
                        server.url = m.group(1).replace("0.0.0.0", "localhost").rstrip("/.,")
                        pm = re.search(r":(\d{2,5})", server.url)
                        if pm:
                            server.project.port = int(pm.group(1))
        finally:
            if fh:
                fh.close()
        code = server.proc.wait()
        log.info("dev.server.exit %s code=%s", server.project.name, code)
        try:
            from core.events import event_bus, EventType
            event_bus.emit_event(EventType.NOTIFY, {"title": f"{server.project.name} server stopped",
                                                    "message": f"exit code {code}"})
        except Exception:
            pass

    def stop(self, project: Project) -> bool:
        with self._lock:
            s = self._servers.pop(project.path, None)
        if s and s.alive():
            _kill_tree(s.proc.pid)
            return True
        return False

    def stop_all(self):
        with self._lock:
            servers, self._servers = list(self._servers.values()), {}
        for s in servers:
            if s.alive():
                _kill_tree(s.proc.pid)


servers = Servers()


# ---------------------------------------------------------------------- #
# Reading failures
# ---------------------------------------------------------------------- #
@dataclass
class Diagnosis:
    kind: str                # missing_dependencies | python_module | port_in_use | import_name | unknown
    reason: str              # for the user / trail
    detail: str = ""         # a package name, an import name...
    module: str = ""


_PIP_NAMES = {"cv2": "opencv-python", "PIL": "pillow", "yaml": "pyyaml", "sklearn": "scikit-learn",
              "bs4": "beautifulsoup4", "dotenv": "python-dotenv", "win32api": "pywin32", "win32con": "pywin32",
              "Crypto": "pycryptodome", "skimage": "scikit-image", "attr": "attrs", "jwt": "pyjwt"}


def diagnose(output: str, project_path: str = "") -> Optional[Diagnosis]:
    out = output or ""
    m = re.search(r"ModuleNotFoundError: No module named ['\"]([\w.]+)['\"]", out)
    if m:
        top = m.group(1).split(".")[0]
        if project_path and (os.path.isdir(os.path.join(project_path, top)) or
                             os.path.isfile(os.path.join(project_path, top + ".py"))):
            return Diagnosis("import_name", f"“{m.group(1)}” is part of the project but can't be imported "
                             f"from where it runs", top)
        pkg = _PIP_NAMES.get(top, top)
        return Diagnosis("python_module", f"the Python package {pkg} isn't installed", pkg)
    m = re.search(r"ImportError: cannot import name ['\"](\w+)['\"] from ['\"]([\w.]+)['\"]", out)
    if m:
        return Diagnosis("import_name", f"{m.group(1)} isn't in {m.group(2)} any more", m.group(1), m.group(2))
    if re.search(r"Cannot find module|ERR_MODULE_NOT_FOUND|is not recognized as an internal or external command|"
                 r"command not found|node_modules|Could not resolve dependency|missing script", out, re.I) and \
            (not project_path or not os.path.isdir(os.path.join(project_path, "node_modules"))
             or re.search(r"Cannot find module", out)):
        return Diagnosis("missing_dependencies", "the project's packages aren't installed")
    if re.search(r"EADDRINUSE|address already in use|Only one usage of each socket address|port \d+ is in use", out, re.I):
        return Diagnosis("port_in_use", "its port is already in use — it may already be running")
    return None


def summarize_tests(output: str) -> Tuple[str, List[Tuple[str, str]]]:
    """('12 passed, 2 failed', [(test, first error line), ...]) for pytest / jest / vitest output."""
    out = output or ""
    m = re.findall(r"=+ (.*?(?:passed|failed|error|skipped).*?) in [\d.]+s", out)
    summary = m[-1] if m else ""
    if not summary:
        m = re.search(r"Tests:\s+(.*)", out)              # jest
        summary = m.group(1).strip() if m else ""
    if not summary:
        nums = re.findall(r"(\d+) (passed|failed)", out)
        summary = ", ".join(f"{n} {w}" for n, w in nums[-2:])
    failures = []
    for t, err in re.findall(r"^FAILED ([^\s]+)(?: - (.*))?$", out, re.M):
        failures.append((t, (err or "").strip()[:160]))
    if not failures:
        for t in re.findall(r"●\s+(.+)", out)[:5]:          # jest
            failures.append((t.strip(), ""))
    return summary, failures[:8]


def find_definition(symbol: str, root: str, limit: int = 8) -> List[Tuple[str, int, str]]:
    """(path, line, text) where ``symbol`` is defined: Python def/class, JS/TS function/class/const."""
    sym = re.escape(symbol.strip())
    if not sym or not root or not os.path.isdir(root):
        return []
    pat = re.compile(rf"^\s*(?:async\s+)?(?:def|class)\s+{sym}\b|"
                     rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\*?|class)\s+{sym}\b|"
                     rf"^\s*(?:export\s+)?(?:const|let|var)\s+{sym}\s*=")
    found = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if not f.endswith((".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cs", ".go", ".rs")):
                continue
            path = os.path.join(dirpath, f)
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        if pat.search(line):
                            found.append((path, i, line.strip()[:160]))
                            if len(found) >= limit:
                                return found
            except OSError:
                continue
    return found


def open_in_editor(path: str, line: int = 0) -> bool:
    editor = config.get("dev.editor", "code") or "code"
    exe = shutil.which(editor)
    target = f"{path}:{line}" if line else path
    if exe:
        args = [exe, "-g", target] if line else [exe, path]
        subprocess.Popen(args, shell=exe.lower().endswith((".cmd", ".bat")), creationflags=_NO_WINDOW)
        return True
    os.startfile(path)
    return False

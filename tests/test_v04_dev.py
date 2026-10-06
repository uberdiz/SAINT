"""v0.4 development agent: projects, captured runs, failure diagnosis (modules/dev/project.py) and the
spoken dev commands (modules/agent/dev_intents.py)."""

import json
import sys

import pytest

from modules.dev import project as proj


def test_detects_a_node_project(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite", "test": "vitest run"}}))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    p = proj.detect(str(tmp_path))
    assert (p.kind, p.start, p.test, p.install, p.port) == ("node", "pnpm run dev", "pnpm test", "pnpm install", 5173)
    assert p.url == "http://localhost:5173"


def test_detects_a_python_project_and_its_venv(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')")
    (tmp_path / "requirements.txt").write_text("requests\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts" / "python.exe").write_text("")
    p = proj.detect(str(tmp_path))
    assert p.kind == "python" and "app.py" in p.start and ".venv" in p.start
    assert p.test.endswith("-m pytest -q") and "requirements.txt" in p.install


def test_finds_a_project_by_name(tmp_path, monkeypatch):
    (tmp_path / "Code" / "my-cool-app").mkdir(parents=True)
    (tmp_path / "Code" / "my-cool-app" / "package.json").write_text("{}")
    monkeypatch.setattr(proj, "_roots", lambda: [str(tmp_path / "Code")])
    from core.config import config
    config.set("dev.projects", {}, persist=False)
    p = proj.find_project("my cool app")
    assert p is not None and p.name == "my-cool-app"
    assert proj.find_project("nothing like it") is None


@pytest.mark.parametrize("output,kind,detail", [
    ("Traceback...\nModuleNotFoundError: No module named 'yaml'", "python_module", "pyyaml"),
    ("ImportError: cannot import name 'load_config' from 'app.settings'", "import_name", "load_config"),
    ("Error: Cannot find module 'vite'\nRequire stack:", "missing_dependencies", ""),
    ("'vite' is not recognized as an internal or external command,", "missing_dependencies", ""),
    ("Error: listen EADDRINUSE: address already in use :::5173", "port_in_use", ""),
])
def test_diagnoses_why_a_project_wont_start(output, kind, detail):
    d = proj.diagnose(output)
    assert d is not None and d.kind == kind and d.detail == detail
    assert proj.diagnose("everything is fine") is None


def test_summarizes_pytest_output():
    out = ("..F.\n=========================== short test summary info ===========================\n"
           "FAILED tests/test_x.py::test_adds - AssertionError: assert 3 == 4\n"
           "=================== 1 failed, 3 passed in 0.12s ===================\n")
    summary, failures = proj.summarize_tests(out)
    assert summary == "1 failed, 3 passed"
    assert failures == [("tests/test_x.py::test_adds", "AssertionError: assert 3 == 4")]


def test_finds_definitions(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n\ndef load_config(path):\n    pass\n")
    (tmp_path / "web.ts").write_text("export const loadConfig = () => 1\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "b.py").write_text("def load_config():\n    pass\n")
    hits = proj.find_definition("load_config", str(tmp_path))
    assert [(h[0].endswith("a.py"), h[1]) for h in hits] == [(True, 3)]          # node_modules skipped
    assert proj.find_definition("loadConfig", str(tmp_path))[0][1] == 1


def test_run_capture_keeps_the_output_and_exit_code(tmp_path):
    code, out = proj.run_capture(f'"{sys.executable}" -c "print(41+1); raise SystemExit(3)"', str(tmp_path),
                                 timeout=60, key="tests")
    assert code == 3 and "42" in out and proj.LAST_OUTPUT["tests"] == out


def test_run_capture_can_be_cancelled(tmp_path):
    import threading
    stop = threading.Event()
    stop.set()
    code, out = proj.run_capture(f'"{sys.executable}" -c "import time; time.sleep(30)"', str(tmp_path),
                                 timeout=60, cancel=stop)
    assert code == -1 and "[stopped]" in out


@pytest.mark.parametrize("said,intent", [
    ("run the tests", "dev.run_tests"),
    ("run the tests in a terminal", "dev.run_tests_terminal"),
    ("why did the tests fail?", "dev.test_failures"),
    ("open my SAINT project", "dev.open_project"),
    ("start the dev server", "dev.start_server"),
    ("stop the dev server", "dev.stop_server"),
    ("install the project dependencies", "dev.install_deps"),
    ("pip install requests", "dev.pip_install"),
    ("where is load_config defined?", "dev.find_definition"),
    ("clone this repo", "dev.clone"),
])
def test_dev_commands_route(said, intent):
    from modules.agent.router import route
    it = route(said)
    assert it is not None and it.name == intent


@pytest.mark.parametrize("said", ["start the Discord app", "open spotify", "run steam", "start the minecraft server"])
def test_ordinary_requests_are_not_dev_commands(said):
    from modules.agent.router import route
    it = route(said)
    assert it is None or not it.name.startswith("dev.")


def test_dev_tools_that_run_commands_are_never_offered_to_the_llm():
    from modules.automation.tools import get_tool_registry
    reg = get_tool_registry()
    for name in ("dev.test_run", "dev.start_server", "dev.install_deps", "dev.pip_install", "dev.clone"):
        t = reg.get(name)
        assert t is not None and not t.llm_exposed and t.permission.value == "medium"


def test_pip_install_refuses_things_that_arent_package_names(tmp_path):
    from modules.automation.tools import get_tool_registry
    res = get_tool_registry().execute("dev.pip_install", package="requests; del C:\\", project=str(tmp_path))
    assert not res.success and "doesn't look like a package" in res.error

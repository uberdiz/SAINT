"""
modules/agent/dev_intents.py

Development commands (the tools are in modules/dev/tools.py):

    "open my SAINT project"                  dev.open_project
    "start the dev server" / "run the project"  dev.start_server (the agent runs it as a task)
    "stop the dev server" / "is the server running?"
    "install the project dependencies" / "install the python package requests"
    "run the tests"                          dev.run_tests: captured, results read out
    "run the tests in a terminal"            the visible terminal (as before)
    "why did the tests fail?"                dev.test_failures
    "where is load_config defined?"          dev.find_definition, opened at the line
    "clone this project" / "download this repo"   dev.clone (the address from what you said,
                                             copied, or the browser)

Long ones (server, tests, installs) are started as agent tasks by modules/agent/autonomy, so
SAINT keeps listening while they run; these intents are what each task step executes.
"""

import os
import re
from typing import Optional

from modules.agent.router import Intent, Reply, call, run_tool

_P = r"(?:(?:my|the|this)\s+)?"
_PROJECT = re.compile(rf"^(?:open|load|pull up|bring up|go to)\s+{_P}(?P<name>[\w .:\\/-]+?)\s+(?:project|repo|repository|codebase)"
                      rf"(?:\s+in\s+(?:vs\s*code|visual studio code|the editor|code))?$|"
                      rf"^(?:open|load)\s+(?:the\s+)?project\s+(?P<name2>[\w .:\\/-]+)$", re.I)
_SERVER_START = re.compile(rf"^(?:start|run|launch|spin up|fire up|boot up)\s+{_P}(?:(?P<name>[\w .:\\/-]+?)\s+)?"
                           rf"(?:dev\s*server|development server|local server|server|project)"
                           rf"(?:\s+for\s+(?P<name2>[\w .:\\/-]+))?$", re.I)
_SERVER_STOP = re.compile(rf"^(?:stop|kill|shut down|close)\s+{_P}(?:(?P<name>[\w .-]+?)\s+)?(?:dev\s*server|server)$", re.I)
_SERVER_STATUS = re.compile(r"^(?:is\s+(?:the|my)\s+(?:dev\s*)?server\s+(?:still\s+)?(?:running|up)|"
                            r"(?:dev\s*)?server\s+status)\??$", re.I)
_INSTALL = re.compile(rf"^(?:install|reinstall)\s+{_P}(?:project'?s?\s+)?(?:dependencies|packages|deps|node modules)"
                      rf"(?:\s+for\s+(?P<name>[\w .:\\/-]+))?$|^(?:npm|yarn|pnpm)\s+install$", re.I)
_PIP = re.compile(r"^(?:install\s+(?:the\s+)?(?:python\s+)?(?:package|module|library)\s+|pip\s+install\s+)"
                  r"(?P<pkg>[A-Za-z0-9][A-Za-z0-9._\[\],<>=!~-]*)$", re.I)
_TESTS = re.compile(r"^(?:(?:run|start|re-?run)\s+(?:the\s+|my\s+|all\s+(?:the\s+)?)?(?:unit\s+)?tests?(?:\s+suite)?"
                    r"(?:\s+again)?|run the test suite|test (?:it|the project))"
                    r"(?P<term>\s+in\s+(?:a\s+|the\s+)?(?:terminal|new window|console))?$", re.I)
_WHY_TESTS = re.compile(r"^(?:why\s+(?:did|do|are|is)\s+(?:the\s+|my\s+)?tests?\s+(?:fail(?:ing|ed)?|broken)|"
                        r"what\s+(?:failed|broke)(?:\s+in\s+the\s+tests?)?|which\s+tests?\s+failed|"
                        r"what(?:'s|\s+is)\s+wrong\s+with\s+the\s+tests?)\??$", re.I)
_DEF = re.compile(r"^(?:find|show me|where(?:'s|\s+is)|go to|open)\s+(?:where\s+)?(?:the\s+(?:definition|function|class)\s+"
                  r"(?:of\s+|for\s+)?)?(?P<sym>[A-Za-z_][\w.]*)(?:\s+(?:is\s+)?defined)?\??$", re.I)
_CLONE = re.compile(r"^(?:clone|download|get|pull)\s+(?:this|that|the)\s+(?:project|repo|repository|code)"
                    r"(?:\s+(?:from\s+)?(?P<url>https?://\S+))?$|^(?:git\s+)?clone\s+(?P<url2>https?://\S+)$", re.I)


def _say(res, ok_text) -> Reply:
    if not res.success:
        return Reply(res.error or "That didn't work.", ok=False)
    return Reply(ok_text(res.result))


def parse_dev(text: str) -> Optional[Intent]:
    t = re.sub(r"[.!]+$", "", (text or "").strip())
    m = _TESTS.match(t)
    if m:
        if m.group("term"):
            return Intent("dev.run_tests_terminal", lambda: run_tool(
                "dev.run_tests", "run the tests", lambda r: "Running the tests in a new terminal."), "dev")

        def run_tests():
            res = call("dev.test_run")
            if not res.success:
                return Reply(res.error, ok=False)
            r = res.result
            if r["passed"]:
                return Reply(f"The tests passed — {r['summary']}.")
            first = r["failures"][0] if r["failures"] else None
            detail = f" The first failure is {first['test']}" + (f": {first['error']}" if first and first["error"]
                                                                 else "") + "." if first else ""
            return Reply(f"The tests failed — {r['summary']}.{detail}", ok=False)
        return Intent("dev.run_tests", run_tests, "dev")
    if _WHY_TESTS.match(t):
        def why():
            res = call("dev.test_failures")
            if not res.success:
                return Reply(res.error, ok=False)
            r = res.result
            parts = [f"{r['summary'] or 'The last run failed'}."]
            if r["diagnosis"]:
                parts.append(f"The cause looks like {r['diagnosis']}.")
            for f in r["failures"][:2]:
                parts.append(f"{f['test'].split('::')[-1]}: {f['error'] or 'see the output'}.")
            if r.get("location"):
                path, line = r["location"]
                parts.append(f"It points at {os.path.basename(path)} line {line} — say “open the file causing "
                             f"the error” to go there.")
            return Reply(" ".join(parts))
        return Intent("dev.test_failures", why, "dev")
    m = _PROJECT.match(t)
    if m:
        name = (m.group("name") or m.group("name2") or "").strip()
        if name.lower() not in ("this", "that", "a", "new"):
            return Intent("dev.open_project", lambda: _say(call("dev.open_project", name=name),
                                                           lambda r: f"Opened {r['name']}."), "dev")
    m = _SERVER_STOP.match(t)
    if m:
        name = (m.group("name") or "").strip()
        return Intent("dev.stop_server", lambda: _say(call("dev.stop_server", project=name),
                                                      lambda r: f"Stopped the {r['project']} server." if r["stopped"]
                                                      else f"The {r['project']} server wasn't running."), "dev")
    if _SERVER_STATUS.match(t):
        def status():
            res = call("dev.server_status")
            if not res.success:
                return Reply(res.error, ok=False)
            run = res.result["running"]
            if not run:
                return Reply("No dev server is running.")
            return Reply(" ".join(f"{s['project']} is running" + (f" at {s['url']}." if s["url"] else ".") for s in run))
        return Intent("dev.server_status", status, "dev")
    m = _SERVER_START.match(t)
    if m and not re.search(r"\b(?:steam|game|minecraft|spotify|discord)\b", t, re.I):
        name = (m.group("name") or m.group("name2") or "").strip()
        if name.lower() in ("dev", "development", "local", "the", "my"):
            name = ""
        return Intent("dev.start_server", lambda: _say(call("dev.start_server", project=name),
                                                       lambda r: f"{r['project']} is running" +
                                                       (f" at {r['url']}." if r.get("url") else ".")), "dev")
    m = _INSTALL.match(t)
    if m:
        name = (m.groupdict().get("name") or "").strip()
        return Intent("dev.install_deps", lambda: _say(call("dev.install_deps", project=name),
                                                       lambda r: f"Installed {r['project']}'s packages."), "dev")
    m = _PIP.match(t)
    if m:
        pkg = m.group("pkg")
        return Intent("dev.pip_install", lambda: _say(call("dev.pip_install", package=pkg),
                                                      lambda r: f"Installed {r['package']}."), "dev")
    m = _CLONE.match(t)
    if m:
        url = m.group("url") or m.group("url2") or ""
        return Intent("dev.clone", lambda: _say(call("dev.clone", url=url),
                                                lambda r: f"Cloned {r['name']} into {r['path']}."), "dev")
    m = _DEF.match(t)
    if m and ("_" in m.group("sym") or re.search(r"defined|definition", t, re.I)):
        sym = m.group("sym")

        def find():
            res = call("dev.find_definition", symbol=sym)
            if not res.success:
                return Reply(res.error, ok=False)
            r = res.result
            from modules.dev.project import open_in_editor
            open_in_editor(r["path"], r["line"])
            more = f" ({len(r['others'])} more places)" if r["others"] else ""
            return Reply(f"{sym} is defined in {os.path.basename(r['path'])} at line {r['line']} — opened it{more}.")
        return Intent("dev.find_definition", find, "dev")
    return None

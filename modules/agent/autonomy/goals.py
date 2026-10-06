"""
modules/agent/autonomy/goals.py

Built-in goals: requests that name an outcome rather than the steps. Each builds a plan from what
SAINT observes (which project, which port, which meeting app is installed, whether music is
playing) — the steps are ordinary SAINT commands with checks, so they can be learned, edited and
replayed like any other procedure.

    "set up my coding workspace" (for SAINT)   open the project, start its dev server, open it in
                                               the browser (+ anything else said in the request)
    "run the project" / "start the dev server" start it and check it answers
    "run the tests (and tell me why they failed)"
    "find out why it isn't starting and fix it"  start -> read the error -> fix -> start again
    "download this project ... and run it"     clone -> install -> start -> check
    "get everything ready for my meeting"      music off, the meeting app open, SAINT quiet

Extra clauses in the same sentence ("..., open Discord and put Spotify on my second monitor")
become extra steps after the goal's own.
"""

import re
from dataclasses import dataclass
from typing import List, Optional

from core.config import config
from modules.agent.autonomy.model import AgentTask, PlanStep, Risk
from modules.agent.autonomy.observe import Observer

_LEAD = r"^(?:(?:hey\s+)?saint[,\s]+)?(?:(?:please|can you|could you|would you|i want you to|go ahead and)\s+)*"


@dataclass
class Goal:
    name: str
    project: str = ""          # a named project ("for SAINT")
    rest: str = ""             # the rest of the sentence: more steps
    url: str = ""


_GOALS = [
    ("coding_workspace", re.compile(
        _LEAD + r"(?:set\s*up|get|make|prepare|ready|open|start|spin up)\s+(?:me\s+)?(?:my\s+|the\s+|a\s+)?"
        r"(?:coding|code|dev|development|programming|work)\s+(?:workspace|environment|setup|set\s*up|station|session|desk)"
        r"(?:\s+(?:ready|up))?(?:\s+for\s+(?P<project>[\w .-]+?))?(?P<rest>(?:,|\s+and\s+|\s+then\s+).+)?$|"
        + _LEAD + r"(?:get\s+me\s+ready\s+to\s+code|set\s+(?:me\s+)?up\s+(?:for|to)\s+cod(?:e|ing))"
        r"(?:\s+on\s+(?P<project2>[\w .-]+?))?(?P<rest2>(?:,|\s+and\s+).+)?$", re.I)),
    ("fix_and_run", re.compile(
        _LEAD + r"(?:(?:download|clone|get)\s+(?:this|that|the)\s+(?:project|repo|repository|code)(?P<url>\s+https?://\S+)?"
        r"[,\s]+(?:and\s+)?)?(?:inspect it[,\s]+(?:and\s+)?)?"
        r"(?:find|figure|work)(?:\s+out)?\s+why\s+(?:it|the\s+project|(?P<project>[\w .-]+?))\s+(?:isn'?t|is\s+not|won'?t|"
        r"doesn'?t|does\s+not|can'?t)\s+(?:start|run|work|load)(?:ing)?(?P<rest>.*)$", re.I)),
    ("fix_and_run", re.compile(
        _LEAD + r"(?:download|clone|get)\s+(?:this|that|the)\s+(?:project|repo|repository|code)(?P<url>\s+https?://\S+)?"
        r"(?:,|\s+and)\s+(?:then\s+)?(?:run|start|launch)\s+it(?P<rest>.*)$", re.I)),
    ("run_project", re.compile(
        _LEAD + r"(?:start|run|launch|spin up|fire up|boot up)\s+(?:my\s+|the\s+)?(?:(?P<project>[\w .-]+?)\s+)?"
        r"(?:dev\s*server|development server|local server|project)(?:\s+for\s+(?P<project2>[\w .-]+?))?"
        r"(?P<rest>(?:,|\s+and\s+|\s+then\s+).+)?$", re.I)),
    ("run_tests", re.compile(
        _LEAD + r"(?:run|start|re-?run)\s+(?:the\s+|my\s+|all\s+(?:the\s+)?)?(?:unit\s+)?tests?(?:\s+suite)?(?:\s+again)?"
        r"(?P<rest>(?:,|\s+and\s+).+)?$", re.I)),
    ("meeting_prep", re.compile(
        _LEAD + r"(?:get|make)\s+(?:everything|me|things|it\s+all)\s+ready\s+for\s+(?:my|the|a)\s+(?:meeting|call|class|lecture)"
        r"(?P<rest>(?:,|\s+and\s+).+)?$|" + _LEAD + r"(?:prepare|prep|set\s*up)\s+(?:me\s+)?(?:for\s+)?(?:my|the|a)\s+"
        r"(?:meeting|call)(?P<rest2>(?:,|\s+and\s+).+)?$|" + _LEAD + r"meeting mode(?:\s+on)?$", re.I)),
]

_GOAL_LABEL = {"coding_workspace": "Set up your coding workspace", "run_project": "Start the project",
               "run_tests": "Run the tests", "fix_and_run": "Get the project running",
               "meeting_prep": "Get ready for your meeting"}


def match(text: str) -> Optional[Goal]:
    t = re.sub(r"[.!?]+$", "", (text or "").strip())
    for name, pat in _GOALS:
        m = pat.match(t)
        if not m:
            continue
        g = m.groupdict()
        project = (g.get("project") or g.get("project2") or "").strip()
        project = re.sub(r"^(?:the|my|this)\s+|\s+(?:project|repo|repository|app|code)$", "", project, flags=re.I)
        if project.lower() in ("the", "my", "dev", "this", "it", "local", "development"):
            project = ""
        rest = (g.get("rest") or g.get("rest2") or "").strip(" ,")
        rest = re.sub(r"^(?:and|then)\s+", "", rest, flags=re.I)
        return Goal(name, project, rest, (g.get("url") or "").strip())
    return None


# ---------------------------------------------------------------------- #
def _project(name: str):
    try:
        from modules.dev.project import find_project
        return find_project(name)
    except Exception:
        return None


def _editor_process() -> str:
    editor = (config.get("dev.editor", "code") or "code").lower()
    return {"code": "code", "code-insiders": "code - insiders", "cursor": "cursor", "subl": "sublime_text",
            "notepad++": "notepad++"}.get(editor, editor)


def build(goal: Goal, request: str, observer: Observer) -> Optional[AgentTask]:
    task = AgentTask(request=request, goal=_GOAL_LABEL.get(goal.name, request), source="goal")
    task.context["goal"] = goal.name
    task.note("TASK_START", f"Goal: {task.goal}")
    builder = {"coding_workspace": _coding, "run_project": _run_project, "run_tests": _run_tests,
               "fix_and_run": _fix_and_run, "meeting_prep": _meeting}[goal.name]
    steps = builder(goal, task, observer)
    if steps is None:
        return task if task.result else None             # result set = a reason it can't be done
    task.plan = steps
    return task


def _coding(goal: Goal, task: AgentTask, observer: Observer) -> Optional[List[PlanStep]]:
    p = _project(goal.project)
    steps: List[PlanStep] = []
    if p is None:
        task.note("OBSERVE", "No project named" + (f" {goal.project}" if goal.project else " in Settings") +
                  " — opening the editor only")
        steps.append(PlanStep("open visual studio code", "Opening VS Code",
                              verify={"kind": "process", "name": _editor_process()}))
    else:
        task.context.update(project=p.path, project_name=p.name)
        task.note("OBSERVE", f"Project: {p.name} ({p.kind}) at {p.path}")
        steps.append(PlanStep(f"open the project {p.path}", f"Opening {p.name} in the editor", tool="dev.open_project",
                              args={"name": p.path}, verify={"kind": "process", "name": _editor_process()}))
        if p.start:
            task.reason = f"{p.name} runs with “{p.start}”"
            steps.append(PlanStep(f"start the dev server for {p.path}", "Starting the dev server", tool="dev.start_server",
                                  args={"project": p.path}, risk=Risk.MEDIUM,
                                  verify={"kind": "port", "port": p.port} if p.port else {"kind": "reply"}))
            if p.kind == "node" or p.port:
                steps.append(PlanStep("open {url}", "Opening it in the browser", tool="desktop.open_url",
                                      args={"url": "{url}"}, optional=True,
                                      verify={"kind": "browser_url", "contains": "localhost", "timeout": 6}))
    steps += _extra(goal.rest)
    for cmd in config.get("goals.coding.extra_steps", []) or []:           # the user's own additions
        steps.append(PlanStep(cmd, _label(cmd)))
    return steps


def _run_project(goal: Goal, task: AgentTask, observer: Observer) -> Optional[List[PlanStep]]:
    p = _project(goal.project)
    if p is None:
        task.result = (f"I couldn't find a project called {goal.project}." if goal.project else
                       "I don't know which project to run — say its name, like “run the SAINT project”.")
        return None
    task.context.update(project=p.path, project_name=p.name)
    task.goal = f"Start {p.name}"
    if not p.start:
        task.result = f"I don't know how to run {p.name} — it has no dev or start script and no app file."
        return None
    task.note("OBSERVE", f"{p.name} runs with “{p.start}”" + (f" on port {p.port}" if p.port else ""))
    steps = [PlanStep(f"start the dev server for {p.path}", f"Starting {p.name}", tool="dev.start_server",
                      args={"project": p.path}, risk=Risk.MEDIUM,
                      verify={"kind": "port", "port": p.port} if p.port else {"kind": "reply"})]
    if re.search(r"\b(?:open|show)\b.*\b(?:browser|it|site|page)\b", goal.rest or "", re.I):
        steps.append(PlanStep("open {url}", "Opening it in the browser", tool="desktop.open_url", args={"url": "{url}"},
                              optional=True))
        goal.rest = ""
    return steps + _extra(goal.rest)


def _run_tests(goal: Goal, task: AgentTask, observer: Observer) -> Optional[List[PlanStep]]:
    steps = [PlanStep("run the tests", "Running the tests", tool="dev.test_run", args={}, risk=Risk.MEDIUM,
                      verify={"kind": "reply"})]
    task.context["tests"] = True
    if re.search(r"\bwhy\b|\bwhat failed\b|\bexplain\b", goal.rest or "", re.I):
        task.context["explain_failures"] = True
        goal.rest = ""
    return steps + _extra(goal.rest)


def _fix_and_run(goal: Goal, task: AgentTask, observer: Observer) -> Optional[List[PlanStep]]:
    goal.rest = ""                  # "...fix it, run it and tell me what was wrong" is this goal itself
    steps: List[PlanStep] = []
    if re.search(r"\b(?:download|clone)\b", task.request, re.I):
        steps.append(PlanStep("clone this project", "Downloading the project", tool="dev.clone",
                              args={"url": goal.url}, risk=Risk.MEDIUM, verify={"kind": "reply"}))
        project = "{project}"
    else:
        p = _project(goal.project)
        if p is None:
            task.result = "I don't know which project you mean — open it or say its name."
            return None
        task.context.update(project=p.path, project_name=p.name)
        project = p.path
    task.context["report_cause"] = True
    steps.append(PlanStep("open the project {project}", "Opening the project", tool="dev.open_project",
                          args={"name": project}, optional=True))
    steps.append(PlanStep("start the dev server for {project}", "Starting it", tool="dev.start_server",
                          args={"project": project}, risk=Risk.MEDIUM, verify={"kind": "reply"}))
    return steps + _extra(goal.rest)


_MEETING_APPS = ("microsoft teams", "teams", "zoom", "webex", "discord")


def _meeting(goal: Goal, task: AgentTask, observer: Observer) -> Optional[List[PlanStep]]:
    steps: List[PlanStep] = []
    sp = observer.spotify()
    if sp.ok and (sp.value or {}).get("is_playing"):
        steps.append(PlanStep("pause the music", "Pausing the music", verify={"kind": "spotify", "playing": False},
                              optional=True))
        task.note("OBSERVE", "Music is playing")
    app = config.get("goals.meeting.app", "") or ""
    if not app:
        for name in _MEETING_APPS:
            if observer.app_installed(name).ok:
                app = name
                break
    if app:
        steps.append(PlanStep(f"open {app}", f"Opening {app.title()}", verify={"kind": "window", "name": app}))
    else:
        task.note("OBSERVE", "No meeting app found (Teams, Zoom, Webex, Discord) — set one in Settings")
    steps.append(PlanStep("silent mode for 60 minutes", "Keeping SAINT quiet during the meeting", optional=True,
                          verify={"kind": "none"}))
    return steps + _extra(goal.rest)


# ---------------------------------------------------------------------- #
def _extra(rest: str) -> List[PlanStep]:
    """'open discord and put spotify on my second monitor' -> steps (only ones SAINT understands)."""
    from modules.agent.autonomy.planner import _understood, split_clauses
    from modules.agent.autonomy.verify import infer
    out = []
    for clause in split_clauses(rest or "")[0]:
        if _understood(clause):
            out.append(PlanStep(clause, _label(clause), verify=infer(clause)))
        else:
            import logging
            logging.getLogger("saint.agent.goals").info("goals.extra_clause_skipped %r", clause)
    return out


_GERUNDS = {"open": "Opening", "start": "Starting", "launch": "Launching", "run": "Running", "move": "Moving",
            "put": "Putting", "play": "Playing", "pause": "Pausing", "close": "Closing", "switch": "Switching to",
            "search": "Searching", "go": "Going", "turn": "Turning", "set": "Setting", "mute": "Muting",
            "install": "Installing", "snap": "Snapping", "maximize": "Maximizing", "minimize": "Minimizing",
            "type": "Typing", "send": "Sending", "save": "Saving", "restore": "Restoring", "show": "Showing"}


def _label(command: str) -> str:
    words = (command or "").strip().split()
    if not words:
        return command
    head = _GERUNDS.get(words[0].lower())
    if head:
        rest = " ".join(words[1:] if head != "Switching to" else words[2:] if len(words) > 2 and
                        words[1].lower() == "to" else words[1:])
        return f"{head} {rest}".strip()
    return command[:1].upper() + command[1:]


label = _label

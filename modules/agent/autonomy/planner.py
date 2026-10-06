"""
modules/agent/autonomy/planner.py

Request -> plan, cheapest first:

    1. a learned procedure       "set up my coding workspace" was done before: reuse those exact
                                 steps and checks (modules/learning/procedures.py)
    2. a built-in goal           goals.py ("run the tests", "get ready for my meeting")
    3. the user's own steps      "open my project, start the dev server, open Discord, put Spotify
                                 on my second monitor and tell me when everything is ready" —
                                 split into clauses the router understands (no model needed)
    4. the local model           only for a complex request none of the above covers; every step it
                                 proposes must be a command SAINT understands and be grounded in the
                                 request (the same checks as the learning planner)

``should_task`` decides whether a request becomes an agent task at all: simple requests ("pause
Spotify") and short sequences stay on the existing deterministic path.
"""

import json
import logging
import re
from typing import List, Optional, Tuple

from core.config import config
from modules.agent.autonomy import goals
from modules.agent.autonomy.model import AgentTask, PlanStep
from modules.agent.autonomy.observe import Observer
from modules.agent.autonomy.verify import infer

log = logging.getLogger("saint.agent.planner")

_WHEN_READY = re.compile(r"(?:,?\s*(?:and\s+)?(?:then\s+)?(?:tell|let)\s+me\s+(?:know\s+)?when\s+"
                         r"(?:it'?s|it\s+is|everything(?:'s|\s+is)|that'?s|you'?re|they'?re|all\s+(?:of\s+)?(?:it|that)\s+is)?\s*"
                         r"(?:all\s+)?(?:ready|done|finished|set(?:\s+up)?|running|up)\s*[.!]?)$", re.I)
_SPLIT = re.compile(r",?\s+(?:and\s+then|then|and\s+also|after\s+that|and)\s+|,\s+|;\s+", re.I)
# Words that make a sequence a job worth tracking (it takes time, or must be checked).
_JOBBY = re.compile(r"\b(?:server|tests?|install|build|deploy|project|workspace|download|clone|meeting|"
                    r"set\s*up|get\s+ready|prepare|everything)\b", re.I)


def split_clauses(text: str) -> Tuple[List[str], bool]:
    """('open my project, start the server and tell me when it's ready') ->
    (["open my project", "start the server"], True)."""
    t = re.sub(r"[.!?]+$", "", (text or "").strip())
    ready = bool(_WHEN_READY.search(t))
    t = _WHEN_READY.sub("", t).strip(" ,")
    parts = [p.strip(" ,.") for p in _SPLIT.split(t) if p and p.strip(" ,.")]
    return parts, ready


def _understood(command: str) -> bool:
    try:
        from modules.learning.planner import understood
        return understood(command)
    except Exception:
        return False


def should_task(text: str) -> bool:
    """Cheap, regex-only: is this a job for the agent loop rather than one command?"""
    if goals.match(text) is not None:
        return True
    clauses, ready = split_clauses(text)
    if len(clauses) >= 2 and ready:
        return True
    if len(clauses) >= 3 and _JOBBY.search(text):
        return True
    return len(clauses) >= 5


def from_procedure(text: str) -> Optional[AgentTask]:
    try:
        from modules.learning.procedures import procedures
    except Exception:
        return None
    proc = procedures.match(text)
    if proc is None:
        return None
    task = AgentTask(request=text, goal=proc.goal or text, source="procedure", procedure=proc.id)
    task.plan = [PlanStep(s.action, s.label, tool=s.tool, args=dict(s.args or {}), verify=dict(s.verify or {}),
                          fallback=list(s.fallback or []), risk=s.risk, optional=s.optional) for s in proc.steps]
    task.note("TASK_START", f"Goal: {task.goal}")
    task.note("PLAN", f"Using what I learned (version {proc.version}, used {proc.uses} times)")
    return task


def from_clauses(text: str) -> Optional[AgentTask]:
    clauses, ready = split_clauses(text)
    if not clauses:
        return None
    unknown = [c for c in clauses if not _understood(c)]
    if unknown:
        log.info("planner.clauses_unknown %r", unknown)
        return None
    task = AgentTask(request=text, goal=text[:1].upper() + text[1:], source="steps")
    task.plan = [PlanStep(c, goals.label(c), verify=infer(c)) for c in clauses]
    task.context["announce_ready"] = ready
    task.note("TASK_START", f"Goal: {task.goal}")
    return task


_SYSTEM = """You plan tasks for SAINT, a voice assistant that controls a Windows PC.
Break the user's goal into at most 8 SAINT commands, in order, using ONLY the command shapes listed.
For each step give what should be true afterwards, and how to check it:
{"goal": "<short goal>", "steps": [{"do": "<command>", "expect": "<what is true after>",
 "check": {"kind": "window|process|port|http|file|monitor|spotify|browser_url|reply", ...}}]}
Check kinds: {"kind":"window","name":"Discord"}, {"kind":"port","port":5173}, {"kind":"monitor","name":"Spotify",
"monitor":"second"}, {"kind":"spotify","playing":true}, {"kind":"reply"}.
Never invent apps, files or text the user didn't mention. If it can't be done with these commands, reply {"steps": []}.
Answer with JSON only."""


def from_model(text: str, observer: Observer) -> Optional[AgentTask]:
    """The model writes the plan; each step is validated before anything runs."""
    if not config.get("learning.planner", True) or not config.get("agent.model_planning", True):
        return None
    try:
        from modules.learning import planner as lp
        from modules.learning.catalog import as_prompt
    except Exception:
        return None
    prompt = (f"Commands SAINT understands:\n{as_prompt()}\n\nRight now: {observer.snapshot()[:400]}\n\n"
              f"Goal: {lp.clean(text)}")
    data = lp._ask_with(_SYSTEM, prompt, num_predict=400) or {}
    raw = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        return None
    steps: List[PlanStep] = []
    for item in raw[:8]:
        if isinstance(item, str):
            item = {"do": item}
        if not isinstance(item, dict):
            continue
        cmd = re.sub(r"\s+", " ", str(item.get("do") or "")).strip(" .")
        if not cmd:
            continue
        if not lp.understood(cmd) or not lp.grounded(cmd, text):
            log.info("planner.model_step_rejected %r", cmd)
            return None                         # one bad step: don't run a half-plan
        check = item.get("check") if isinstance(item.get("check"), dict) else infer(cmd)
        if check.get("kind") not in ("window", "foreground", "process", "port", "http", "file", "monitor",
                                     "spotify", "browser_url", "reply", "none"):
            check = infer(cmd)
        steps.append(PlanStep(cmd, goals.label(cmd), expect=str(item.get("expect") or "")[:120], verify=check))
    if not steps:
        return None
    task = AgentTask(request=text, goal=str(data.get("goal") or text)[:120], source="model")
    task.plan = steps
    task.note("TASK_START", f"Goal: {task.goal}")
    task.note("PLAN", "Planned with the local model: " + "; ".join(s.action for s in steps))
    log.info("planner.model %r -> %s", text[:80], json.dumps([s.action for s in steps]))
    return task


def plan(text: str, observer: Observer, allow_model: bool = False) -> Optional[AgentTask]:
    """The plan for ``text``, or None when it isn't a task (the normal path handles it)."""
    task = from_procedure(text)
    if task is not None:
        return task
    g = goals.match(text)
    if g is not None:
        return goals.build(g, text, observer)
    if should_task(text):
        task = from_clauses(text)
        if task is not None:
            return task
        if allow_model:
            return from_model(text, observer)
    return None

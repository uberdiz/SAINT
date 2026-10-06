"""
modules/agent/autonomy/recover.py

A step failed. Before telling the user "I couldn't", work out why and whether SAINT can fix it:

    app not found        re-scan the installed apps (Start menu, Start apps, Program Files) and
                         retry with the real name ("discrod" -> Discord)
    window / app missing open the app first, then retry ("move Spotify to my second monitor"
                         with Spotify closed)
    Spotify has no device  open Spotify, then retry
    not in front / slow  wait for the UI, look again, retry once
    server exited        read its output: missing packages -> install them (if allowed), port
                         busy -> check whether it's already running
    anything else        one compact question to the local model: the goal, the failed step, the
                         error and what's on screen -> one replacement command, which must be a
                         command SAINT understands and be grounded in what the user asked

Every recovery is bounded (executor.py: attempts per step, recoveries per task) — a task can't
loop. ``None`` means nothing more to try: the task fails with an honest reason.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

from core.config import config
from modules.agent.autonomy.model import AgentTask, PlanStep, Risk
from modules.agent.autonomy.observe import Observer
from modules.agent.autonomy.verify import Verdict

log = logging.getLogger("saint.agent.recover")

APP_NOT_FOUND = "app_not_found"
APP_NOT_RUNNING = "app_not_running"
NO_DEVICE = "no_device"
NOT_VERIFIED = "not_verified"
NEEDS_USER = "needs_user"
NOT_ALLOWED = "not_allowed"
UNAVAILABLE = "unavailable"
PROCESS_EXITED = "process_exited"
UNKNOWN = "unknown"


@dataclass
class Recovery:
    kind: str                                   # replace | before | retry | give_up
    message: str                                # for the trail: what SAINT is doing about it
    steps: List[PlanStep] = field(default_factory=list)
    wait: float = 0.0
    learnable: bool = False                     # a replacement worth remembering in the procedure


def classify(step: PlanStep, reply_text: str, verdict: Optional[Verdict]) -> str:
    t = (reply_text or "").lower()
    if re.search(r"couldn'?t find an app|no app called|isn'?t installed|not installed", t):
        return APP_NOT_FOUND
    if re.search(r"isn'?t playing on any device|no active device|open spotify|spotify isn'?t open", t):
        return NO_DEVICE
    if re.search(r"not allowed|blocked in settings|permission|gaming mode|anti-?cheat", t):
        return NOT_ALLOWED
    if re.search(r"isn'?t (?:connected|set up|enabled|running at)|ollama isn'?t|no internet|offline", t):
        return UNAVAILABLE
    if re.search(r"exited with|crashed|stopped with code|server stopped", t):
        return PROCESS_EXITED
    if re.search(r"(?:couldn'?t|can'?t|could not|cannot) find .{0,40}\bwindow|no .{0,40} window|isn'?t open|"
                 r"not running|isn'?t running|nothing called", t):
        return APP_NOT_RUNNING
    if verdict is not None and not verdict.ok and step.status != "failed_action":
        return NOT_VERIFIED
    return UNKNOWN


def _target_app(action: str) -> str:
    """The app a command is about: "move spotify to my second monitor" -> "spotify"."""
    a = (action or "").lower().strip()
    for pat in (r"^(?:open|launch|start|switch to|focus|close|maximi[sz]e|minimi[sz]e|snap|bring up)\s+"
                r"(?:the\s+|my\s+)?(.+?)(?:\s+(?:to|on|onto)\s+.+)?$",
                r"^(?:move|put|send|place)\s+(?:the\s+|my\s+)?(.+?)\s+(?:to|on|onto)\s+.+$"):
        m = re.match(pat, a)
        if m:
            return re.sub(r"\s+(?:app|window)$", "", m.group(1)).strip()
    return ""


def _find_installed(name: str) -> Optional[str]:
    """The installed app ``name`` most likely means, after a fresh scan."""
    try:
        from modules.desktop.apps import app_catalog
        app_catalog.refresh()
        entry = app_catalog.resolve(name)
        if entry is not None:
            return entry.name
        hints = app_catalog.suggestions(name, 1)
        if hints:
            import difflib
            if difflib.SequenceMatcher(None, name.lower(), hints[0].lower()).ratio() >= 0.6:
                return hints[0]
    except Exception as e:
        log.debug("recover.catalog_failed %s", e)
    # Programs that never made a shortcut: look for the executable in the usual install folders.
    roots = [os.environ.get("LOCALAPPDATA", ""), os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
             os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")]
    want = re.sub(r"[^a-z0-9]", "", name.lower())
    if len(want) < 3:
        return None
    for root in roots:
        try:
            for d in os.listdir(root) if root and os.path.isdir(root) else []:
                if re.sub(r"[^a-z0-9]", "", d.lower()) == want:
                    folder = os.path.join(root, d)
                    for f in os.listdir(folder):
                        if f.lower().endswith(".exe") and re.sub(r"[^a-z0-9]", "", f.lower()[:-4]) == want:
                            return os.path.join(folder, f)
        except OSError:
            continue
    return None


def recover(task: AgentTask, step: PlanStep, reply_text: str, verdict: Optional[Verdict],
            observer: Observer, used: int) -> Optional[Recovery]:
    """One recovery for ``step``; ``used`` = recoveries already tried for it."""
    # The plan's own fallbacks first, in order.
    if used < len(step.fallback):
        alt = step.fallback[used]
        return Recovery("replace", f"Trying another way: {alt}", [PlanStep(alt, label=step.label or alt,
                        verify=step.verify, risk=step.risk)], learnable=True)
    kind = classify(step, reply_text, verdict)
    app = _target_app(step.action)
    if kind in (NOT_ALLOWED, NEEDS_USER):
        return None
    if kind == APP_NOT_FOUND and app and used < len(step.fallback) + 1:
        found = _find_installed(app)
        if found and found.lower() != app.lower():
            new = re.sub(re.escape(app), found, step.action, count=1, flags=re.I)
            return Recovery("replace", f"{app} isn't an app name here — found {found}",
                            [PlanStep(new, label=step.label, verify=_swap_name(step.verify, app, found),
                                      risk=step.risk)], learnable=True)
        return None
    if kind == NO_DEVICE and used == 0:
        return Recovery("before", "Spotify isn't open on any device — opening it",
                        [PlanStep("open spotify", label="Opening Spotify", verify={"kind": "window", "name": "spotify"})],
                        wait=3.0)
    if kind == APP_NOT_RUNNING and app and used == 0 and not step.action.lower().startswith(("open", "launch")):
        return Recovery("before", f"{app} isn't open — opening it first",
                        [PlanStep(f"open {app}", label=f"Opening {app}", verify={"kind": "window", "name": app})],
                        wait=1.0)
    if kind == NOT_VERIFIED and used == 0:
        if verdict is not None and verdict.checked == "foreground" and app:
            return Recovery("before", f"{app} opened behind something — bringing it to the front",
                            [PlanStep(f"switch to {app}", label=f"Switching to {app}")])
        return Recovery("retry", "That didn't take effect yet — looking again", wait=1.5)
    if kind == PROCESS_EXITED and used == 0:
        fix = _server_fix(task, reply_text)
        if fix is not None:
            return fix
    if used <= len(step.fallback) + 1 and task.retries < int(config.get("agent.max_model_recoveries", 2)) + 2:
        return _ask_model(task, step, reply_text, verdict, observer)
    return None


def _swap_name(spec, old: str, new: str):
    if not spec:
        return spec
    spec = dict(spec)
    if str(spec.get("name", "")).lower() == old.lower():
        spec["name"] = os.path.splitext(os.path.basename(new))[0] if os.path.isabs(new) else new
    return spec


def _server_fix(task: AgentTask, output: str) -> Optional[Recovery]:
    """A dev server that exited: the deterministic fixes (modules/dev/project.py diagnoses)."""
    try:
        from modules.dev.project import diagnose
    except Exception:
        return None
    d = diagnose(output, task.context.get("project", ""))
    if d is None:
        return None
    if d.kind == "missing_dependencies":
        return Recovery("before", d.reason,
                        [PlanStep("install the project dependencies", label="Installing dependencies",
                                  inputs={"project": task.context.get("project", "")}, risk=Risk.MEDIUM,
                                  verify={"kind": "reply"})], learnable=False)
    if d.kind == "python_module" and d.detail:
        return Recovery("before", d.reason,
                        [PlanStep(f"install the python package {d.detail}", label=f"Installing {d.detail}",
                                  inputs={"project": task.context.get("project", "")}, risk=Risk.MEDIUM)])
    if d.kind == "port_in_use":
        return Recovery("retry", d.reason, wait=0.5)
    return None


_SYSTEM = """You help SAINT, a voice assistant controlling a Windows PC, recover from a failed step.
Reply with JSON only: {"action": "<one SAINT command>", "why": "<short reason>"}.
The command must use one of the command shapes listed. Use names that appear in the goal, the step,
the error or the screen. If nothing in the list can fix it, reply {"action": "", "why": "<reason>"}."""


def _ask_model(task: AgentTask, step: PlanStep, reply_text: str, verdict: Optional[Verdict],
               observer: Observer) -> Optional[Recovery]:
    """The compact reasoning call: goal + failed step + error + a one-line observation. Never the
    conversation, never the whole state. The answer is validated like a planned step."""
    if not config.get("learning.planner", True) or not config.get("agent.model_recovery", True):
        return None
    try:
        from modules.learning import planner
        from modules.learning.catalog import as_prompt
    except Exception:
        return None
    prompt = (f"Commands SAINT understands:\n{as_prompt()}\n\n"
              f"Goal: {task.goal or task.request}\n"
              f"Failed step: {step.action}\n"
              f"What happened: {(reply_text or '')[:200]}"
              + (f" / check: {verdict.evidence}" if verdict is not None and verdict.evidence else "") + "\n"
              f"On screen: {observer.snapshot()[:300]}\n")
    data = planner._ask_with(_SYSTEM, prompt, num_predict=120)
    action = re.sub(r"\s+", " ", str((data or {}).get("action") or "")).strip(" .")
    why = str((data or {}).get("why") or "")[:160]
    task.retries += 1
    if not action or action.lower() == step.action.lower():
        log.info("recover.model none why=%r", why)
        return None
    if not planner.understood(action) or not planner.grounded(action, f"{task.request} {step.action}"):
        log.info("recover.model rejected %r", action)
        return None
    log.info("recover.model %r -> %r (%s)", step.action, action, why)
    return Recovery("replace", f"Trying another way: {action}" + (f" ({why})" if why else ""),
                    [PlanStep(action, label=step.label or action, verify=step.verify, risk=step.risk)],
                    learnable=True)

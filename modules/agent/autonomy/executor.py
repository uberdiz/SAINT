"""
modules/agent/autonomy/executor.py

Runs one AgentTask, step by step:

    for each step:
        permission  policy.decide(risk): run / ask once for the plan / ask for this step / not allowed
        act         a built-in tool, or a SAINT command through the router (the same permission checks
                    and confirmations as speech)
        wait        the action asked something ("Which window?"): park the task, resume on the answer
        verify      verify.py — did the expected result happen?
        recover     recover.py — another way, bounded (MAX_STEP_RECOVERIES per step,
                    MAX_TASK_RECOVERIES per task, a wall-clock limit)
    complete        a short honest summary; the manager speaks it and learns the procedure

Pause / resume / cancel are checked between steps and while waiting; ``hold`` (the user is talking)
makes the runner wait at the next step boundary so a new instruction can't race a running step.
The task never loops forever: every retry consumes a budget and the plan can only grow so much.
"""

import contextlib
import logging
import re
import threading
import time
from typing import Any, Dict, Optional

from core.config import config
from modules.agent.autonomy import policy
from modules.agent.autonomy.model import AgentTask, PlanStep, StepStatus, TaskStatus
from modules.agent.autonomy.observe import Observer, observer as default_observer
from modules.agent.autonomy.recover import recover
from modules.agent.autonomy.verify import infer, verify

log = logging.getLogger("saint.agent.task")

MAX_STEP_RECOVERIES = 2           # per step (3 attempts)
MAX_TASK_RECOVERIES = 6
MAX_PLAN_STEPS = 24               # plan + inserted recovery steps
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_FAILED_WORDS = re.compile(r"\b(?:couldn'?t|can'?t|didn'?t|failed|not allowed|isn'?t|won'?t|error)\b", re.I)


class Runner:
    def __init__(self, task: AgentTask, manager, observer: Optional[Observer] = None):
        self.task = task
        self.manager = manager
        self.observer = observer or default_observer
        self.cancel_evt = threading.Event()
        self.pause_evt = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._recoveries: Dict[str, int] = {}
        limit = float(config.get("agent.task_timeout_sec", 900))
        self.deadline = time.time() + limit

    # ---- control ------------------------------------------------------------------------------
    def start(self, background: bool = True):
        if background:
            self.thread = threading.Thread(target=self.run, daemon=True, name=f"task-{self.task.id}")
            self.thread.start()
        else:
            self.run()

    def cancel(self):
        self.cancel_evt.set()

    def pause(self):
        self.pause_evt.set()

    @property
    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _sleep(self, secs: float):
        end = time.time() + secs
        while time.time() < end and not self.cancel_evt.is_set():
            time.sleep(0.05)

    # ---- the loop -------------------------------------------------------------------------------
    def run(self):
        try:
            self._run()
        except Exception as e:                                   # never leave a task "running" forever
            log.exception("task.crashed %s", self.task.id)
            self.task.note("ERROR", f"Something went wrong inside SAINT: {e}")
            self.task.result = "Something went wrong while I was working on that, so I stopped."
            self.manager.set_status(self.task, TaskStatus.FAILED)
        finally:
            self.manager.runner_finished(self)

    def _run(self):
        t, m = self.task, self.manager
        if not self._permission_gate():
            return
        while True:
            if self.cancel_evt.is_set():
                return m.set_status(t, TaskStatus.CANCELLED)
            if self.pause_evt.is_set():
                t.note("PAUSE", "Paused")
                return m.set_status(t, TaskStatus.PAUSED)
            m.wait_while_user_talks(self)
            if time.time() > self.deadline:
                t.result = f"That was taking too long, so I stopped — {self._done_text()}"
                t.note("FAILED", "Time limit reached")
                return m.set_status(t, TaskStatus.FAILED)
            i = next((k for k, s in enumerate(t.plan) if s.status == StepStatus.PENDING), None)
            if i is None:
                return self._complete()
            outcome = self._step(i)
            if outcome in ("waiting", "failed", "cancelled"):
                return

    def _permission_gate(self) -> bool:
        """Safe mode: refuse up front what it can't do. Confirm mode: one question for the whole
        plan when it has MEDIUM steps (asked by the manager; the answer restarts the runner)."""
        t = self.task
        mode = policy.mode()
        blocked = [s for s in t.plan if s.status == StepStatus.PENDING and not s.optional
                   and policy.decide(s.risk, mode) == policy.DENY]
        if blocked:
            names = ", ".join(s.title().lower() for s in blocked[:3])
            t.result = (f"In Safe mode I can't {names}. Switch to Confirm or Autonomous in Settings › Permissions "
                        f"and ask again.")
            t.note("FAILED", f"Not allowed in Safe mode: {names}")
            self.manager.set_status(t, TaskStatus.FAILED)
            return False
        needs_plan_ok = any(policy.decide(s.risk, mode) == policy.CONFIRM_PLAN for s in t.plan
                            if s.status == StepStatus.PENDING)
        if needs_plan_ok and not t.context.get("plan_approved"):
            self.manager.ask_plan_approval(t)
            return False
        return True

    # ---- one step -------------------------------------------------------------------------------
    def _step(self, i: int) -> str:
        t, m = self.task, self.manager
        step = t.plan[i]
        t.current = i
        if policy.decide(step.risk) == policy.CONFIRM_STEP and not step.inputs.get("approved"):
            m.ask_step_approval(t, i)
            return "waiting"
        answered = step.inputs.pop("answered", None)
        if answered is not None:
            # The user answered the question this step asked: it ran; now check it worked.
            reply_text, reply_ok, result = answered, not _FAILED_WORDS.search(answered or ""), None
        else:
            step.status = StepStatus.RUNNING
            step.attempts += 1
            m.set_status(t, TaskStatus.EXECUTING)
            action = self._fill(step.action)
            t.note("ACTION", step.title() if action == step.action else f"{step.title()} ({action})", i)
            m.trail_changed(t)
            approve = policy.approved_step() if step.inputs.get("approved") else contextlib.nullcontext()
            with approve:
                reply_text, reply_ok, asks, result = self._do(step, action)
            step.result = (reply_text or "")[:300]
            if self.cancel_evt.is_set():
                step.status = StepStatus.PENDING
                m.set_status(t, TaskStatus.CANCELLED)
                return "cancelled"
            if asks:
                m.park_for_answer(t, i, reply_text)
                return "waiting"
            self._absorb(step, result)
        m.set_status(t, TaskStatus.VERIFYING)
        spec = step.verify or infer(self._fill(step.action))
        verdict = verify(spec, reply_ok, self.observer, cancelled=self.cancel_evt.is_set)
        t.note("VERIFY", verdict.evidence, i)
        if verdict.ok:
            step.status, step.evidence = StepStatus.DONE, verdict.evidence[:200]
            m.trail_changed(t)
            return "done"
        # ---- it didn't work: recover, within budget -------------------------------------------------
        reason = (reply_text if not reply_ok else verdict.evidence) or "it didn't work"
        t.note("ERROR", reason, i)
        t.failures.append(f"{step.title()}: {reason}"[:240])
        used = self._recoveries.get(step.id, 0)
        if used >= MAX_STEP_RECOVERIES or t.retries >= MAX_TASK_RECOVERIES or len(t.plan) >= MAX_PLAN_STEPS:
            return self._give_up(i, reason)
        m.set_status(t, TaskStatus.RECOVERING)
        rec = recover(t, step, reply_text, verdict, self.observer, used)
        self._recoveries[step.id] = used + 1
        if rec is None:
            return self._give_up(i, reason)
        t.retries += 1
        t.reason = rec.message
        t.note("RECOVERY", rec.message, i)
        m.trail_changed(t)
        if rec.wait:
            self._sleep(rec.wait)
        if rec.kind == "replace" and rec.steps:
            new = rec.steps[0]
            if not step.replaced:
                step.replaced = step.action
            step.action, step.tool, step.args = new.action, new.tool, dict(new.args)
            step.verify = new.verify or step.verify
            step.note = rec.message
            if rec.learnable:
                t.context.setdefault("replaced_steps", []).append(i)
        elif rec.kind == "before":
            for k, s in enumerate(rec.steps):
                s.inputs["recovery"] = True
                t.plan.insert(i + k, s)
            self._recoveries[rec.steps[0].id] = MAX_STEP_RECOVERIES      # a recovery step isn't recovered itself
        step.status = StepStatus.PENDING
        return "recovering"

    def _give_up(self, i: int, reason: str) -> str:
        t, step = self.task, self.task.plan[i]
        if step.optional:
            step.status, step.note = StepStatus.SKIPPED, reason[:200]
            t.note("NOTE", f"Skipped {step.title().lower()}: {reason}", i)
            return "done"
        step.status, step.note = StepStatus.FAILED, reason[:200]
        t.result = (f"I couldn't finish {t.goal[:1].lower() + t.goal[1:] if t.goal else 'that'}: "
                    f"{step.title().lower()} didn't work — {_short(reason)}. {self._done_text()}"
                    "Say “why did that fail?” or “continue” to try again.").replace("  ", " ")
        t.note("FAILED", f"{step.title()}: {reason}", i)
        self.manager.set_status(t, TaskStatus.FAILED)
        return "failed"

    def _done_text(self) -> str:
        done = [s.title().lower() for s in self.task.plan if s.status == StepStatus.DONE]
        if not done:
            return ""
        return "Done so far: " + ", ".join(done[:4]) + ". "

    # ---- doing --------------------------------------------------------------------------------------
    def _do(self, step: PlanStep, action: str):
        """(reply text, ok, asks a question, tool result)"""
        try:
            if step.tool:
                return self._do_tool(step)
            from modules.agent.meta import match_meta, run_meta
            meta = match_meta(action)
            if meta is not None and meta.kind not in ("stop", "stop_all", "status"):
                return run_meta(meta), True, False, None
            from modules.agent.router import route
            intent = route(action)
            if intent is None:
                from modules.learning.skills import run_steps, skills
                sk = skills.match(action)
                if sk is not None:
                    r = run_steps(sk.steps)
                    if r is not None:
                        return r.text, r.ok, r.expects_reply, None
                return f"I don't know how to “{action}”.", False, False, None
            r = intent.run()
            return r.text, r.ok, r.expects_reply, None
        except Exception as e:
            log.exception("task.step_error %s", action)
            return f"Something went wrong: {e}", False, False, None

    def _do_tool(self, step: PlanStep):
        from modules.agent.confirm import PendingAction, confirmations
        from modules.automation.tools import get_tool_registry
        reg = get_tool_registry()
        args = {k: self._fill(v) for k, v in (step.args or {}).items()}
        res = reg.execute(step.tool, _cancel_event=self.cancel_evt, **args)
        if res.error_code == "CONFIRM_REQUIRED" and policy.preapproved(step.tool):
            res = reg.execute(step.tool, _confirmed=True, _cancel_event=self.cancel_evt, **args)
        if res.error_code == "CONFIRM_REQUIRED":
            def run_confirmed(tool=step.tool, a=args):
                r2 = reg.execute(tool, _confirmed=True, **a)
                return _describe(step, r2.result) if r2.success else (r2.error or f"I couldn't {step.title().lower()}.")
            confirmations.ask(PendingAction(description=step.title().lower(), run=run_confirmed, tool=step.tool))
            return f"Do you want me to {step.title().lower()}?", True, True, None
        if not res.success:
            return res.error or f"I couldn't {step.title().lower()}.", False, False, res.result
        return _describe(step, res.result), True, False, res.result

    def _fill(self, value: Any) -> Any:
        """'{url}' -> what an earlier step found (the dev server's address, the cloned folder)."""
        if not isinstance(value, str) or "{" not in value:
            return value
        ctx = self.task.context
        return _PLACEHOLDER.sub(lambda m: str(ctx.get(m.group(1), m.group(0))), value)

    def _absorb(self, step: PlanStep, result):
        if not isinstance(result, dict):
            return
        ctx = self.task.context
        if result.get("url"):
            ctx["url"] = result["url"]
        if step.tool in ("dev.clone", "dev.open_project") and result.get("path"):
            ctx["project"] = result["path"]
            ctx["project_name"] = result.get("name", ctx.get("project_name", ""))
        if step.tool == "dev.test_run":
            ctx["test_result"] = {k: result.get(k) for k in ("summary", "passed", "failures", "exit_code")}

    # ---- the end ---------------------------------------------------------------------------------------
    def _complete(self):
        t = self.task
        t.current = -1
        t.result = summary(t)
        t.note("COMPLETE", t.result)
        self.manager.set_status(t, TaskStatus.COMPLETED)


def _short(text: str) -> str:
    text = (text or "").strip().split("\n")[0]
    return text[:160].rstrip(" .")


def _describe(step: PlanStep, result) -> str:
    r = result if isinstance(result, dict) else {}
    if step.tool == "dev.start_server":
        return f"{r.get('project', 'The project')} is running" + (f" at {r['url']}" if r.get("url") else "") + "."
    if step.tool == "dev.test_run":
        return f"The tests {'passed' if r.get('passed') else 'failed'} — {r.get('summary', '')}."
    if step.tool == "dev.open_project":
        return f"Opened {r.get('name', 'the project')}."
    if step.tool == "dev.clone":
        return f"Downloaded {r.get('name', 'the project')}."
    return f"{step.title()} — done."


def summary(t: AgentTask) -> str:
    """The one or two sentences SAINT says when a task finishes."""
    ctx = t.context
    skipped = [s for s in t.plan if s.status == StepStatus.SKIPPED]
    recovered = [s for s in t.plan if s.replaced or s.inputs.get("recovery")]
    notes = []
    for s in skipped[:2]:
        notes.append(f"I skipped {s.title().lower()} ({_short(s.note)})")
    if ctx.get("tests"):
        tr = ctx.get("test_result") or {}
        if tr.get("passed"):
            text = f"The tests passed — {tr.get('summary', '')}."
        else:
            first = (tr.get("failures") or [{}])[0]
            text = f"The tests failed — {tr.get('summary', '')}."
            if first.get("test"):
                text += f" First failure: {first['test'].split('::')[-1]}" + (f" — {first['error']}" if first.get("error") else "") + "."
            text += " Say “why did the tests fail?” for more."
        return text
    if ctx.get("report_cause") and t.failures:
        cause = _short(t.failures[0].split(": ", 1)[-1])
        fixes = [s.title().lower() for s in t.plan if s.inputs.get("recovery") and s.status == StepStatus.DONE]
        url = ctx.get("url")
        text = f"It wasn't starting: {cause}."
        if fixes:
            text += f" I fixed it by {', then '.join(fixes)}."
        return text + (f" It's running at {url}." if url else " It's running now.")
    goal = t.goal or "that"
    if t.source == "goal" and ctx.get("goal") in ("coding_workspace", "meeting_prep"):
        what = "your coding workspace is ready" if ctx["goal"] == "coding_workspace" else "you're ready for your meeting"
        text = f"Done — {what}."
        if ctx.get("url"):
            text += f" The app is at {ctx['url']}."
    elif ctx.get("announce_ready"):
        text = "Everything's ready."
    elif ctx.get("goal") == "run_project" and ctx.get("url"):
        text = f"{ctx.get('project_name') or 'It'} is running at {ctx['url']}."
    else:
        text = "Done."
    if recovered and not ctx.get("report_cause"):
        fixed = recovered[0]
        text += f" (I had to {fixed.title().lower()} first.)" if fixed.inputs.get("recovery") else \
            f" (“{fixed.replaced}” didn't work, so I {fixed.action} instead.)"
    if notes:
        text += " " + "; ".join(notes) + "."
    return text

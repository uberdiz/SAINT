"""
modules/agent/autonomy/manager.py

Owns SAINT's tasks: the one running now, the ones paused or waiting for an answer, and the last
few finished (data/agent_tasks.json). It is the only thing the rest of SAINT talks to:

    agent.handle()        -> try_start(text)            a request that is a task starts here
    voice control         -> pause / resume / stop / describe / where / next / failure / redo /
                             remember / modify            (control.py parses the words)
    the runner            -> set_status / trail_changed / park_for_answer / ask_*_approval
    the UI                -> snapshot() and EventType.AGENT_TASK events

A new task while one runs pauses the old one and resumes it afterwards. Talking to SAINT while a
task runs makes the task wait at its next step (``user_turn``), so a new instruction never races a
step that's halfway through. Finished tasks are learned as procedures (modules/learning/procedures).
"""

import contextlib
import json
import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional

from core.config import config
from core.events import event_bus, EventType
from core.paths import data_path
from modules.agent.autonomy import planner, policy
from modules.agent.autonomy.model import AgentTask, PlanStep, StepStatus, TaskStatus
from modules.agent.autonomy.observe import observer

log = logging.getLogger("saint.agent.tasks")
trail_log = logging.getLogger("saint.agent.trail")

KEEP = 30
RESUME_MAX_AGE = 6 * 3600
REMEMBER_MAX_AGE = 15 * 60


def _gerund(goal: str) -> str:
    """'Set up your coding workspace' -> 'Setting up your coding workspace'."""
    words = (goal or "").split()
    if not words:
        return "Working on it"
    first = words[0].lower()
    irregular = {"set": "Setting", "get": "Getting", "run": "Running", "put": "Putting", "stop": "Stopping",
                 "start": "Starting", "open": "Opening", "make": "Making", "find": "Finding", "use": "Using"}
    g = irregular.get(first) or (first[:-1] + "ing" if first.endswith("e") and not first.endswith("ee")
                                 else first + "ing").capitalize()
    return " ".join([g] + words[1:])


class AgentTaskManager:
    def __init__(self, path=None):
        self._path = path
        self._lock = threading.RLock()
        self._tasks: List[AgentTask] = []
        self._runners: Dict[str, object] = {}
        self._stack: List[str] = []              # tasks paused by a newer one, resumed after it
        self._talking = 0
        self._loaded = False

    # ---- persistence -------------------------------------------------------------------------------
    def _file(self):
        return self._path or data_path("agent_tasks.json")

    def load(self):
        with self._lock:
            self._loaded = True
            try:
                data = json.loads(open(self._file(), encoding="utf-8").read())
                self._tasks = [AgentTask.from_dict(d) for d in data.get("tasks", []) if isinstance(d, dict)][-KEEP:]
            except FileNotFoundError:
                self._tasks = []
            except Exception:
                log.warning("tasks.load_failed", exc_info=True)
                self._tasks = []
            for t in self._tasks:                # SAINT restarted mid-task: it's paused there
                if t.status in TaskStatus.ACTIVE or t.status == TaskStatus.WAITING_FOR_USER:
                    t.status = TaskStatus.PAUSED
                    t.note("PAUSE", "SAINT restarted")
                    for s in t.plan:
                        if s.status == StepStatus.RUNNING:
                            s.status = StepStatus.PENDING

    def _ensure(self):
        if not self._loaded:
            self.load()

    def _save(self):
        try:
            data = {"version": 1, "tasks": []}
            for t in self._tasks[-KEEP:]:
                d = t.to_dict()
                d["trail"] = d["trail"][-80:]
                data["tasks"].append(d)
            path = str(self._file())
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, path)
        except Exception:
            log.warning("tasks.save_failed", exc_info=True)

    # ---- queries -------------------------------------------------------------------------------------
    def all(self) -> List[AgentTask]:
        with self._lock:
            self._ensure()
            return list(self._tasks)

    def get(self, task_id: str) -> Optional[AgentTask]:
        with self._lock:
            self._ensure()
            return next((t for t in reversed(self._tasks) if t.id == task_id), None)

    def running(self) -> Optional[AgentTask]:
        with self._lock:
            self._ensure()
            return next((t for t in reversed(self._tasks) if t.status in TaskStatus.ACTIVE), None)

    def current(self) -> Optional[AgentTask]:
        """The task SAINT is working on, waiting on, or paused in (recent), else None."""
        with self._lock:
            self._ensure()
            for t in reversed(self._tasks):
                if t.status in TaskStatus.ACTIVE or t.status == TaskStatus.WAITING_FOR_USER:
                    return t
            for t in reversed(self._tasks):
                if t.status == TaskStatus.PAUSED and time.time() - t.updated < RESUME_MAX_AGE:
                    return t
        return None

    def last(self, statuses=None) -> Optional[AgentTask]:
        with self._lock:
            self._ensure()
            for t in reversed(self._tasks):
                if statuses is None or t.status in statuses:
                    return t
        return None

    def snapshot(self) -> Dict:
        cur = self.current()
        return {"current": cur.summary() if cur else None,
                "recent": [t.summary() for t in reversed(self.all()[-12:])]}

    # ---- starting ---------------------------------------------------------------------------------------
    def try_start(self, text: str, allow_model: bool = False) -> Optional[Dict]:
        """If ``text`` is a task, start it and return {"text", "expects_reply"} for this turn."""
        if not config.get("agent.tasks", True):
            return None
        try:
            task = planner.plan(text, observer, allow_model=allow_model)
        except Exception:
            log.exception("tasks.plan_failed %r", text[:80])
            return None
        if task is None:
            return None
        return self.start(task)

    def start(self, task: AgentTask, background: bool = True) -> Dict:
        with self._lock:
            self._ensure()
            if not task.plan:
                task.status = TaskStatus.FAILED
                task.result = task.result or "I couldn't work out the steps for that."
                self._tasks.append(task)
                self._save()
                self._emit(task)
                return {"text": task.result, "expects_reply": False, "ok": False}
            running = self.running()
            if running is not None and running.id != task.id:
                self._pause_for(running, task)
            task.background = background
            self._tasks.append(task)
            del self._tasks[:-KEEP]
        log.info("task.start %s %r source=%s steps=%s", task.id, task.goal, task.source,
                 [s.action for s in task.plan])
        self._trail_line(task, task.trail[-1] if task.trail else None)
        gate = self._gate(task)
        if gate is not None:
            return gate
        self._launch(task, background)
        n = len(task.plan)
        return {"text": f"{_gerund(task.goal)}." if task.source == "goal" or task.source == "procedure"
                else f"On it — {n} step{'s' if n != 1 else ''}.", "expects_reply": False, "ok": True,
                "task": task.id}

    def _gate(self, task: AgentTask) -> Optional[Dict]:
        """Permission checks before anything runs (policy.py): a plain no in Safe mode, one
        question for the plan in Confirm mode."""
        mode = policy.mode()
        blocked = [s for s in task.plan if not s.optional and policy.decide(s.risk, mode) == policy.DENY]
        if blocked:
            names = ", ".join(s.title().lower() for s in blocked[:3])
            task.result = (f"In Safe mode I can't {names}. Switch to Confirm or Autonomous in Settings › "
                           f"Permissions and ask again.")
            task.note("FAILED", f"Not allowed in Safe mode: {names}")
            self.set_status(task, TaskStatus.FAILED, announce=False)
            return {"text": task.result, "expects_reply": False, "ok": False, "task": task.id}
        if any(policy.decide(s.risk, mode) == policy.CONFIRM_PLAN for s in task.plan) and \
                not task.context.get("plan_approved"):
            question = self._plan_question(task)
            self._ask(task, question, lambda: self._approve_plan(task))
            return {"text": question, "expects_reply": True, "ok": True, "task": task.id}
        return None

    def _plan_question(self, task: AgentTask) -> str:
        titles = [s.title().lower() for s in task.plan]
        listed = ", ".join(titles[:5]) + (f" and {len(titles) - 5} more" if len(titles) > 5 else "")
        return f"{_gerund(task.goal)} means: {listed}. Go ahead?"

    def _launch(self, task: AgentTask, background: bool = True):
        from modules.agent.autonomy.executor import Runner
        runner = Runner(task, self)
        with self._lock:
            self._runners[task.id] = runner
        task.status = TaskStatus.EXECUTING
        self._emit(task)
        runner.start(background)

    def _pause_for(self, old: AgentTask, new: AgentTask):
        runner = self._runners.get(old.id)
        if runner is not None:
            runner.pause()
        old.note("PAUSE", f"Paused for a new request: {new.request[:60]}")
        self._stack.append(old.id)

    # ---- callbacks from the runner -------------------------------------------------------------------------
    def set_status(self, task: AgentTask, status: str, announce: bool = True):
        with self._lock:
            changed = task.status != status
            task.status = status
            task.updated = time.time()
            if changed:
                self._save()
        self._emit(task)
        if not changed:
            return
        log.info("task.status %s %s", task.id, status)
        if status in TaskStatus.FINAL:
            self._finished(task, announce)

    def trail_changed(self, task: AgentTask):
        if task.trail:
            self._trail_line(task, task.trail[-1])
        self._emit(task)

    def runner_finished(self, runner):
        task = runner.task
        with self._lock:
            if self._runners.get(task.id) is runner:
                self._runners.pop(task.id, None)
        if task.status in TaskStatus.FINAL and self._stack:
            nxt = self.get(self._stack.pop())
            if nxt is not None and nxt.status == TaskStatus.PAUSED:
                nxt.note("RESUME", "Back to this after the other request")
                self._launch(nxt)

    def park_for_answer(self, task: AgentTask, index: int, question: str):
        """A step asked the user something (a confirmation or "which window?"): when it's answered,
        the step's own action runs, then the task continues from there."""
        from modules.agent.confirm import choices, confirmations
        step = task.plan[index]
        resume = self._resume_after_answer

        if confirmations.pending is not None:
            p = confirmations.pending
            original = p.run

            def run_and_resume(original=original):
                out = original()
                resume(task, index, out)
                return out
            p.run = run_and_resume
            task.context["waiting_on"] = p.description
        elif choices.pending is not None:
            c = choices.pending
            original_c = c.run

            def choose_and_resume(value, original_c=original_c):
                out = original_c(value)
                resume(task, index, out)
                return out
            c.run = choose_and_resume
        step.status = StepStatus.RUNNING
        task.reason = question
        task.note("WAIT", question, index)
        self.set_status(task, TaskStatus.WAITING_FOR_USER)
        self._say(task, question, expects_reply=True)

    def _resume_after_answer(self, task: AgentTask, index: int, output: str):
        step = task.plan[index]
        step.inputs["answered"] = output or ""
        step.status = StepStatus.PENDING
        task.note("RESUME", "Got your answer")
        task.context.pop("waiting_on", None)
        self._launch(task)

    def ask_plan_approval(self, task: AgentTask):
        question = self._plan_question(task)
        self._ask(task, question, lambda: self._approve_plan(task))
        self._say(task, question, expects_reply=True)

    def _approve_plan(self, task: AgentTask) -> str:
        task.context["plan_approved"] = True
        task.note("RESUME", "You said go ahead")
        self._launch(task)
        return "Okay."

    def ask_step_approval(self, task: AgentTask, index: int):
        step = task.plan[index]
        question = f"Next I'd {step.title().lower()}. Is that okay?"

        def approve():
            step.inputs["approved"] = True
            task.note("RESUME", f"You approved: {step.title()}", index)
            self._launch(task)
            return "Okay."
        self._ask(task, question, approve)
        self._say(task, question, expects_reply=True)

    def _ask(self, task: AgentTask, question: str, on_yes):
        from modules.agent.confirm import PendingAction, confirmations
        confirmations.ask(PendingAction(description=f"task {task.id}: {question}"[:120], run=on_yes, tool=""))
        task.context["waiting_on"] = f"task {task.id}: {question}"[:120]
        task.reason = question
        task.note("WAIT", question)
        self.set_status(task, TaskStatus.WAITING_FOR_USER, announce=False)

    def on_confirmation_resolved(self, payload: Dict):
        """A question a task asked was answered no / expired."""
        desc, result = payload.get("description", ""), payload.get("result", "")
        if result in ("confirmed", "superseded", ""):
            return
        for task in self.all():
            if task.status == TaskStatus.WAITING_FOR_USER and task.context.get("waiting_on") == desc:
                task.context.pop("waiting_on", None)
                if result == "declined":
                    task.result = "Okay, I stopped there. Say “continue” if you change your mind."
                    task.note("CANCELLED", "You said no")
                    self.set_status(task, TaskStatus.CANCELLED, announce=False)
                else:
                    task.note("PAUSE", "The question went unanswered")
                    self.set_status(task, TaskStatus.PAUSED)

    # ---- the user talking while a task runs ---------------------------------------------------------------------
    @contextlib.contextmanager
    def user_turn(self):
        with self._lock:
            self._talking += 1
        try:
            yield
        finally:
            with self._lock:
                self._talking = max(0, self._talking - 1)

    def wait_while_user_talks(self, runner, limit: float = 30.0):
        end = time.time() + limit
        while self._talking and time.time() < end and not runner.cancel_evt.is_set():
            time.sleep(0.05)

    # ---- control -------------------------------------------------------------------------------------------------
    def pause(self) -> Optional[str]:
        t = self.running()
        if t is None:
            return None
        runner = self._runners.get(t.id)
        if runner is not None:
            runner.pause()
        return f"Pausing {t.goal[:1].lower() + t.goal[1:]} after this step. Say “continue” when you're ready."

    def resume(self) -> Optional[str]:
        t = self.current() or self.last((TaskStatus.FAILED, TaskStatus.CANCELLED))
        if t is None or time.time() - t.updated > RESUME_MAX_AGE:
            return None
        if t.status in TaskStatus.ACTIVE:
            return f"I'm still on it — {self.describe()}"
        if t.status == TaskStatus.WAITING_FOR_USER:
            from modules.agent.confirm import confirmations
            if confirmations.pending is not None and t.context.get("waiting_on"):
                return t.reason
        for s in t.plan:
            if s.status in (StepStatus.FAILED, StepStatus.RUNNING):
                s.status, s.attempts = StepStatus.PENDING, 0
        if not any(s.status == StepStatus.PENDING for s in t.plan):
            return None
        t.retries = 0
        t.note("RESUME", "Continuing")
        nxt = next(s for s in t.plan if s.status == StepStatus.PENDING)
        self._launch(t)
        return f"Picking up {t.goal[:1].lower() + t.goal[1:]}: {nxt.title().lower()}."

    def stop_active(self, reason: str = "you said stop") -> Optional[str]:
        t = self.current()
        if t is None or t.status == TaskStatus.PAUSED:
            return None
        runner = self._runners.get(t.id)
        if runner is not None:
            runner.cancel()
        if t.status == TaskStatus.WAITING_FOR_USER:
            from modules.agent.confirm import confirmations
            if t.context.get("waiting_on") and confirmations.pending is not None:
                confirmations.clear("dismissed")
        t.note("CANCELLED", reason)
        t.result = t.result or f"Stopped {t.goal[:1].lower() + t.goal[1:]}."
        self.set_status(t, TaskStatus.CANCELLED, announce=False)
        return f"Stopped {t.goal[:1].lower() + t.goal[1:]}. Say “continue” to pick it up again."

    def describe(self) -> Optional[str]:
        t = self.current()
        if t is None:
            return None
        step = t.step
        n = len(t.plan)
        if t.status == TaskStatus.WAITING_FOR_USER:
            return f"I'm waiting for your answer: {t.reason}"
        if t.status == TaskStatus.PAUSED:
            nxt = next((s for s in t.plan if s.status == StepStatus.PENDING), None)
            return f"{t.goal} is paused" + (f" before {nxt.title().lower()}" if nxt else "") + \
                ". Say “continue” to pick it up."
        if step is None:
            return f"I'm working on {t.goal[:1].lower() + t.goal[1:]}."
        text = f"I'm {step.title()[:1].lower() + step.title()[1:]} — step {t.current + 1} of {n} of " \
               f"{t.goal[:1].lower() + t.goal[1:]}."
        if t.status == TaskStatus.RECOVERING and t.reason:
            text += f" {t.reason}."
        return text

    def where(self) -> Optional[str]:
        t = self.current() or self.last()
        if t is None:
            return None
        done = [s.title().lower() for s in t.plan if s.status == StepStatus.DONE]
        if t.status in TaskStatus.FINAL:
            return f"{t.goal} {'finished' if t.status == TaskStatus.COMPLETED else t.status}" + \
                (f" — done: {', '.join(done[:5])}." if done else ".")
        cur = t.step
        parts = [f"Step {t.current + 1} of {len(t.plan)}: {cur.title().lower()}." if cur else f"{t.goal}."]
        if done:
            parts.append("Done: " + ", ".join(done[:5]) + ".")
        return " ".join(parts)

    def next_step(self) -> Optional[str]:
        t = self.current()
        if t is None:
            return None
        pending = [s.title().lower() for s in t.plan if s.status == StepStatus.PENDING]
        if t.step is not None and t.step.status == StepStatus.RUNNING and pending and \
                pending[0] == t.step.title().lower():
            pending = pending[1:]
        if not pending:
            return "That's the last step."
        return "Next: " + ", then ".join(pending[:3]) + "."

    def failure(self) -> Optional[str]:
        t = next((x for x in reversed(self.all()) if x.failures), None)
        if t is None or time.time() - t.updated > RESUME_MAX_AGE:
            return None
        last = t.failures[-1]
        tried = [e.text for e in t.trail if e.kind == "RECOVERY"][-2:]
        text = f"{last}."
        if tried:
            text += " I tried: " + "; ".join(tried) + "."
        if t.status == TaskStatus.COMPLETED:
            text += " It worked in the end."
        elif t.status in (TaskStatus.FAILED, TaskStatus.PAUSED, TaskStatus.CANCELLED):
            text += " Say “continue” to try again."
        return text

    def redo(self) -> Optional[Dict]:
        t = self.last(TaskStatus.FINAL)
        if t is None or time.time() - t.updated > RESUME_MAX_AGE:
            return None
        return self.try_start(t.request, allow_model=t.source == "model")

    def remember(self, name: str = "") -> Optional[str]:
        """'Remember how I just did that' / 'save that as coding time'."""
        t = self.last((TaskStatus.COMPLETED,))
        if t is None or time.time() - t.updated > REMEMBER_MAX_AGE:
            return None
        from modules.learning.procedures import procedures
        proc = procedures.save_from_task(t, phrase=name or t.request, source="taught")
        if proc is None:
            return None
        t.learned = proc.id
        self._save()
        return (f"Got it — when you say “{(name or t.request).strip(' .')}”, I'll do those "
                f"{len(proc.steps)} steps again.")

    def modify(self, text: str) -> Optional[str]:
        """'Actually put Spotify on my main monitor' / 'use the other monitor' during a task: change
        the step it's about (or redo it if it already ran). None when it isn't about this task."""
        t = self.current()
        if t is None or t.status in TaskStatus.FINAL:
            return None
        instruction = re.sub(r"^(?:actually|no|instead|rather|wait|oh|um|and)[,\s]+", "", text.strip(), flags=re.I)
        instruction = re.sub(r"\s+instead$", "", instruction, flags=re.I).strip(" .!")
        from modules.agent.autonomy.recover import _target_app
        mon = re.search(r"\b(main|primary|first|second|third|left|right|other)\s+(monitor|screen|display)\b",
                        instruction, re.I)
        app = _target_app(instruction)
        candidates = [i for i, s in enumerate(t.plan)]
        target = None
        for i in reversed(candidates):
            s = t.plan[i]
            if app and app in s.action.lower():
                target = i
                if s.status == StepStatus.PENDING:
                    break
            elif not app and mon and re.search(r"\b(?:monitor|screen|display)\b", s.action, re.I):
                target = i
                if s.status == StepStatus.PENDING:
                    break
        if target is None:
            return None
        s = t.plan[target]
        if not app and mon:
            new_action = re.sub(r"\b(?:my\s+|the\s+)?(?:main|primary|first|second|third|left|right|other|\d)\s+"
                                r"(?:monitor|screen|display)\b", f"my {mon.group(1).lower()} {mon.group(2).lower()}",
                                s.action, flags=re.I)
        else:
            new_action = instruction
        try:
            from modules.learning.planner import understood
            if not understood(new_action):
                return None
        except Exception:
            return None
        if s.status == StepStatus.PENDING:
            from modules.agent.autonomy.goals import label as _label
            from modules.agent.autonomy.verify import infer
            s.replaced = s.replaced or s.action
            s.action, s.tool, s.args, s.label = new_action, "", {}, _label(new_action)
            s.verify = infer(new_action)
            t.note("NOTE", f"You changed a step: {s.replaced} → {new_action}", target)
            t.context.setdefault("user_changes", []).append(target)
            self._emit(t)
            return f"Okay — I'll {new_action} instead."
        # Already done: do it the new way now, then carry on.
        from modules.agent.autonomy.goals import label
        new = PlanStep(new_action, label(new_action))
        new.inputs["user_change"] = target
        insert_at = next((i for i, x in enumerate(t.plan) if x.status == StepStatus.PENDING), len(t.plan))
        t.plan.insert(insert_at, new)
        t.note("NOTE", f"You asked to change: {s.action} → {new_action}", target)
        t.context.setdefault("user_changes", []).append(target)
        if t.status == TaskStatus.PAUSED or (t.status in TaskStatus.FINAL):
            self._launch(t)
        self._emit(t)
        return f"Okay — {new_action}."

    # ---- finishing ----------------------------------------------------------------------------------------------
    def _finished(self, task: AgentTask, announce: bool = True):
        try:
            self._learn(task)
        except Exception:
            log.exception("tasks.learn_failed %s", task.id)
        self._save()
        if announce and task.result and task.background and task.announce:
            self._say(task, task.result)
        if task.status == TaskStatus.COMPLETED:
            event_bus.emit_event(EventType.TASK_DONE, {"summary": "", "status": "done", "announce": False,
                                                       "agent_task": task.id})

    def _learn(self, task: AgentTask):
        if not config.get("learning.from_tasks", True):
            return
        from modules.learning.procedures import procedures
        ok = task.status == TaskStatus.COMPLETED
        if task.procedure:
            procedures.note_result(task.procedure, ok)
            if not ok:
                return
            # The procedure's steps are the plan's own steps, recovery insertions aside.
            own = [i for i, s in enumerate(task.plan) if not s.inputs.get("recovery") and not s.inputs.get("user_change")]
            changed = set(task.context.get("replaced_steps", [])) | set(task.context.get("user_changes", []))
            for k, i in enumerate(own):
                s = task.plan[i]
                if i in changed and s.replaced and s.status == StepStatus.DONE:
                    why = "you asked for it" if i in task.context.get("user_changes", []) else \
                        f"“{s.replaced}” stopped working"
                    if procedures.update_step(task.procedure, k, s.action, why, verify=s.verify):
                        task.note("LEARN", f"Updated the procedure: {s.replaced} → {s.action}", i)
                        task.learned = task.procedure
            return
        if ok and task.source in ("goal", "steps", "model") and \
                len([s for s in task.plan if s.status == StepStatus.DONE]) >= 2:
            proc = procedures.save_from_task(task)
            if proc is not None:
                task.learned = proc.id
                task.note("LEARN", f"Learned how to “{task.request.strip(' .')}” ({len(proc.steps)} steps)")

    # ---- output ---------------------------------------------------------------------------------------------------
    def _say(self, task: AgentTask, text: str, expects_reply: bool = False):
        if not text:
            return
        try:
            from core.conversation import get_controller
            ctrl = get_controller()
            if ctrl is not None:
                ctrl.announce(text, source="task", expects_reply=expects_reply)
        except Exception:
            log.debug("tasks.say_failed", exc_info=True)

    def _emit(self, task: AgentTask):
        event_bus.emit_event(EventType.AGENT_TASK, task.summary())

    def _trail_line(self, task: AgentTask, entry):
        if entry is None:
            return
        stamp = time.strftime("%H:%M:%S", time.localtime(entry.at))
        trail_log.info("%s task=%s %-9s %s", stamp, task.id, entry.kind, entry.text)


agent_tasks = AgentTaskManager()


def _on_event(ev):
    if ev.type == EventType.AGENT_CONFIRM_RESOLVED:
        try:
            agent_tasks.on_confirmation_resolved(ev.payload or {})
        except Exception:
            log.exception("tasks.confirm_hook_failed")


event_bus.subscribe(_on_event)

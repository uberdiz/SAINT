"""v0.4 agent task loop: plan -> act -> verify -> recover -> complete -> learn, with pause / resume /
cancel / interruption and the permission modes. Commands are fake (no real desktop): the router and
the observer are replaced, so these tests exercise the loop itself."""

import threading
import time

import pytest

from core.config import config
from modules.agent.autonomy import manager as mgr_mod
from modules.agent.autonomy.model import AgentTask, PlanStep, StepStatus, TaskStatus
from modules.agent.router import Intent, Reply


# ---------------------------------------------------------------- fakes
class World:
    """A pretend PC: commands change it, the observer reads it."""

    def __init__(self):
        self.windows = set()
        self.log = []
        self.fail = {}            # command -> how many more times it fails
        self.slow = {}            # command -> seconds it takes
        self.ask = set()          # commands that ask a yes/no question first
        self.fail_text = {}       # command -> what the tool says when it fails

    def run(self, cmd):
        self.log.append(cmd)
        time.sleep(self.slow.get(cmd, 0))
        if self.fail.get(cmd, 0) > 0:
            self.fail[cmd] -= 1
            return Reply(self.fail_text.get(cmd, f"I couldn't do {cmd}."), ok=False)
        if cmd in self.ask:
            from modules.agent.confirm import PendingAction, confirmations

            def yes(c=cmd):
                self.windows.add(c.split(" ", 1)[1])
                return f"Did {c}."
            confirmations.ask(PendingAction(description=cmd, run=yes, tool="fake"))
            return Reply(f"Do you want me to {cmd}?", expects_reply=True)
        if cmd.startswith("open "):
            self.windows.add(cmd[5:])
        return Reply(f"Done: {cmd}.")


class FakeObserver:
    def __init__(self, world):
        self.world = world

    def window(self, name, fresh=False):
        from modules.agent.autonomy.observe import Observation
        ok = name in self.world.windows
        return Observation("window", ok, [], "os", f"{name} {'open' if ok else 'not open'}")

    def snapshot(self, spotify=False):
        return "In front: test"

    def __getattr__(self, name):
        raise AttributeError(name)


@pytest.fixture
def world(monkeypatch, tmp_path):
    w = World()
    known = ("open ", "move ", "put ", "pause ", "type ", "delete ", "install ")

    def route(text):
        if text.startswith(known):
            return Intent("fake." + text.split()[0], lambda t=text: w.run(t), "desktop")
        return None
    monkeypatch.setattr("modules.agent.router.route", route)
    monkeypatch.setattr("modules.learning.planner.understood", lambda c: c.startswith(known))
    monkeypatch.setattr("modules.learning.planner.grounded", lambda c, r, recent="": True)
    obs = FakeObserver(w)
    monkeypatch.setattr("modules.agent.autonomy.executor.default_observer", obs)
    monkeypatch.setattr("modules.agent.autonomy.verify.default_observer", obs)
    monkeypatch.setattr("modules.agent.autonomy.recover._ask_model", lambda *a, **k: None)
    monkeypatch.setattr("modules.agent.autonomy.verify.infer", lambda a: {"kind": "reply"})
    config.set("automation.permission_mode", "autonomous", persist=False)
    config.set("automation.confirm_dangerous", True, persist=False)
    m = mgr_mod.AgentTaskManager(path=str(tmp_path / "agent_tasks.json"))
    m._loaded = True
    monkeypatch.setattr(mgr_mod, "agent_tasks", m)
    monkeypatch.setattr("modules.agent.autonomy.control.agent_tasks", m)
    from modules.learning import procedures as procs, skills as sk
    monkeypatch.setattr(sk, "skills", sk.SkillStore(str(tmp_path / "skills.json")))
    monkeypatch.setattr(procs, "procedures", procs.ProcedureStore(str(tmp_path / "procedures.json")))
    w.manager = m
    yield w
    for r in list(m._runners.values()):
        r.cancel()
    from modules.agent.confirm import confirmations
    confirmations.clear("test")
    config.set("automation.permission_mode", "confirm", persist=False)


def _task(*cmds, request="set up my test workspace", verify=None):
    t = AgentTask(request=request, goal="Set up your test workspace", source="steps")
    t.plan = [PlanStep(c, c.capitalize(), verify=verify or {"kind": "reply"}) for c in cmds]
    return t


def _wait(task, statuses, secs=5.0):
    end = time.time() + secs
    while time.time() < end:
        if task.status in statuses:
            return task.status
        time.sleep(0.02)
    raise AssertionError(f"task stayed {task.status}; trail: {[(e.kind, e.text) for e in task.trail]}")


# ---------------------------------------------------------------- planning
def test_simple_requests_are_not_tasks():
    from modules.agent.autonomy import planner
    assert not planner.should_task("pause spotify")
    assert not planner.should_task("play my gym playlist")
    assert not planner.should_task("open discord and spotify")
    assert planner.should_task("set up my coding workspace")
    assert planner.should_task("open discord, open spotify and tell me when everything is ready")


def test_clause_split_and_ready_flag():
    from modules.agent.autonomy.planner import split_clauses
    parts, ready = split_clauses("Open my project, start the dev server, open Discord, put Spotify on the second "
                                 "monitor, and tell me when everything is ready.")
    assert parts == ["Open my project", "start the dev server", "open Discord", "put Spotify on the second monitor"]
    assert ready


def test_goal_plan_uses_observations(monkeypatch, tmp_path):
    from modules.agent.autonomy import goals
    from modules.dev.project import Project
    proj = Project(str(tmp_path), "demo", "node", start="npm run dev", test="npm test", install="npm install", port=5173)
    monkeypatch.setattr(goals, "_project", lambda name: proj)
    monkeypatch.setattr("modules.agent.autonomy.planner._understood", lambda c: True)
    g = goals.match("set up my coding workspace for demo and open discord")
    task = goals.build(g, "set up my coding workspace for demo and open discord", object())
    tools = [s.tool for s in task.plan]
    assert tools[:3] == ["dev.open_project", "dev.start_server", "desktop.open_url"]
    assert task.plan[1].verify == {"kind": "port", "port": 5173}
    assert task.plan[-1].action == "open discord"
    assert any(e.kind == "OBSERVE" for e in task.trail)


# ---------------------------------------------------------------- the loop
def test_task_runs_verifies_and_completes(world):
    t = _task("open alpha", "open beta", verify=None)
    for s in t.plan:
        s.verify = {"kind": "window", "name": s.action[5:]}
    res = world.manager.start(t)
    assert res["ok"]
    _wait(t, (TaskStatus.COMPLETED,))
    assert [s.status for s in t.plan] == [StepStatus.DONE, StepStatus.DONE]
    kinds = [e.kind for e in t.trail]
    assert "ACTION" in kinds and "VERIFY" in kinds and kinds[-1] in ("COMPLETE", "LEARN")
    assert t.result.startswith("Done")


def test_verification_failure_is_recovered_by_opening_the_app(world):
    """'move spotify ...' when Spotify isn't open: open it first, then retry and verify."""
    t = _task("move spotify to my second monitor")
    t.plan[0].verify = {"kind": "window", "name": "spotify"}
    world.fail["move spotify to my second monitor"] = 1
    world.fail_text["move spotify to my second monitor"] = "I couldn't find a Spotify window."
    world.manager.start(t)
    _wait(t, (TaskStatus.COMPLETED, TaskStatus.FAILED))
    assert t.status == TaskStatus.COMPLETED, t.trail
    assert world.log[:3] == ["move spotify to my second monitor", "open spotify", "move spotify to my second monitor"]
    assert any(e.kind == "RECOVERY" for e in t.trail)


def test_retry_limits_end_a_failing_task_honestly(world):
    t = _task("open alpha", "open never")
    world.fail["open never"] = 99
    world.manager.start(t)
    _wait(t, (TaskStatus.FAILED,))
    assert world.log.count("open never") <= 3                     # bounded: no infinite loop
    assert "didn't work" in t.result and "open alpha" in t.result.lower()
    assert len(t.plan) <= 24


def test_optional_step_failure_is_skipped(world):
    t = _task("open alpha", "open never")
    t.plan[1].optional = True
    world.fail["open never"] = 99
    world.manager.start(t)
    _wait(t, (TaskStatus.COMPLETED,))
    assert t.plan[1].status == StepStatus.SKIPPED and "skipped" in t.result


def test_stop_cancels_and_continue_resumes(world):
    t = _task("open slow", "open beta")
    world.slow["open slow"] = 0.6
    world.manager.start(t)
    time.sleep(0.15)
    assert "Stopped" in world.manager.stop_active()
    _wait(t, (TaskStatus.CANCELLED,))
    world.slow["open slow"] = 0
    reply = world.manager.resume()
    assert reply and "Picking up" in reply
    _wait(t, (TaskStatus.COMPLETED,))
    assert t.plan[1].status == StepStatus.DONE


def test_pause_and_resume(world):
    t = _task("open slow", "open beta", "open gamma")
    world.slow["open slow"] = 0.4
    world.manager.start(t)
    time.sleep(0.1)
    assert world.manager.pause()
    _wait(t, (TaskStatus.PAUSED,))
    assert t.plan[2].status == StepStatus.PENDING
    assert "paused" in world.manager.describe()
    world.manager.resume()
    _wait(t, (TaskStatus.COMPLETED,))


def test_user_talking_holds_the_task_at_the_next_step(world):
    t = _task("open alpha", "open beta")
    with world.manager.user_turn():
        world.manager.start(t)
        time.sleep(0.3)
        assert world.log == []                                   # waits while the user speaks
    _wait(t, (TaskStatus.COMPLETED,))


def test_actually_changes_a_pending_step(world):
    t = _task("open slow", "put spotify on my second monitor")
    world.slow["open slow"] = 0.4
    world.manager.start(t)
    time.sleep(0.1)
    reply = world.manager.modify("actually put spotify on my main monitor")
    assert reply and "main monitor" in reply
    _wait(t, (TaskStatus.COMPLETED,))
    assert "put spotify on my main monitor" in world.log
    assert "put spotify on my second monitor" not in world.log


def test_use_the_other_monitor_rewrites_the_monitor(world):
    t = _task("open slow", "move spotify to my main monitor")
    world.slow["open slow"] = 0.4
    world.manager.start(t)
    time.sleep(0.1)
    assert world.manager.modify("use the other monitor")
    _wait(t, (TaskStatus.COMPLETED,))
    assert "move spotify to my other monitor" in world.log


def test_a_question_parks_the_task_until_answered(world):
    from modules.agent.confirm import confirmations
    t = _task("open alpha", "delete junk", "open gamma")
    world.ask.add("delete junk")
    world.manager.start(t)
    _wait(t, (TaskStatus.WAITING_FOR_USER,))
    assert "delete junk" in t.reason
    assert confirmations.resolve("yes") == "Did delete junk."
    _wait(t, (TaskStatus.COMPLETED,))
    assert world.log[-1] == "open gamma"


def test_saying_no_stops_the_task(world):
    from modules.agent.confirm import confirmations
    t = _task("delete junk", "open gamma")
    world.ask.add("delete junk")
    world.manager.start(t)
    _wait(t, (TaskStatus.WAITING_FOR_USER,))
    confirmations.resolve("no")
    _wait(t, (TaskStatus.CANCELLED,))
    assert "open gamma" not in world.log


# ---------------------------------------------------------------- permissions
def test_safe_mode_refuses_medium_steps_up_front(world):
    config.set("automation.permission_mode", "safe", persist=False)
    t = _task("open alpha", "type hello")
    t.plan[1].risk = "medium"
    res = world.manager.start(t)
    assert not res["ok"] and "Safe mode" in res["text"]
    assert world.log == []


def test_confirm_mode_asks_once_for_a_plan_with_medium_steps(world):
    from modules.agent.confirm import confirmations
    config.set("automation.permission_mode", "confirm", persist=False)
    t = _task("open alpha", "type hello")
    t.plan[1].risk = "medium"
    res = world.manager.start(t)
    assert res["expects_reply"] and "Go ahead?" in res["text"]
    assert world.log == []
    confirmations.resolve("yes")
    _wait(t, (TaskStatus.COMPLETED,))
    assert world.log == ["open alpha", "type hello"]


def test_autonomous_runs_medium_but_asks_for_high(world):
    from modules.agent.confirm import confirmations
    t = _task("type hello", "delete everything", "open beta")
    t.plan[0].risk, t.plan[1].risk = "medium", "high"
    world.manager.start(t)
    _wait(t, (TaskStatus.WAITING_FOR_USER,))
    assert world.log == ["type hello"]
    confirmations.resolve("yes")
    _wait(t, (TaskStatus.COMPLETED,))
    assert world.log == ["type hello", "delete everything", "open beta"]


def test_risk_classification():
    from modules.agent.autonomy.policy import classify
    assert classify("open discord") == "low"
    assert classify("put spotify on my second monitor") == "low"
    assert classify("type hello world") == "medium"
    assert classify("install the project dependencies") == "medium"
    assert classify("delete the build folder") == "high"
    assert classify("run the command format c:") == "high"


# ---------------------------------------------------------------- learning
def test_finished_task_is_learned_and_reused(world):
    from modules.agent.autonomy import planner
    from modules.learning import procedures as procs
    t = _task("open alpha", "open beta", request="get my test desk ready")
    world.manager.start(t)
    _wait(t, (TaskStatus.COMPLETED,))
    time.sleep(0.05)
    assert t.learned
    again = planner.plan("get my test desk ready", world.manager)       # observer unused for procedures
    assert again is not None and again.source == "procedure"
    assert [s.action for s in again.plan] == ["open alpha", "open beta"]
    assert procs.procedures.get(t.learned).version == 1


def test_failed_procedure_step_is_replaced_and_the_procedure_updated(world):
    from modules.agent.autonomy import planner
    from modules.learning import procedures as procs
    t = _task("open alpha", "open beta", request="get my test desk ready")
    world.manager.start(t)
    _wait(t, (TaskStatus.COMPLETED,))
    time.sleep(0.05)
    # Next time "open beta" stops working; the plan's fallback finds another way.
    task2 = planner.plan("get my test desk ready", world.manager)
    task2.plan[1].fallback = ["open beta2"]
    world.fail["open beta"] = 99
    world.manager.start(task2)
    _wait(task2, (TaskStatus.COMPLETED,))
    time.sleep(0.05)
    proc = procs.procedures.get(task2.procedure)
    assert [s.action for s in proc.steps] == ["open alpha", "open beta2"]
    assert proc.version == 2 and "stopped working" in proc.history[-1]["reason"]
    assert "open beta" in proc.steps[1].fallback                          # the old way is kept as a fallback


def test_unvalidated_replacement_is_never_learned(world, monkeypatch):
    from modules.learning import procedures as procs
    t = _task("open alpha", "open beta", request="get my test desk ready")
    world.manager.start(t)
    _wait(t, (TaskStatus.COMPLETED,))
    time.sleep(0.05)
    assert procs.procedures.update_step(t.learned, 1, "frobnicate the beta", "model said so") is None
    assert [s.action for s in procs.procedures.get(t.learned).steps] == ["open alpha", "open beta"]


def test_remember_how_i_just_did_that(world):
    t = _task("open alpha", "open beta", request="do my morning thing")
    t.source = "composite"                                               # not learned automatically
    world.manager.start(t)
    _wait(t, (TaskStatus.COMPLETED,))
    reply = world.manager.remember("morning setup")
    assert reply and "morning setup" in reply
    from modules.agent.autonomy import planner
    assert planner.plan("morning setup", world.manager).source == "procedure"


# ---------------------------------------------------------------- voice control
def test_control_words_only_apply_to_a_task(world):
    from modules.agent.autonomy import control
    assert control.handle("continue") is None                         # nothing to continue
    assert control.handle("pause") is None                            # the music hot-word stays the music's
    t = _task("open slow", "open beta")
    world.slow["open slow"] = 0.5
    world.manager.start(t)
    time.sleep(0.1)
    out = control.handle("what's next?")
    assert out and "open beta" in out["text"].lower()
    out = control.handle("hold on")
    assert out and out["intent"] == "agent.task.pause"
    _wait(t, (TaskStatus.PAUSED,))
    out = control.handle("continue")
    assert out and "Picking up" in out["text"]
    _wait(t, (TaskStatus.COMPLETED,))


def test_why_did_that_fail(world):
    from modules.agent.autonomy import control
    t = _task("open never")
    world.fail["open never"] = 99
    world.manager.start(t)
    _wait(t, (TaskStatus.FAILED,))
    out = control.handle("why did that fail?")
    assert out and "Open never" in out["text"]


def test_tasks_survive_a_restart_paused(tmp_path):
    m = mgr_mod.AgentTaskManager(path=str(tmp_path / "t.json"))
    m._loaded = True
    t = _task("open a", "open b")
    t.status = TaskStatus.EXECUTING
    t.plan[0].status = StepStatus.DONE
    t.plan[1].status = StepStatus.RUNNING
    m._tasks.append(t)
    m._save()
    m2 = mgr_mod.AgentTaskManager(path=str(tmp_path / "t.json"))
    m2.load()
    t2 = m2.get(t.id)
    assert t2.status == TaskStatus.PAUSED and t2.plan[1].status == StepStatus.PENDING

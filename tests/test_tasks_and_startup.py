"""Task memory ("continue what we were doing", "do the same for X"), the
gaming-workspace phrases, and the startup registry (2026-10-01)."""

import pytest

from modules.agent import task_memory as tm
from modules.agent.router import Intent, Reply, run_plan
from modules.agent.task_intents import parse_task, substitute


@pytest.fixture
def memory(tmp_path, monkeypatch):
    m = tm.TaskMemory(path=tmp_path / "tasks.json")
    monkeypatch.setattr(tm, "task_memory", m)
    return m


def _intent(name, log, ok=True):
    def run():
        log.append(name)
        return Reply(f"{name} done." if ok else f"{name} failed.", ok=ok)
    return Intent(name, run, "test")


def test_plan_steps_are_recorded(memory):
    log = []
    task = memory.begin("open a then b", [{"text": "open a", "part": 0, "label": "opening a"},
                                           {"text": "open b", "part": 1, "label": "opening b"}])
    r = run_plan([_intent("a", log), _intent("b", log)], task=task)
    assert r.ok and log == ["a", "b"]
    t = memory.current()
    assert t["status"] == "done" and [s["status"] for s in t["steps"]] == ["done", "done"]
    assert memory.resumable() is None


def test_failed_plan_is_resumable_and_survives_a_restart(memory, tmp_path):
    log = []
    task = memory.begin("open a then b then c", [{"text": t, "part": i, "label": t}
                                                  for i, t in enumerate(["open a", "open b", "open c"])])
    r = run_plan([_intent("a", log), _intent("b", log, ok=False), _intent("c", log)], task=task)
    assert not r.ok and log == ["a", "b"]
    again = tm.TaskMemory(path=tmp_path / "tasks.json")       # SAINT restarted
    t = again.resumable()
    assert t and t["status"] == "failed" and again.remaining(t) == [1, 2]
    assert "Still to do" in again.describe() and "didn't work" in again.describe()


def test_continue_runs_the_remaining_steps(memory, monkeypatch):
    from modules.agent import task_intents
    log = []
    steps = ["open a", "open b", "open c"]
    task = memory.begin("open a, open b, open c", [{"text": t, "part": i, "label": t} for i, t in enumerate(steps)])
    memory.step(task, 0, tm.DONE)
    memory.step(task, 1, tm.FAILED, "not found")
    memory.finish(task, "failed")
    monkeypatch.setattr(task_intents, "route", lambda words: _intent(words, log))
    intent = parse_task("continue what we were doing")
    assert intent and intent.name == "task.continue"
    r = intent.run()
    assert r.ok and log == ["open b", "open c"]            # step a isn't done twice
    assert memory.current()["status"] == "done"


def test_continue_with_nothing_unfinished(memory):
    r = parse_task("finish it").run()
    assert not r.ok and "nothing unfinished" in r.text


def test_same_thing_for_swaps_only_the_named_thing():
    assert substitute("open chrome and move it to my second monitor", "Discord") == \
        "open Discord and move it to my second monitor"
    assert substitute("move chrome to my second monitor", "Discord") == "move Discord to my second monitor"
    assert substitute("play bad bunny on spotify", "Rammstein") == "play Rammstein on spotify"
    assert substitute("what time is it", "Discord") is None             # nothing to swap: no guessing
    assert substitute("open it", "Discord") is None


@pytest.mark.parametrize("text,name", [
    ("where were we", "task.status"),
    ("what's left to do", "task.status"),
    ("pick up where we left off", "task.continue"),
    ("do the same thing for discord", "task.same_for"),
    ("set up my gaming workspace", "task.gaming_workspace"),
    ("get my gaming setup ready", "task.gaming_workspace"),
    ("move the mini player to my second monitor", "ui.place"),
    ("put saint on the left screen", "ui.place"),
    ("turn off auto gaming mode", "ui.auto_gaming_mode"),
])
def test_task_phrases(text, name):
    intent = parse_task(text)
    assert intent is not None and intent.name == name


@pytest.mark.parametrize("text", ["continue", "resume", "keep playing", "play something", "gaming mode on",
                                  "finish the song"])
def test_not_task_phrases(text):
    assert parse_task(text) is None          # "continue" alone is music; Gaming Mode is saint_intents


def test_route_sends_task_phrases_before_other_parsers():
    from modules.agent.router import route
    assert route("continue what we were doing").name == "task.continue"
    assert route("auto gaming mode on").name == "ui.auto_gaming_mode"


def test_startup_records_failures_and_retries():
    from core.startup import Startup
    s = Startup()
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Ollama isn't answering")
    assert not s.run("ai", "AI model", flaky)
    assert s.failed()[0]["detail"] == "Ollama isn't answering" and s.failed()[0]["retryable"]
    assert s.run("voice", "Voice", lambda: None)
    s.run("link", "SAINT Link", lambda: None, enabled=False)
    assert {x["key"]: x["status"] for x in s.steps()} == {"ai": "failed", "voice": "ok", "link": "off"}
    assert s.retry("ai") and not s.failed()

"""Reminders/automations: parsing, persistence across restart, execution, cancel."""

import time
from datetime import datetime

import pytest

from modules.automation import scheduler as sched_mod
from modules.automation.scheduler import Scheduler, AutomationStore, ACTIVE, COMPLETED, CANCELLED, MISSED
from modules.automation.timeparse import extract_reminder, parse_schedule, next_run, describe


NOW = datetime(2026, 9, 23, 14, 0)  # a Wednesday


@pytest.mark.parametrize("text,kind,expect", [
    ("remind me at 5 PM to work on AIDE", "once", "17:00"),
    ("remind me in 30 minutes", "once", "14:30"),
    ("remind me tomorrow at 9 to call mom", "once", "09:00"),
    ("every morning remind me to check my schedule", "daily", "08:00"),
    ("remind me every weekday at 8:30 am to stand up", "daily", "08:30"),
    ("remind me every 2 hours to drink water", "interval", None),
])
def test_parse(text, kind, expect):
    sched, _ = parse_schedule(text, now=NOW)
    assert sched["type"] == kind
    if kind == "once":
        assert datetime.fromtimestamp(sched["at"]).strftime("%H:%M") == expect
    elif kind == "daily":
        assert sched["time"] == expect


def test_reminder_message():
    r = extract_reminder("remind me at 5 PM to work on AIDE")
    assert r["message"] == "Work on AIDE" and r["schedule"]["type"] == "once"
    assert extract_reminder("remind me to buy milk")["schedule"] is None
    assert extract_reminder("what reminders do I have") is None


def test_weekday_next_run():
    sched = {"type": "daily", "time": "08:00", "days": [0, 1, 2, 3, 4]}
    fri_eve = datetime(2026, 9, 25, 20, 0).timestamp()      # Friday 8pm
    assert datetime.fromtimestamp(next_run(sched, fri_eve)).weekday() == 0   # Monday
    assert describe(sched) == "every weekday at 8:00 AM"


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    from core.config import config
    config.set("automation.speak_reminders", True, persist=False)
    path = str(tmp_path / "automations.db")

    def make():
        s = Scheduler()
        s._store = AutomationStore(path)
        s.announce = lambda text: spoken.append(text)
        return s
    spoken = []
    return make, spoken


def test_persist_restart_execute_cancel(fresh):
    make, spoken = fresh
    s1 = make()
    a = s1.create("reminder", "Work on AIDE", {"type": "once", "at": time.time() + 3600})
    b = s1.create("reminder", "Stretch", {"type": "once", "at": time.time() + 7200})
    # "Restart": a new scheduler on the same database still has both.
    s2 = make()
    ids = {x.id for x in s2.list(active_only=True)}
    assert {a.id, b.id} <= ids
    # Execute one for real (due now) through the run loop.
    s2.store.save(_due(s2.store.get(a.id)))
    s2.start()
    deadline = time.time() + 5
    while time.time() < deadline and s2.store.get(a.id).status == ACTIVE:
        time.sleep(0.05)
    s2.stop()
    assert s2.store.get(a.id).status == COMPLETED
    assert any("Work on AIDE" in t for t in spoken)
    # Cancel the other one; it stays cancelled after another restart.
    s2.cancel(b.id)
    assert make().store.get(b.id).status == CANCELLED


def _due(a):
    a.next_run = time.time() - 1
    return a


def test_missed_reminder_fires_on_startup(fresh):
    make, spoken = fresh
    s = make()
    a = s.create("reminder", "Missed one", {"type": "once", "at": time.time() + 3600})
    a.next_run = time.time() - 600            # became due 10 min ago while "closed"
    s.store.save(a)
    old = s.create("reminder", "Ancient", {"type": "once", "at": time.time() + 3600})
    old.next_run = time.time() - 3 * 86400
    s.store.save(old)
    s2 = make()
    s2._catch_up()
    assert s2.store.get(a.id).status == COMPLETED
    assert any("missed" in t.lower() for t in spoken)
    assert s2.store.get(old.id).status == MISSED


def test_recurring_reschedules(fresh):
    make, _ = fresh
    s = make()
    a = s.create("reminder", "Water", {"type": "interval", "every_sec": 3600, "start": time.time() + 3600})
    a.next_run = time.time() - 1
    s.store.save(a)
    s._execute(s.store.get(a.id))
    after = s.store.get(a.id)
    assert after.status == ACTIVE and after.next_run > time.time() and after.run_count == 1


def test_command_automation_runs_agent(fresh):
    make, _ = fresh
    s = make()
    ran = []
    s.run_command = lambda text: ran.append(text) or "ok"
    a = s.create("command", "pause the music", {"type": "once", "at": time.time() + 60})
    s._execute(s.store.get(a.id))
    assert ran == ["pause the music"]


def test_past_time_rejected(fresh):
    make, _ = fresh
    with pytest.raises(ValueError):
        make().create("reminder", "x", {"type": "once", "at": time.time() - 100})

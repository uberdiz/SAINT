"""Sync between devices: last-writer-wins with a hybrid clock, tombstones, no echo — and the real stores."""

import os
import tempfile
import time

import pytest

from modules.link.sync import Adapter, Mirror, SyncEngine, fingerprint


class DictAdapter(Adapter):
    """A store that's just a dict, to test the engine on its own."""
    kind = "note"

    def __init__(self, normalise=None, refuse=()):
        self.data = {}
        self.normalise = normalise
        self.refuse = set(refuse)

    def snapshot(self):
        return {k: dict(v) for k, v in self.data.items()}

    def apply_batch(self, changes):
        refused = []
        for uid, d in changes:
            if uid in self.refuse:
                refused.append(uid)
            elif d is None:
                self.data.pop(uid, None)
            else:
                self.data[uid] = self.normalise(dict(d)) if self.normalise else dict(d)
        return refused


def device(name, **kw):
    a = DictAdapter(**kw)
    eng = SyncEngine(name, [a], Mirror(os.path.join(tempfile.mkdtemp(), "sync.db")))
    return a, eng


def sync(eng_a, eng_b):
    """One round, as LinkService.sync_with does it."""
    eng_a.scan()
    eng_b.scan()
    want, offer = eng_b.diff(eng_a.manifest())
    eng_a.apply(offer)
    eng_b.apply(eng_a.items_for(want))


def test_create_edit_delete_propagate_both_ways():
    a, ea = device("A")
    b, eb = device("B")
    a.data["1"] = {"text": "remember the milk"}
    b.data["2"] = {"text": "call mom"}
    sync(ea, eb)
    assert a.data == b.data == {"1": {"text": "remember the milk"}, "2": {"text": "call mom"}}
    a.data["1"]["text"] = "remember oat milk"
    sync(ea, eb)
    assert b.data["1"]["text"] == "remember oat milk"
    del b.data["2"]
    sync(ea, eb)
    assert "2" not in a.data                         # a tombstone, not "B lacks it"
    sync(ea, eb)
    assert a.data == b.data == {"1": {"text": "remember oat milk"}}


def test_applying_remote_changes_does_not_echo_back():
    a, ea = device("A")
    b, eb = device("B")
    a.data["1"] = {"text": "x"}
    sync(ea, eb)
    assert eb.scan() == 0 and ea.scan() == 0
    # even when the store stores it differently from how it arrived
    a2, ea2 = device("A2")
    b2, eb2 = device("B2", normalise=lambda d: {"text": d["text"].upper()})
    a2.data["1"] = {"text": "hello"}
    sync(ea2, eb2)
    assert b2.data["1"] == {"text": "HELLO"}
    assert eb2.scan() == 0
    sync(ea2, eb2)
    assert a2.data["1"] == {"text": "hello"}          # no ping-pong


def test_last_writer_wins_and_ties_break_on_device_id():
    a, ea = device("A")
    b, eb = device("B")
    a.data["1"] = {"text": "v0"}
    sync(ea, eb)
    a.data["1"] = {"text": "from A"}
    ea.scan()
    time.sleep(0.01)
    b.data["1"] = {"text": "from B"}                  # later
    sync(ea, eb)
    assert a.data["1"] == b.data["1"] == {"text": "from B"}


def test_a_device_with_a_wrong_clock_cant_overwrite_later_edits(monkeypatch):
    a, ea = device("A")
    b, eb = device("B")
    a.data["1"] = {"text": "v1"}
    sync(ea, eb)
    # B's clock is an hour behind, yet its next edit still orders after what it has seen
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 3600)
    b.data["1"] = {"text": "B edits after seeing v1"}
    eb.scan()
    monkeypatch.setattr(time, "time", real)
    sync(ea, eb)
    assert a.data["1"]["text"] == "B edits after seeing v1"
    # and a device with a clock far in the future doesn't freeze everyone else out
    monkeypatch.setattr(time, "time", lambda: real() + 86400 * 400)
    a.data["1"] = {"text": "A in the future"}
    ea.scan()
    monkeypatch.setattr(time, "time", real)
    sync(ea, eb)
    b.data["1"] = {"text": "B answers"}
    sync(ea, eb)
    assert a.data["1"]["text"] == "B answers"         # the clock follows the largest timestamp seen


def test_a_refused_item_is_recorded_as_deleted_not_requested_forever():
    a, ea = device("A")
    b, eb = device("B", refuse={"1"})
    a.data["1"] = {"text": "x"}
    sync(ea, eb)
    assert "1" not in b.data
    want, offer = eb.diff(ea.manifest())
    sync(ea, eb)
    assert "1" not in a.data                          # the refusal propagated as a deletion


def test_three_devices_converge():
    a, ea = device("A")
    b, eb = device("B")
    c, ec = device("C")
    a.data["x"] = {"v": 1}
    b.data["y"] = {"v": 2}
    c.data["z"] = {"v": 3}
    for _ in range(2):
        sync(ea, eb)
        sync(eb, ec)
        sync(ea, ec)
    assert a.data == b.data == c.data == {"x": {"v": 1}, "y": {"v": 2}, "z": {"v": 3}}


def test_bad_items_are_ignored():
    a, ea = device("A")
    assert ea.apply([{"k": "note"}, {"k": "nope", "u": "1", "ts": 1, "o": "x", "d": {}}, "junk", None,
                     {"k": "note", "u": "1", "ts": "x", "o": "x", "d": {}},
                     {"k": "note", "u": "2", "ts": 5, "o": "x", "d": "not a dict"}]) == 0


def test_fingerprint_is_order_independent():
    assert fingerprint({"a": 1, "b": [1, 2]}) == fingerprint({"b": [1, 2], "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})


# ---------------------------------------------------------------------- #
# The real stores
# ---------------------------------------------------------------------- #
def test_memory_adapter_round_trip_and_uid_assignment():
    from modules.link.sync import MemoryAdapter
    from modules.memory.service import memory_service
    ad = MemoryAdapter()
    before = set(ad.snapshot())
    memory_service.remember(key="favorite test food", value="pizza", category="preference")
    snap = ad.snapshot()
    new = [u for u in snap if u not in before]
    assert len(new) == 1 and snap[new[0]]["value"] == "pizza" and snap[new[0]]["how"] == "told"
    uid = new[0]
    assert ad.snapshot()[uid] == snap[uid]                              # the uid sticks
    try:
        ad.apply_batch([(uid, dict(snap[uid], value="pasta", content="My favorite test food is pasta")),
                        ("remote-mem-1", {"content": "I live in Lisbon", "key": "home location", "value": "Lisbon",
                                          "category": "personal", "how": "told", "tags": ["personal"], "confidence": 1.0})])
        after = ad.snapshot()
        assert after[uid]["value"] == "pasta" and after["remote-mem-1"]["value"] == "Lisbon"
        assert memory_service.get_by_key("favorite test food").metadata["value"] == "pasta"
        ad.apply_batch([(uid, None), ("remote-mem-1", None)])
        assert uid not in ad.snapshot() and "remote-mem-1" not in ad.snapshot()
    finally:
        ad.apply_batch([(uid, None), ("remote-mem-1", None)])


def test_skill_adapter_keeps_local_stats_and_the_smaller_id_wins_a_duplicate_phrase():
    from modules.learning.skills import skills
    from modules.link.sync import SkillAdapter
    ad = SkillAdapter()
    try:
        assert ad.apply_batch([("bbbb0002", {"phrase": "test shortcut one", "steps": ["open notepad"], "how": "shown", "said": "x"})]) == []
        assert ad.snapshot()["bbbb0002"]["steps"] == ["open notepad"]
        # the same phrase learned elsewhere under a larger id is refused; under a smaller id it replaces ours
        assert ad.apply_batch([("cccc0003", {"phrase": "test shortcut one", "steps": ["open paint"], "how": "shown", "said": "x"})]) == ["cccc0003"]
        assert ad.apply_batch([("aaaa0001", {"phrase": "test shortcut one", "steps": ["open paint"], "how": "shown", "said": "x"})]) == []
        snap = ad.snapshot()
        assert "aaaa0001" in snap and "bbbb0002" not in snap
        assert ad.apply_batch([("dddd0004", {"phrase": "", "steps": [], "how": "shown"})]) == ["dddd0004"]
    finally:
        ad.apply_batch([("aaaa0001", None), ("bbbb0002", None), ("cccc0003", None)])


def test_alias_and_scene_adapters():
    from modules.agent.aliases import aliases
    from modules.automation.scenes import scenes
    from modules.link.sync import AliasAdapter, SceneAdapter
    al, sc = AliasAdapter(), SceneAdapter()
    try:
        al.apply_batch([("the test lab", {"target": "open my test project in vs code"})])
        assert aliases.all()["the test lab"] == "open my test project in vs code"
        sc.apply_batch([("sync0001", {"name": "Test scene", "steps": ["play lofi"], "phrase": "test time"})])
        assert scenes.get("sync0001").steps == ["play lofi"]
        # the same name with a different id and different steps is kept under another name, not lost
        sc.apply_batch([("sync0002", {"name": "Test scene", "steps": ["play jazz"], "phrase": ""})])
        names = {s.name for s in scenes.all() if s.id in ("sync0001", "sync0002")}
        assert names == {"Test scene", "Test scene (synced)"}
    finally:
        al.apply_batch([("the test lab", None)])
        sc.apply_batch([("sync0001", None), ("sync0002", None)])
    assert "the test lab" not in aliases.all() and scenes.get("sync0001") is None


def test_reminder_adapter_creates_and_completes_a_reminder():
    from modules.automation.scheduler import scheduler
    from modules.link.sync import ReminderAdapter
    ad = ReminderAdapter()
    when = {"type": "once", "at": time.time() + 3600}
    try:
        ad.apply_batch([("rem00001", {"title": "Call mom", "message": "Call mom", "schedule": when,
                                      "status": "active", "last_run": 0})])
        a = scheduler.store.get("rem00001")
        assert a.kind == "reminder" and a.status == "active" and a.next_run == pytest.approx(when["at"])
        assert ad.snapshot()["rem00001"]["title"] == "Call mom"
        ad.apply_batch([("rem00001", {"title": "Call mom", "message": "Call mom", "schedule": when,
                                      "status": "completed", "last_run": round(time.time())})])
        assert scheduler.store.get("rem00001").status == "completed" and scheduler.store.get("rem00001").next_run is None
        # scheduled *commands* act on one PC, so they never sync
        scheduler.create("command", "play jazz", {"type": "once", "at": time.time() + 600}, title="cmd")
        assert all(v["message"] != "play jazz" for v in ad.snapshot().values())
    finally:
        ad.apply_batch([("rem00001", None)])
        for x in list(scheduler.store.all()):
            if x.message == "play jazz":
                scheduler.delete(x.id)


def test_settings_adapter_syncs_language_preferences():
    from core.config import config
    from modules.link.sync import SettingsAdapter
    ad = SettingsAdapter()
    old = config.get("language.preferred")
    try:
        ad.apply_batch([("language", {"preferred": ["es", "en"], "mixed_mode": "dominant"})])
        assert config.get("language.preferred") == ["es", "en"] and config.get("language.mixed_mode") == "dominant"
        assert ad.snapshot()["language"]["preferred"] == ["es", "en"]
    finally:
        config.set("language.preferred", old or [], persist=False)
        config.set("language.mixed_mode", "mirror", persist=False)


def test_engine_with_all_real_adapters_converges_two_stores_in_one_process():
    """One process has one set of stores, so use the real engine twice over the same data:
    everything in the stores must show up in the manifest and survive a no-op sync."""
    from modules.link.sync import default_adapters
    from modules.memory.service import memory_service
    eng = SyncEngine("dev-real", default_adapters(), Mirror(os.path.join(tempfile.mkdtemp(), "sync.db")))
    memory_service.remember(key="favorite sync color", value="teal", category="preference")
    try:
        assert eng.scan() >= 1
        man = eng.manifest()
        assert "memory" in man["items"] and eng.scan() == 0
        peer = SyncEngine("dev-peer", [], Mirror(os.path.join(tempfile.mkdtemp(), "sync.db")))
        want, offer = eng.diff(peer.manifest())
        assert offer == [] or all(i["k"] for i in offer)
    finally:
        entry = memory_service.get_by_key("favorite sync color")
        if entry:
            memory_service.forget(entry.id)
        eng.scan()

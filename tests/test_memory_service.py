"""Structured long-term memory: store, recall, update, delete, survive restart."""

import pytest

from modules.memory import database
from modules.memory.service import MemoryService, extract_fact
from modules.agent.agent import agent


@pytest.fixture
def svc(tmp_path, monkeypatch):
    db = database.MemoryDatabase(str(tmp_path / "mem.db"))
    monkeypatch.setattr(database, "_memory_db", db)
    return MemoryService()


def test_extract_fact():
    f = extract_fact("My favorite programming language is Python.")
    assert f["key"] == "favorite programming language" and f["value"] == "Python"
    assert extract_fact("what is my favorite color?") is None
    assert extract_fact("call me Sam")["value"] == "Sam"


def test_store_recall_update_delete(svc):
    r = svc.remember(key="favorite programming language", value="Python", category="preference")
    assert not r["updated"]
    hits = svc.recall("What programming language do I like?")
    assert hits and hits[0].value == "Python"
    r2 = svc.remember(key="favourite programming language", value="Rust", category="preference")
    assert r2["updated"] and r2["previous"] == "Python"
    assert len(svc.all()) == 1
    assert svc.recall("programming language")[0].value == "Rust"
    deleted = svc.forget_matching("favorite programming language")
    assert deleted and not svc.all()


def test_persists_across_restart(tmp_path, monkeypatch):
    path = str(tmp_path / "persist.db")
    monkeypatch.setattr(database, "_memory_db", database.MemoryDatabase(path))
    MemoryService().remember(key="favorite programming language", value="Python", category="preference")
    # "Restart": brand-new database object + service on the same file.
    monkeypatch.setattr(database, "_memory_db", database.MemoryDatabase(path))
    hits = MemoryService().recall("what programming language do I like")
    assert hits and hits[0].value == "Python"


def test_agent_end_to_end(svc):
    from core.module_manager import module_manager  # registers memory tools
    assert module_manager.get("memory").enabled
    r = agent.handle("My favorite programming language is Python.")
    assert r is not None and "python" in r.text.lower()
    r = agent.handle("What programming language do I like?")
    assert r.text == "Your favorite programming language is Python."
    r = agent.handle("What's my favorite color?")
    assert "don't have anything stored" in r.text
    r = agent.handle("forget my favorite programming language")
    assert "forgotten" in r.text.lower()
    r = agent.handle("What programming language do I like?")
    assert "don't have anything stored" in r.text


def test_context_for_llm(svc):
    svc.remember(key="favorite band", value="Radiohead", category="preference")
    assert any("Radiohead" in c for c in svc.context_for("recommend me a band I'd like"))
    assert svc.context_for("what's the weather") == []

"""Memory acquisition (passive + model), retention (reinforce / fade / never overwrite what you said) and the profile."""

import time

import pytest

from core.config import config
from modules.memory import database
from modules.memory import learner as learner_mod
from modules.memory import profile
from modules.memory.passive import extract
from modules.memory.service import LEARNED, TOLD, MemoryService


@pytest.fixture
def svc(tmp_path, monkeypatch):
    db = database.MemoryDatabase(str(tmp_path / "mem.db"))
    monkeypatch.setattr(database, "_memory_db", db)
    import modules.memory.service as service_mod
    s = MemoryService()
    monkeypatch.setattr(service_mod, "memory_service", s)
    return s


@pytest.mark.parametrize("text,key,value", [
    ("I'm a software engineer at Google.", "occupation", "software engineer at Google"),
    ("I'm 17 years old", "age", "17"),
    ("I'm from Toronto", "hometown", "Toronto"),
    ("I have a dog named Max", "pet dog", "Max"),
    ("My sister's name is Mia", "sister's name", "Mia"),
    ("I'm learning Japanese", "learning japanese", "Japanese"),
    ("I really love hiking", "likes hiking", "hiking"),
    ("I hate horror movies because they're scary", "dislikes horror movies", "horror movies"),
    ("I'm a big fan of Drake", "likes drake", "Drake"),
    ("I play Valorant", "plays valorant", "Valorant"),
])
def test_passive_statements(text, key, value):
    facts = extract(text)
    assert facts and facts[0]["key"] == key and facts[0]["value"] == value


@pytest.mark.parametrize("text", ["I'm tired", "I love you", "I like that", "open spotify", "what do I like?",
                                  "I'm going to the store", "I'm so hungry right now"])
def test_passive_ignores_moods_requests_and_questions(text):
    assert extract(text) == []


def test_learned_fact_is_reinforced_not_duplicated(svc):
    svc.remember(content="I like hiking", key="likes hiking", value="hiking", category="interest",
                 confidence=0.6, how=LEARNED)
    r = svc.remember(content="I like hiking", key="likes hiking", value="hiking", category="interest",
                     confidence=0.6, how=LEARNED)
    assert r["reinforced"]
    (e,) = svc.all()
    assert e.metadata["mentions"] == 2 and e.confidence > 0.6 and e.metadata["how"] == LEARNED


def test_a_guess_never_overwrites_what_you_said(svc):
    svc.remember(key="home location", value="Toronto", category="personal")
    r = svc.remember(content="I live in Paris", key="home location", value="Paris", how=LEARNED, confidence=0.7)
    assert r.get("kept")
    assert svc.get_by_key("home location").metadata["value"] == "Toronto"


def test_saying_it_outright_upgrades_a_learned_fact(svc):
    svc.remember(content="I'm a nurse", key="occupation", value="nurse", category="identity", how=LEARNED,
                 confidence=0.6)
    svc.remember(content="I'm a nurse", key="occupation", value="nurse", category="identity")
    e = svc.get_by_key("occupation")
    assert e.metadata["how"] == TOLD and e.confidence == 1.0


def test_unconfirmed_learned_facts_fade_but_told_ones_stay(svc, monkeypatch):
    config.set("memory.learned_ttl_days", 30, persist=False)
    svc.remember(key="favorite color", value="green", category="preference")
    svc.remember(content="I like chess", key="likes chess", value="chess", category="interest", how=LEARNED,
                 confidence=0.6)
    confirmed = svc.remember(content="I like golf", key="likes golf", value="golf", category="interest",
                             how=LEARNED, confidence=0.6)
    svc.confirm(confirmed["id"])
    later = time.time() + 120 * 86400
    assert svc.consolidate(now=later)["faded"] == 1
    assert sorted(e.metadata["key"] for e in svc.all()) == ["favorite color", "likes golf"]


def test_near_duplicates_merge(svc):
    svc.remember(content="I love hiking in the mountains")
    r = svc.remember(content="I love hiking in the mountains!")
    assert r["reinforced"] and len(svc.all()) == 1


def test_core_facts_lead_with_identity(svc):
    svc.remember(content="I like chess", key="likes chess", value="chess", category="interest")
    svc.remember(key="name", value="Sam", category="identity", content="My name is Sam")
    assert svc.core_facts()[0] == "My name is Sam"


def test_learner_stores_passive_facts_and_skips_the_model(svc, monkeypatch):
    config.set("memory.learn_with_ai", True, persist=False)
    calls = []
    monkeypatch.setattr(learner_mod.MemoryLearner, "_ask_model", staticmethod(lambda t: calls.append(t) or ""))
    stored = learner_mod.MemoryLearner().learn("I'm a nurse")
    assert stored and svc.get_by_key("occupation").metadata["how"] == LEARNED
    assert calls == []                                   # a plain statement never needs the model


def test_learner_uses_the_model_for_harder_sentences(svc, monkeypatch):
    config.set("memory.learn_with_ai", True, persist=False)
    monkeypatch.setattr(learner_mod.MemoryLearner, "_wait_until_quiet", staticmethod(lambda limit=0: None))
    monkeypatch.setattr(learner_mod.MemoryLearner, "_ask_model", staticmethod(
        lambda t: '{"facts": [{"statement": "I work night shifts at the hospital", "key": "work schedule", '
                  '"value": "night shifts", "category": "routine", "confidence": 0.9}]}'))
    learner_mod.MemoryLearner().learn("honestly my week is rough since I work night shifts at the hospital")
    e = svc.get_by_key("work schedule")
    assert e is not None and e.metadata["how"] == LEARNED and e.confidence <= 0.8


def test_parse_facts_is_defensive():
    assert learner_mod.parse_facts("") == []
    assert learner_mod.parse_facts("not json") == []
    assert learner_mod.parse_facts('{"facts": [{"statement": "Is it?", "confidence": 1}]}') == []
    assert learner_mod.parse_facts('{"facts": [{"statement": "I use Linux", "confidence": 0.3}]}') == []
    got = learner_mod.parse_facts('```json\n{"facts": [{"statement": "i use Linux", "key": "os", '
                                  '"value": "Linux", "category": "interest"}]}\n```')
    assert got[0]["content"] == "I use Linux"


def test_worth_learning():
    assert learner_mod.worth_learning("my brother and I build robots on weekends")
    assert not learner_mod.worth_learning("open spotify please now")
    assert not learner_mod.worth_learning("what is my name?")
    assert not learner_mod.worth_learning("nice")


def test_profile_groups_and_habits(svc, monkeypatch):
    svc.remember(key="name", value="Sam", category="identity", content="My name is Sam")
    svc.remember(content="I'm a nurse", key="occupation", value="nurse", category="identity")
    svc.remember(content="I like chess", key="likes chess", value="chess", category="interest", how=LEARNED,
                 confidence=0.7)
    svc.remember(content="My sister's name is Mia", key="sister's name", value="Mia", category="person")
    now = time.time()
    entries = [{"ts": now - i * 3600, "source": "voice", "user": "search youtube for cats",
                "tools": [{"tool": "desktop.web_search"}]} for i in range(5)] + \
              [{"ts": now, "source": "typed", "user": "open discord", "tools": [{"tool": "desktop.open_app"}]}]
    monkeypatch.setattr(profile, "habits", lambda e=None, _entries=entries: profile.__dict__["_real_habits"](_entries))
    p = profile.build()
    assert p["name"] == "Sam" and p["identity"]["occupation"] == "nurse"
    assert [i["label"] for i in p["likes"]] == ["Chess"] and p["likes"][0]["how"] == LEARNED
    assert p["people"][0]["label"] == "Mia (sister)"
    h = p["habits"]
    assert h["requests"] == 6 and "YouTube" in h["top_sites"] and "Discord" in h["top_apps"]
    assert profile.fallback_summary(p).startswith("You're a nurse")


profile.__dict__["_real_habits"] = profile.habits

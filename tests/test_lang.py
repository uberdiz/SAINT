"""SAINT in more than one language (and two at once): detection, command translation, replies.

The cases live in tests/data/lang_cases.json, which the iOS app's tests run too.
"""

import json
import os

import pytest

from core.config import config
from modules import lang
from modules.lang import detect, pack, reply
from modules.lang.segments import segments

CASES = json.load(open(os.path.join(os.path.dirname(__file__), "data", "lang_cases.json"), encoding="utf-8"))


@pytest.fixture(autouse=True)
def fresh_state():
    lang.state.reset()
    config.set("language.preferred", [], persist=False)
    config.set("language.auto_detect", True, persist=False)
    config.set("language.reply_in_user_language", True, persist=False)
    config.set("agent.enabled", True, persist=False)          # another test file can leave it off
    yield
    lang.state.reset()
    config.set("language.preferred", [], persist=False)


@pytest.mark.parametrize("case", CASES["commands"], ids=[c["said"][:40] for c in CASES["commands"]])
def test_commands_become_the_english_the_router_knows(case):
    config.set("language.preferred", case.get("prefer", []), persist=False)
    turn = lang.analyze(case["said"])
    wanted = case["lang"] if isinstance(case["lang"], list) else [case["lang"]]
    assert turn.language in wanted
    assert turn.english == case["english"]
    if "mixed" in case:
        assert turn.mixed == case["mixed"]


@pytest.mark.parametrize("text", CASES["english_unchanged"])
def test_plain_english_is_left_alone(text):
    turn = lang.analyze(text)
    assert turn.language == "en" and not turn.translated and turn.routed_text == text


@pytest.mark.parametrize("case", CASES["replies"], ids=[f"{c['lang']}:{c['en'][:30]}" for c in CASES["replies"]])
def test_replies_come_back_in_the_users_language(case):
    assert reply.localize(case["en"], case["lang"], allow_llm=False) == case["out"]


@pytest.mark.parametrize("case", CASES["detect"], ids=[c["text"][:30] for c in CASES["detect"]])
def test_detection(case):
    det = detect.detect(case["text"], prefer=case.get("prefer", []))
    assert det.primary == case["primary"] and det.mixed == case["mixed"]
    if case.get("secondary"):
        assert det.secondary == case["secondary"]


# Things that are commands in English but only by the router, not by a pattern: skip these when
# checking that every translated command is routable.
_NOT_ROUTED = {"yes", "no", "thank you", "stop", "stop everything", "never mind", "what are you doing",
               "you can talk again"}


def test_every_english_command_we_produce_is_one_the_router_understands():
    """The translation is only useful if the router really understands its output."""
    from modules.agent.router import route
    from modules.agent import meta
    failures = []
    for case in CASES["commands"]:
        english = case["english"]
        if not english or english in _NOT_ROUTED:
            continue
        if route(english) is None and meta.match_meta(english) is None:
            failures.append((case["said"], english))
    assert failures == []


def test_spanish_reminder_becomes_a_real_schedule():
    from modules.automation.timeparse import extract_reminder
    english = lang.analyze("recuérdame llamar a mamá a las 5 de la tarde").english
    rem = extract_reminder(english)
    assert rem and rem["schedule"] and rem["message"].lower().startswith("llamar a mam")
    from datetime import datetime
    at = datetime.fromtimestamp(rem["schedule"]["at"]) if rem["schedule"]["type"] == "once" else None
    assert at and (at.hour, at.minute) == (17, 0)
    weekly = extract_reminder(lang.analyze("recuérdame los lunes a las 7 hacer ejercicio").english)
    # "at 7" means 7 PM in SAINT's reminder parser (as in English); "a las 7 de la mañana" says otherwise
    assert weekly["schedule"]["type"] == "daily" and weekly["schedule"]["days"] == [0] and weekly["schedule"]["time"] == "19:00"
    morning = extract_reminder(lang.analyze("recuérdame los lunes a las 7 de la mañana hacer ejercicio").english)
    assert morning["schedule"]["time"] == "07:00"
    timer = extract_reminder(lang.analyze("pon un temporizador de 10 minutos").english)
    assert timer["timer"] and timer["schedule"]["type"] == "once"


def test_a_one_word_answer_keeps_the_language_of_the_conversation():
    assert lang.analyze("¿qué hora es?").language == "es"
    follow = lang.analyze("ok")                                  # no language of its own
    assert follow.language == "es"
    assert lang.analyze("what time is it").language == "en"      # a clear English sentence switches back
    assert lang.analyze("ok").language == "en"


def test_preferred_languages_break_ties():
    assert detect.detect("pausa", prefer=["pt", "es"]).candidates[0] == "pt"
    assert lang.analyze("pausa").language == "es"
    lang.state.reset()
    config.set("language.preferred", ["it"], persist=False)
    assert lang.analyze("pausa").language == "it"                 # "pausa" is also Italian


def test_turning_it_off_makes_everything_plain_english():
    config.set("language.auto_detect", False, persist=False)
    turn = lang.analyze("pon música de Bad Bunny")
    assert not turn.translated and turn.language == "en"
    config.set("language.auto_detect", True, persist=False)
    config.set("language.reply_in_user_language", False, persist=False)
    turn = lang.analyze("pon música de Bad Bunny")
    assert turn.english == "play Bad Bunny" and turn.language == "en"      # understood, answered in English


def test_directive_for_the_language_model():
    assert lang.analyze("what time is it").directive() == ""
    d = lang.analyze("¿qué hora es?").directive()
    assert "Spanish" in d and "only" in d
    config.set("language.preferred", ["es"], persist=False)
    mixed = lang.analyze("pon some jazz")
    assert mixed.mixed and "mixes Spanish and English" in mixed.directive()
    config.set("language.mixed_mode", "dominant", persist=False)
    assert "mixes" not in lang.analyze("pon some jazz").directive()
    config.set("language.mixed_mode", "mirror", persist=False)


def test_segments_split_mixed_speech_for_text_to_speech():
    assert segments("Reproduciendo Bad Bunny") == [("es", "Reproduciendo Bad Bunny")]
    assert segments("What time is it") == [("en", "What time is it")]
    parts = segments("Reproduciendo la canción, and I'll queue up more songs like it")
    assert [p[0] for p in parts] == ["es", "en"] and "".join(p[1] for p in parts) == \
        "Reproduciendo la canción, and I'll queue up more songs like it"
    assert segments("") == [] and segments("   ") == []


def test_every_pack_loads_and_every_pattern_compiles():
    assert set(pack.available()) == {"es", "fr", "pt", "de", "it"}
    for code in pack.available():
        p = pack.get_pack(code)
        assert p.commands and p.phrasebook and p.time_rules and p.weekdays and len(p.months) == 12
        assert p.locale and p.english


def test_unknown_language_reply_is_left_in_english_without_a_model():
    assert reply.localize("Something nobody translated.", "es", allow_llm=False) == "Something nobody translated."
    assert reply.localize("Paused.", "zz", allow_llm=False) == "Paused."


def test_the_language_model_translates_what_the_phrasebook_cant(monkeypatch):
    monkeypatch.setattr(reply, "_llm_cached", lambda text, language: f"[{language}] {text}")
    config.set("language.llm_translate", True, persist=False)
    assert reply.localize("Something nobody translated.", "es") == "[Spanish] Something nobody translated."
    config.set("language.llm_translate", False, persist=False)
    assert reply.localize("Something nobody translated.", "es") == "Something nobody translated."
    config.set("language.llm_translate", True, persist=False)


# ---------------------------------------------------------------------- #
# Through the AI module, as a spoken or typed request arrives
# ---------------------------------------------------------------------- #
def _ask(text, language=""):
    from core.module_manager import module_manager
    ai = module_manager.get("ai")
    out = []
    ai.stream_prompt(text, on_token=out.append, language=language)
    return "".join(out), ai


def test_through_the_ai_module_spanish_in_spanish_out():
    text, ai = _ask("¿qué hora es?")
    assert text.startswith("Son las ") and ai.last_language == "es"
    text, ai = _ask("¿qué día es hoy?")
    assert text.startswith("Hoy es ") and " de " in text
    text, ai = _ask("what time is it")
    assert text.startswith("It's ") and ai.last_language == "en"


def test_through_the_ai_module_a_spanish_reminder_is_actually_scheduled():
    from modules.automation.scheduler import scheduler
    before = {a.id for a in scheduler.store.all()}
    text, _ = _ask("recuérdame llamar al dentista mañana a las 9 de la mañana")
    new = [a for a in scheduler.store.all() if a.id not in before]
    try:
        assert len(new) == 1 and new[0].kind == "reminder" and new[0].message.lower().startswith("llamar al dentista")
        assert text.startswith("Vale, recordatorio para") and "mañana" in text
    finally:
        for a in new:
            scheduler.delete(a.id)


def test_follow_up_gate_understands_spanish():
    from modules.agent.agent import agent
    assert agent.accepts_followup("salta esta canción")
    assert agent.accepts_followup("sube el volumen")
    assert not agent.accepts_followup("me encanta esta receta de la abuela con mucho ajo")

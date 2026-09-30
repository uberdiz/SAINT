"""
modules/lang/pack.py

Language packs: one JSON file per language in modules/lang/lexicon/, loaded once.

A pack holds everything SAINT knows about a language as data, so adding or
fixing one never touches code (and the iPhone app reads the very same files):

    words        common words, used to tell the language (accent-folded)
    chars        letters that only this language uses among the supported ones
    verbs        a leading command word -> its English command word ("pon" -> "play")
    commands     [[regex, template], ...] full-sentence patterns -> an English command
    apps, keys, units, numbers   small dictionaries the templates can use
    time_rules   [[regex, replacement], ...] turning a spoken time into English
    phrasebook   [[regex on the English reply, template], ...] English replies -> this language
    weekdays, months, when_rules   for dates in replies

Patterns are written for *folded* text: lower case with accents removed letter
for letter ("canción" -> "cancion", "mañana" -> "manana"), so they match whether
or not the speech recogniser wrote the accents. Captured text is always taken
from the original, so "Tití Me Preguntó" stays exactly as said.
"""

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger("saint.lang")

LEXICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lexicon")
SUPPORTED = ("es", "fr", "pt", "de", "it")

# Languages known only by their script (no command pack: the language model handles them).
SCRIPT_LANGS = {"ja": "Japanese", "zh": "Chinese", "ko": "Korean", "ru": "Russian", "ar": "Arabic",
                "hi": "Hindi", "th": "Thai", "el": "Greek", "he": "Hebrew"}


def fold_char(c: str) -> str:
    """Lower case, accents off, always exactly one character back."""
    c = c.lower()
    if c.isascii():
        return c
    if c == "ß":
        return "s"
    base = unicodedata.normalize("NFD", c)[:1]
    return base if base and base.isascii() else c


def fold(text: str) -> str:
    return "".join(fold_char(c) for c in text or "")


@dataclass
class Command:
    regex: "re.Pattern"
    to: str


@dataclass
class Pack:
    code: str
    name: str = ""            # in the language itself ("Español")
    english: str = ""         # in English ("Spanish"), for prompts
    words: frozenset = frozenset()
    chars: str = ""
    verbs: Dict[str, str] = field(default_factory=dict)
    commands: List[Command] = field(default_factory=list)
    apps: Dict[str, str] = field(default_factory=dict)
    keys: Dict[str, str] = field(default_factory=dict)
    key_names: Dict[str, str] = field(default_factory=dict)       # english noun -> the noun as written in this language
    units: Dict[str, str] = field(default_factory=dict)
    numbers: Dict[str, int] = field(default_factory=dict)
    time_rules: List[tuple] = field(default_factory=list)
    clock: Dict[str, str] = field(default_factory=dict)
    phrasebook: List[Command] = field(default_factory=list)
    weekdays: List[str] = field(default_factory=list)
    months: List[str] = field(default_factory=list)
    when_rules: List[tuple] = field(default_factory=list)
    polite_tail: Optional["re.Pattern"] = None
    filler: Optional["re.Pattern"] = None
    wake_prefix: Optional["re.Pattern"] = None
    tts_voice: str = ""
    tts_lang: str = ""
    locale: str = ""


_packs: Dict[str, Pack] = {}
_english: Optional[frozenset] = None


def _compile(p: str, flags=re.I | re.U) -> "re.Pattern":
    return re.compile(p, flags)


_REGEX_SYNTAX = re.compile(r"\(\?P<\w+>|\\[a-zA-Z]|\(\?[:=!]")


def _pattern_words(pattern: str) -> set:
    """The literal words inside a command pattern ("recuerdame", "volumen"): they are
    this language's command vocabulary, so a bare "pausa" still counts as Spanish."""
    return set(re.findall(r"[a-z]{3,}", _REGEX_SYNTAX.sub(" ", pattern)))


def _load(code: str) -> Optional[Pack]:
    path = os.path.join(LEXICON_DIR, f"{code}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        log.warning("lang.pack_missing %s", code)
        return None
    pack = Pack(code=code, name=raw.get("name", code), english=raw.get("english", raw.get("name", code)))
    pack.words = frozenset(fold(w) for w in raw.get("words", []))
    pack.chars = raw.get("chars", "")
    pack.verbs = {fold(k): v for k, v in (raw.get("verbs") or {}).items()}
    pack.apps = {fold(k): v for k, v in (raw.get("apps") or {}).items()}
    pack.keys = {fold(k): v for k, v in (raw.get("keys") or {}).items()}
    for native, english in (raw.get("keys") or {}).items():
        pack.key_names.setdefault(english, native)
    pack.units = {fold(k): v for k, v in (raw.get("units") or {}).items()}
    pack.numbers = {fold(k): int(v) for k, v in (raw.get("numbers") or {}).items()}
    pack.clock = raw.get("clock") or {}
    pack.weekdays = list(raw.get("weekdays") or [])
    pack.months = list(raw.get("months") or [])
    pack.tts_voice, pack.tts_lang, pack.locale = raw.get("tts_voice", ""), raw.get("tts_lang", ""), raw.get("locale", "")
    for c in raw.get("commands") or []:
        pattern, to = (c["re"], c["to"]) if isinstance(c, dict) else (c[0], c[1])
        try:
            pack.commands.append(Command(_compile(pattern), to))
        except re.error as e:
            log.error("lang.bad_command %s %r: %s", code, pattern, e)
    for c in raw.get("phrasebook") or []:
        pattern, to = (c["en"], c["to"]) if isinstance(c, dict) else (c[0], c[1])
        try:
            pack.phrasebook.append(Command(_compile("^" + pattern + "$"), to))
        except re.error as e:
            log.error("lang.bad_phrase %s %r: %s", code, pattern, e)
    for rule in raw.get("time_rules") or []:
        try:
            pack.time_rules.append((_compile(rule[0]), rule[1]))
        except (re.error, IndexError) as e:
            log.error("lang.bad_time_rule %s %r: %s", code, rule, e)
    for rule in raw.get("when_rules") or []:
        try:
            pack.when_rules.append((_compile(rule[0]), rule[1]))
        except (re.error, IndexError) as e:
            log.error("lang.bad_when_rule %s %r: %s", code, rule, e)
    extra = set(pack.verbs)
    for c in pack.commands:
        extra |= _pattern_words(c.regex.pattern)
    from_english = english_words()
    # What SAINT says in this language counts as this language too (so its own replies are recognised
    # when they're spoken back): the words of the phrasebook's answers, minus the {placeholders}.
    for c in pack.phrasebook:
        extra |= {fold(w) for w in re.findall(r"[^\W\d_]+", re.sub(r"\{[^}]*\}", " ", c.to))}
    pack.words = frozenset(pack.words | {w for w in extra if w not in from_english})
    if raw.get("translit"):                       # German typed without umlauts: spaet / spät
        pack.words = frozenset(pack.words | {w.replace("ae", "a").replace("oe", "o").replace("ue", "u")
                                             for w in pack.words})
    if raw.get("filler"):
        pack.filler = _compile(raw["filler"])
    if raw.get("polite_tail"):
        pack.polite_tail = _compile(raw["polite_tail"])
    if raw.get("wake_prefix"):
        pack.wake_prefix = _compile(raw["wake_prefix"])
    return pack


def get_pack(code: str) -> Optional[Pack]:
    if code not in _packs:
        _packs[code] = _load(code)
    return _packs[code]


def available() -> List[str]:
    return [c for c in SUPPORTED if get_pack(c) is not None]


def english_words() -> frozenset:
    global _english
    if _english is None:
        try:
            with open(os.path.join(LEXICON_DIR, "en.json"), "r", encoding="utf-8") as f:
                _english = frozenset(fold(w) for w in json.load(f).get("words", []))
        except (OSError, ValueError):
            _english = frozenset()
    return _english


def language_name(code: str) -> str:
    if code == "en":
        return "English"
    pack = get_pack(code)
    if pack and pack.english:
        return pack.english
    return SCRIPT_LANGS.get(code, code)


def reload():
    """Forget loaded packs (tests, or after editing a lexicon file)."""
    global _english
    _packs.clear()
    _english = None

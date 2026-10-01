"""
modules/lang/normalize.py

What the user said, as the English command SAINT's router already understands.

    "pon música de Bad Bunny"                  ->  "play Bad Bunny"
    "recuérdame llamar a mamá a las 5"         ->  "remind me at 5 to llamar a mamá"
    "pon some jazz"                            ->  "play some jazz"
    "sube el volumen"                          ->  "turn it up"
    "manda este prompt al PC de Gian en Claude: resume mis notas"
                                               ->  "send this prompt to Gian's PC on Claude: resume mis notas"

Only a command's *shape* is translated. What the user named — a song, an artist,
a person, the text of a reminder — is copied from what they said, accents and all,
so it still matches the real thing. Nothing here calls a model; a request no
pattern fits returns None and goes on to the language model as before.
"""

import re
from typing import Iterable, List, Optional

from modules.lang.pack import Pack, fold, get_pack

_PLACEHOLDER = re.compile(r"\{(\w+)(?::(\w+))?\}")
_EDGE = " \t\r\n¿¡?!.,;:"
_CLOCK = re.compile(r"\b(\d{1,2})\s+(y|menos|et|moins|e|meno|und|vor|nach)\s+(\w+)\b")


def strip_wake(text: str, pack: Optional[Pack] = None) -> str:
    t = re.sub(r"^\s*(?:hey\s+|ok(?:ay)?\s+)?saint\b[\s,.!:;-]*", "", text or "", flags=re.I)
    if pack is not None and pack.wake_prefix is not None:
        t = pack.wake_prefix.sub("", t)
    return t.strip()


def clean(text: str, pack: Optional[Pack] = None) -> str:
    t = strip_wake(text, pack).strip(_EDGE)
    if pack is not None and pack.filler is not None:
        t = pack.filler.sub("", t).strip(_EDGE)
    if pack is not None and pack.polite_tail is not None:
        t = pack.polite_tail.sub("", t).strip(_EDGE)
    return t


# ---------------------------------------------------------------------- #
# Times
# ---------------------------------------------------------------------- #
def _numbers(t: str, pack: Pack) -> str:
    if not pack.numbers:
        return t
    words = sorted(pack.numbers, key=len, reverse=True)
    rx = re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b")
    return rx.sub(lambda m: str(pack.numbers[m.group(1)]), t)


def _clock(t: str, pack: Pack) -> str:
    """"5 y media" / "5 menos cuarto" -> "5:30" / "4:45" (per pack.clock words)."""
    c = pack.clock
    if not c:
        return t

    def repl(m):
        hour, joiner, what = int(m.group(1)), m.group(2), m.group(3)
        if joiner not in (c.get("and"), c.get("minus")):
            return m.group(0)
        if what == c.get("half"):
            minutes = 30
        elif what == c.get("quarter"):
            minutes = 15
        elif what.isdigit():
            minutes = int(what)
        else:
            return m.group(0)
        if joiner == c.get("minus"):
            hour, minutes = (hour - 1) % 24, 60 - minutes
        return f"{hour}:{minutes:02d}"
    return _CLOCK.sub(repl, t)


def translate_time(phrase: str, pack: Pack) -> str:
    """A spoken time or duration as the English phrase SAINT's reminder parser reads."""
    t = fold(phrase).strip()
    t = _numbers(t, pack)
    t = _clock(t, pack)
    for rx, rep in pack.time_rules:
        t = rx.sub(rep, t)
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------------- #
# Templates
# ---------------------------------------------------------------------- #
def _modify(value: str, how: Optional[str], pack: Pack) -> str:
    if not how:
        return value
    f = fold(value).strip()
    if how == "time":
        return translate_time(value, pack)
    if how == "app":
        return pack.apps.get(f, value)
    if how == "key":
        return pack.keys.get(f, value)
    if how == "unit":
        return pack.units.get(f, value)
    if how == "num":
        return str(pack.numbers.get(f, value))
    return value


def render(template: str, m: "re.Match", original: str, pack: Pack) -> str:
    def sub(ph):
        name, how = ph.group(1), ph.group(2)
        try:
            a, b = m.span(int(name) if name.isdigit() else name)
        except IndexError:                 # an unknown group name
            return ""
        if a < 0:
            return ""
        return _modify(original[a:b].strip(_EDGE), how, pack)
    out = _PLACEHOLDER.sub(sub, template)
    return re.sub(r"\s+", " ", out).strip()


# ---------------------------------------------------------------------- #
def to_english(text: str, languages: Iterable[str]) -> Optional[str]:
    """The English command for ``text``, or None when no pattern of the given
    languages (tried in order) fits it."""
    found = to_english_in(text, languages)
    return found[0] if found else None


def to_english_in(text: str, languages: Iterable[str]) -> Optional[tuple]:
    """(the English command, the language whose pattern matched), or None."""
    for code in languages:
        pack = get_pack(code)
        if pack is None:
            continue
        original = clean(text, pack)
        if not original:
            continue
        folded = fold(original)
        for cmd in pack.commands:
            m = cmd.regex.match(folded)
            if m:
                english = render(cmd.to, m, original, pack)
                return (english, code) if english else None
        via_verb = _leading_verb(original, folded, pack)
        if via_verb:
            return via_verb, code
    return None


def _leading_verb(original: str, folded: str, pack: Pack) -> Optional[str]:
    """"pon some jazz" -> "play some jazz": just the first word is a command word
    of this language and the rest is left as said."""
    parts = folded.split(None, 1)
    if not parts:
        return None
    verb = pack.verbs.get(parts[0].strip(_EDGE))
    if verb is None:
        return None
    rest = original.split(None, 1)[1].strip() if len(parts) > 1 else ""
    return f"{verb} {rest}".strip()

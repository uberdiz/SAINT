"""
modules/lang/reply.py

SAINT's English reply, in the language the user spoke.

The router builds its answers in English from real results ("Playing Tití Me
Preguntó by Bad Bunny."). To answer in Spanish without asking a model for every
"Paused.", replies go through the language's phrasebook first — sentence by
sentence, patterns with placeholders, so titles and names pass through
untouched. What the phrasebook doesn't know goes to the local language model
("translate this, keep names as they are") when ``language.llm_translate`` is
on; if that isn't available either, the English is kept rather than guessed at.

Replies the language model wrote itself are already in the right language (it
is told to answer in the user's language), so they never come through here.
"""

import functools
import logging
import re
from typing import Optional

from core.config import config
from modules.lang.pack import Pack, get_pack, language_name

log = logging.getLogger("saint.lang")

_SENTENCES = re.compile(r"(?<=[.!?])\s+(?=[A-Z“\"'¿¡(])")
_PLACEHOLDER = re.compile(r"\{(\w+)(?::(\w+))?\}")
_EN_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December"]


def _when(text: str, pack: Pack) -> str:
    """"today at 5:00 PM", "every weekday at 8:30 AM", "Friday Oct 03 at 9:00 AM" in this language."""
    out = text
    for rx, rep in pack.when_rules:
        out = rx.sub(rep, out)
    for i, name in enumerate(_EN_DAYS):
        if i < len(pack.weekdays):
            out = re.sub(rf"\b{name}\b", pack.weekdays[i], out)
    for i, name in enumerate(_EN_MONTHS):
        if i < len(pack.months):
            out = re.sub(rf"\b{name[:3]}\b", pack.months[i][:3].lower() if len(pack.months[i]) > 3 else pack.months[i], out)
    return out


def _phrase(sentence: str, pack: Pack) -> Optional[str]:
    text = sentence.strip()
    for entry in pack.phrasebook:
        m = entry.regex.match(text)
        if not m:
            continue

        def sub(ph):
            name, how = ph.group(1), ph.group(2)
            try:
                value = m.group(int(name) if name.isdigit() else name) or ""
            except IndexError:
                return ""
            if how == "when":
                return _when(value, pack)
            if how == "noun":             # "food" -> "comida"
                return pack.key_names.get(value.lower(), value)
            if how == "weekday":
                i = _EN_DAYS.index(value.title()) if value.title() in _EN_DAYS else -1
                return pack.weekdays[i].lower() if 0 <= i < len(pack.weekdays) else value
            if how == "month":
                i = _EN_MONTHS.index(value.title()) if value.title() in _EN_MONTHS else -1
                return pack.months[i] if 0 <= i < len(pack.months) else value
            return value
        return _PLACEHOLDER.sub(sub, entry.to)
    return None


@functools.lru_cache(maxsize=256)
def _llm_cached(text: str, language: str) -> Optional[str]:
    return _llm(text, language)


def _llm(text: str, language: str) -> Optional[str]:
    try:
        from core.module_manager import module_manager
        from modules.ai.providers import get_provider
        name = config.get("ai.provider", "ollama")
        base_url = config.get("ai.base_url", "http://localhost:11434")
        ai = module_manager.get("ai")
        model, _ = ai._resolve_model(name, base_url, config.get("ai.model", ""))
        system = (f"Translate the user's message into {language}. Keep names, song and artist titles, numbers "
                  f"and times exactly as they are. Reply with the translation only, in one line.")
        out = get_provider(name).send(messages=[{"role": "system", "content": system},
                                                {"role": "user", "content": text}],
                                      model=model, api_key=config.get("ai.api_key", ""), base_url=base_url,
                                      temperature=0.2, timeout=min(20.0, float(config.get("ai.timeout_seconds", 30))))
        out = (out or "").strip().strip('"“”')
        return out or None
    except Exception as e:
        log.info("lang.llm_translate_unavailable %s", e)
        return None


def localize(reply: str, code: str, allow_llm: bool = True) -> str:
    """``reply`` (English) in language ``code``; unchanged when that's English or unknown."""
    if not reply or code in ("", "en", "und"):
        return reply
    pack = get_pack(code)
    whole = _phrase(reply.strip(), pack) if pack is not None else None
    if whole is not None:                         # "Skipped. Now playing X." is one phrase
        return whole
    parts = _SENTENCES.split(reply.strip())
    translated, missing = [], False
    for part in parts:
        t = _phrase(part, pack) if pack is not None else None
        if t is None:
            missing = True
            translated.append(part)
        else:
            translated.append(t)
    if not missing:
        return " ".join(translated)
    if allow_llm and config.get("language.llm_translate", True) and re.search(r"[A-Za-z]{3,}", reply):
        out = _llm_cached(reply.strip(), language_name(code))
        if out:
            return out
    return " ".join(translated)

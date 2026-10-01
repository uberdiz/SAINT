"""
modules/lang

SAINT in more than one language — and in two at once.

    turn = lang.analyze("recuérdame to call mom at 5")
    turn.language            "es"      the language to answer in
    turn.mixed, turn.secondary   True, "en"
    turn.english             "remind me to call mom at 5"   (what the router gets)
    turn.directive()         a sentence for the language model's prompt
    turn.localize("Okay.")   "Vale."

Speech recognition, the router and the reminder parser stay exactly as they
were; this layer sits in front of them (translating command *shapes* into the
English they understand) and behind them (answering in the user's language).
See detect.py, normalize.py, reply.py and the packs in lexicon/.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from core.config import config
from modules.lang import detect as _detect
from modules.lang import normalize, reply
from modules.lang.pack import available, get_pack, language_name

__all__ = ["analyze", "Turn", "state", "available", "language_name"]


class LanguageState:
    """The language of the conversation so far, so "sí", "vale" or "ok" don't flip it."""

    def __init__(self):
        self._lock = threading.Lock()
        self.sticky = ""
        self.sticky_at = 0.0
        self.reply_language = "en"      # what TTS should speak right now
        self.reply_secondary = ""       # the second language of a mixed reply (for segmenting speech)

    def current(self) -> str:
        minutes = float(config.get("language.sticky_minutes", 10) or 10)
        with self._lock:
            return self.sticky if self.sticky and time.time() - self.sticky_at <= minutes * 60 else ""

    def note(self, language: str):
        with self._lock:
            self.sticky, self.sticky_at = language, time.time()

    def set_reply(self, language: str, secondary: str = ""):
        with self._lock:
            self.reply_language, self.reply_secondary = language or "en", secondary or ""

    def reset(self):
        with self._lock:
            self.sticky, self.sticky_at, self.reply_language, self.reply_secondary = "", 0.0, "en", ""


state = LanguageState()


@dataclass
class Turn:
    text: str
    language: str = "en"                 # the language to answer in
    secondary: str = ""                  # the other language of a mixed sentence
    mixed: bool = False
    english: Optional[str] = None        # the English command, when the text needed translating
    detection: Optional[_detect.Detection] = None

    @property
    def translated(self) -> bool:
        return bool(self.english) and self.english != self.text

    @property
    def routed_text(self) -> str:
        """What the router should see."""
        return self.english if self.translated else self.text

    def directive(self) -> str:
        """A line for the language model's system prompt ("" for plain English)."""
        if self.language in ("", "en", "und") and not self.mixed:
            return ""
        main = language_name(self.language if self.language not in ("", "und") else "en")
        if self.mixed and self.secondary and config.get("language.mixed_mode", "mirror") == "mirror":
            other = language_name(self.secondary)
            return (f"The user mixes {main} and {other} in the same sentence. Answer in the same natural mix they "
                    f"used, mostly {main}; keep words they said in {other} (titles, app names, phrases) as they "
                    f"said them. Keep it to one short sentence.")
        return (f"The user is speaking {main}. Answer in {main} only, natural and conversational, in one short "
                f"sentence. Don't translate names, song titles or app names.")

    def localize(self, english_reply: str, allow_llm: bool = True) -> str:
        if self.language in ("", "en", "und"):
            return english_reply
        return reply.localize(english_reply, self.language, allow_llm)


def analyze(text: str, hint: str = "", update_state: bool = True) -> Turn:
    """Work out the language(s) of ``text`` and, if it isn't English, the English
    command it means. ``hint`` is the speech recogniser's language guess."""
    if not config.get("language.auto_detect", True):
        return Turn(text=text)
    prefer = list(config.get("language.preferred", []) or [])
    sticky = state.current()
    det = _detect.detect(normalize.strip_wake(text), hint=hint, prefer=prefer, sticky=sticky)
    language = det.primary if det.primary != "und" else sticky
    turn = Turn(text=text, language=language or "en", secondary=det.secondary, mixed=det.mixed, detection=det)

    # Which languages' command patterns to try, most likely first.
    try_langs: List[str] = []
    if language and language != "en":
        try_langs.append(language)
    if det.secondary and det.secondary != "en":
        try_langs.append(det.secondary)
    if not det.counts:                      # only words several languages share ("pausa", "para"): try each
        try_langs += [c for c in det.candidates if c != "en"]
    try_langs = list(dict.fromkeys(try_langs))
    if try_langs:
        found = normalize.to_english_in(text, try_langs)
        if found:
            turn.english = found[0]
            if found[1] != turn.language and (turn.language in ("en", "und") or not det.counts):
                turn.language = found[1]
    if not config.get("language.reply_in_user_language", True):
        turn.language = "en"
    if update_state and (det.counts or turn.translated) and turn.language not in ("", "und"):
        state.note(turn.language)
    if update_state:
        state.set_reply(turn.language, turn.secondary if turn.mixed else "")
    return turn

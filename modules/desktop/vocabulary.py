"""
modules/desktop/vocabulary.py

Which name did the user mean? Speech-to-text writes names it doesn't know as
ordinary words that sound alike — "Claude" comes out as "clad", "cloud" or
"clawed", "Rocket League" as "rocket leak", "Bloxstrap" as "block strap".
SAINT already knows every name it can act on (the apps and games installed on
this PC — Start menu, Store apps, desktop/taskbar shortcuts — plus the user's
aliases), so rather than teaching Whisper each word, a name that doesn't match
exactly is compared by *sound* with the names SAINT knows:

    phonetic key   claude -> KLT   clad -> KLT   cloud -> KLT   icloud -> AKLT
    score          sound (key similarity) + spelling + how often the user opens it

One clear winner is used, and remembered for next time ("clad" -> Claude); two
close ones are asked about ("Did you mean Claude or iCloud?") and the answer is
remembered too. Deterministic and local — no model call, nothing added to the
STT prompt. Stored in data/vocabulary.json.
"""

import difflib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from core.paths import data_path

log = logging.getLogger("saint.desktop")

ACCEPT = 0.80          # a single name this close is what was meant
ASK = 0.70             # names this close are offered as "did you mean ...?"
MARGIN = 0.06          # the winner must beat the runner-up by this much

# Sound-alike spellings -> one symbol. Order matters (digraphs before letters).
_RULES = [
    (r"^kn", "n"), (r"^wr", "r"), (r"^ps", "s"), (r"^x", "s"), (r"ph", "f"), (r"tch", "X"), (r"sch", "sk"),
    (r"dge", "j"), (r"ck", "k"), (r"qu", "kw"), (r"q", "k"), (r"x", "ks"), (r"c(?=[eiy])", "s"), (r"c", "k"),
    (r"g(?=[eiy])", "j"), (r"gh(?![aeiou])", ""), (r"sh", "X"), (r"ch", "X"), (r"th", "0"), (r"wh", "w"),
    # Voiced / unvoiced pairs Whisper mixes up in names it doesn't know.
    (r"z", "s"), (r"d", "t"), (r"g", "k"), (r"v", "f"), (r"b", "p"),
]
_RULES = [(re.compile(p), r) for p, r in _RULES]


def _word_key(word: str) -> str:
    w = word.lower()
    for rx, rep in _RULES:
        w = rx.sub(rep, w)
    if not w:
        return ""
    head, tail = w[0], w[1:]
    head = "A" if head in "aeiou" else head
    tail = re.sub(r"[aeiouyhw]", "", tail)
    return (head + tail).upper()


def phonetic(text: str) -> str:
    """'Rocket League' -> 'RKTLK' (the same key as 'rocket leak')."""
    key = "".join(_word_key(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower()))
    return re.sub(r"(.)\1+", r"\1", key)


def _letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


@dataclass
class Match:
    name: str
    score: float


class Vocabulary:
    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.Lock()
        self._data: Optional[Dict] = None

    # ---- persistence ------------------------------------------------------ #
    def _file(self) -> str:
        return self._path or str(data_path("vocabulary.json"))

    def _load(self) -> Dict:
        if self._data is None:
            try:
                with open(self._file(), encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                d = {}
            self._data = {"heard": dict(d.get("heard") or {}), "used": dict(d.get("used") or {})}
        return self._data

    def _save(self):
        tmp = self._file() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=1)
            os.replace(tmp, self._file())
        except OSError:
            log.warning("vocabulary.save_failed", exc_info=True)

    # ---- learning ----------------------------------------------------------- #
    def note_used(self, name: str):
        """The user opened / used ``name``: a little more likely next time."""
        key = _letters(name)
        if not key:
            return
        with self._lock:
            d = self._load()
            d["used"][key] = int(d["used"].get(key, 0)) + 1
            self._save()

    def learn(self, heard: str, meant: str):
        """'clad' meant Claude: use it straight away next time."""
        h, m = _letters(heard), (meant or "").strip()
        if not h or not m or h == _letters(m):
            return
        with self._lock:
            d = self._load()
            if d["heard"].get(h) != m:
                d["heard"][h] = m
                self._save()
                log.info("vocabulary.learned %r -> %r", heard, m)

    def learned(self, heard: str) -> str:
        with self._lock:
            return self._load()["heard"].get(_letters(heard), "")

    # ---- matching ----------------------------------------------------------- #
    def rank(self, spoken: str, names: Iterable[str], limit: int = 3) -> List[Match]:
        """Known names that sound like ``spoken``, best first (only those worth offering)."""
        s_key, s_let = phonetic(spoken), _letters(spoken)
        if len(s_let) < 3 or len(s_key) < 2:
            return []
        with self._lock:
            used = dict(self._load()["used"])
        n_words = len(re.findall(r"[a-z0-9]+", (spoken or "").lower()))
        best: Dict[str, Match] = {}
        for name in names:
            n_let = _letters(name)
            if not n_let:
                continue
            words = re.findall(r"[a-z0-9]+", name.lower())
            # The whole name, and its first words ("opra" ~ "Opera" of "Opera Browser"), a little less.
            forms = [(name, 1.0)] + ([(" ".join(words[:n_words]), 0.97)] if len(words) > n_words else [])
            for form, weight in forms:
                f_key, f_let = phonetic(form), _letters(form)
                if not f_key:
                    continue
                sound = difflib.SequenceMatcher(None, s_key, f_key).ratio()
                if sound < 0.6:
                    continue
                spelling = difflib.SequenceMatcher(None, s_let, f_let).ratio()
                score = weight * (0.65 * sound + 0.35 * spelling) + min(0.06, 0.01 * int(used.get(n_let, 0)))
                if score >= ASK and (n_let not in best or score > best[n_let].score):
                    best[n_let] = Match(name, round(score, 3))
        return sorted(best.values(), key=lambda m: -m.score)[:limit]

    def resolve(self, spoken: str, names: Iterable[str]) -> Optional[str]:
        """The one name ``spoken`` clearly meant, or None (no match, or more than one)."""
        names = list(names)
        meant = self.learned(spoken)
        if meant and _letters(meant) in {_letters(n) for n in names}:
            return next(n for n in names if _letters(n) == _letters(meant))
        ranked = self.rank(spoken, names)
        if not ranked or ranked[0].score < ACCEPT or self._tied(ranked):
            return None
        log.info("vocabulary.heard %r as %r (%.2f)", spoken, ranked[0].name, ranked[0].score)
        return ranked[0].name

    def ambiguous(self, spoken: str, names: Iterable[str]) -> bool:
        """Two known names fit about equally well ("cloud": Claude or iCloud) — ask, don't guess."""
        if self.learned(spoken):
            return False
        ranked = self.rank(spoken, names)
        return bool(ranked) and ranked[0].score >= ACCEPT and self._tied(ranked)

    @staticmethod
    def _tied(ranked: List[Match]) -> bool:
        return len(ranked) > 1 and ranked[0].score - ranked[1].score < MARGIN


vocabulary = Vocabulary()

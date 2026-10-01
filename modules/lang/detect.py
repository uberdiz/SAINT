"""
modules/lang/detect.py

Which language is this, and is it one language or two?

Detection is per word, not per sentence, because people mix: "play la canción
de Bad Bunny", "recuérdame to call mom at 5", "pon some jazz". Every word is looked
up in each language's word list (modules/lang/lexicon/*.json):

    in exactly one list    a vote for that language
    in several lists       ambiguous ("no", "la", "a"): given to whichever
                           candidate the clearer words in the sentence point at
    in none                a name, a title, a number: no vote ("Bad Bunny")

Letters only one language uses (ñ ¿ ¡ for Spanish, ã õ for Portuguese, ß for
German) count extra, the speech recogniser's own guess is one more vote, and
text in another script (Japanese, Russian, Arabic, ...) is recognised by script.
A sentence is *mixed* when a second language has a real share of the votes.

No model, no network, a few microseconds.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Tuple

from modules.lang.pack import SCRIPT_LANGS, SUPPORTED, english_words, fold, get_pack

_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.U)

_SCRIPTS = (
    ("ja", (0x3040, 0x30FF)), ("ko", (0xAC00, 0xD7AF)), ("ko", (0x1100, 0x11FF)), ("zh", (0x4E00, 0x9FFF)),
    ("ru", (0x0400, 0x04FF)), ("ar", (0x0600, 0x06FF)), ("hi", (0x0900, 0x097F)), ("th", (0x0E00, 0x0E7F)),
    ("el", (0x0370, 0x03FF)), ("he", (0x0590, 0x05FF)),
)
# Said in every language, so they say nothing about which one.
NEUTRAL = frozenset({"ok", "okay", "hmm", "hm", "uh", "um", "eh", "ah", "oh", "hey", "hi", "yeah", "wow"})
MIXED_SHARE = 0.25          # a second language needs this share of the votes to make a sentence "mixed"


@dataclass
class Detection:
    primary: str = "und"                  # "en", "es", ... or "und" when nothing says
    secondary: str = ""
    mixed: bool = False
    confidence: float = 0.0
    counts: Dict[str, float] = field(default_factory=dict)
    tags: List[Tuple[str, str]] = field(default_factory=list)       # (word, language or "")
    candidates: List[str] = field(default_factory=list)             # every language a word might belong to, likeliest first

    def languages(self) -> List[str]:
        return [l for l in (self.primary, self.secondary) if l and l != "und"]


def _script_counts(text: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for ch in text:
        cp = ord(ch)
        for lang, (lo, hi) in _SCRIPTS:
            if lo <= cp <= hi:
                out[lang] = out.get(lang, 0) + 1
                break
    return out


def detect(text: str, hint: str = "", prefer: Sequence[str] = (), sticky: str = "") -> Detection:
    """``hint``: the speech recogniser's guess. ``prefer``: the user's languages,
    most likely first. ``sticky``: the language of the conversation so far."""
    text = text or ""
    det = Detection()
    words = _WORD.findall(text)
    letters = sum(len(w) for w in words)

    # ---- other scripts --------------------------------------------------
    scripts = _script_counts(text)
    if scripts and letters:
        if "ja" in scripts:
            scripts.pop("zh", None)              # kana present: the Han characters are Japanese
        lang, n = max(scripts.items(), key=lambda kv: kv[1])
        if n / max(1, letters) >= 0.3:
            det.primary, det.confidence = lang, 0.95
            det.counts = {lang: float(n)}
            latin = [w for w in words if all(ord(c) < 0x250 for c in w)]
            eng = [w for w in latin if fold(w) in english_words()]
            if eng and len(eng) / max(1, len(words)) >= MIXED_SHARE:
                det.secondary, det.mixed = "en", True
            det.tags = [(w, lang if any(ord(c) >= 0x250 for c in w) else "") for w in words]
            return det

    # ---- Latin-script languages ------------------------------------------
    packs = {"en": english_words()}
    for code in SUPPORTED:
        pack = get_pack(code)
        if pack is not None:
            packs[code] = pack.words
    counts: Dict[str, float] = {code: 0.0 for code in packs}
    soft: Dict[str, float] = {code: 0.0 for code in packs}         # a word several languages share: a fraction of a vote each
    tagged: List[Tuple[str, List[str]]] = []
    for i, w in enumerate(words):
        if i > 0 and w[0].isupper() and not w.isupper():
            tagged.append((w, []))                                # a capital mid-sentence: a name or a title ("Bad Bunny")
            continue
        f = fold(w.replace("’", "'"))
        if f in NEUTRAL:
            tagged.append((w, []))
            continue
        cands = [code for code, vocab in packs.items() if f in vocab]
        if not cands and "'" in f:                     # "l'heure", "don't": try the part after the apostrophe
            tail = f.split("'", 1)[1]
            cands = [code for code, vocab in packs.items() if tail in vocab or f in vocab]
        tagged.append((w, cands))
        if len(cands) == 1:
            counts[cands[0]] += 1.0
        for c in cands:
            soft[c] += 1.0 / len(cands)

    lowered = text.lower()
    for code in SUPPORTED:
        pack = get_pack(code)
        if pack is not None and pack.chars:
            counts[code] += min(3.0, 1.5 * sum(lowered.count(c) for c in pack.chars))
    if hint in counts:
        counts[hint] += 1.0

    order = [c for c in (hint, sticky, *prefer, "en") if c in counts]

    def pick(cands: Iterable[str]) -> str:
        return max(cands, key=lambda c: (counts[c], -order.index(c) if c in order else -99))

    tags: List[Tuple[str, str]] = []
    for w, cands in tagged:
        if len(cands) == 1:
            tags.append((w, cands[0]))
        elif len(cands) > 1:
            choice = pick(cands)
            counts[choice] += 0.5 if counts[choice] > 0 else 0.0
            tags.append((w, choice))
        else:
            tags.append((w, ""))
    det.tags = tags

    det.candidates = [c for c, v in sorted(soft.items(), key=lambda kv: (-kv[1], order.index(kv[0]) if kv[0] in order else 99))
                      if v > 0]
    total = sum(counts.values())
    det.counts = {k: v for k, v in counts.items() if v > 0}
    if total <= 0:
        det.primary = sticky or "und"
        return det
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], order.index(kv[0]) if kv[0] in order else 99))
    det.primary = ranked[0][0]
    det.confidence = ranked[0][1] / total
    second, n2 = ranked[1]
    if n2 >= 1.0 and n2 / total >= MIXED_SHARE:
        det.secondary, det.mixed = second, True
    return det

"""
modules/voice/stt_filters.py

Clean-up for Whisper transcripts before they reach the agent.

Whisper sometimes loops on a phrase when it hears noise or its own echo
("No, no, no, no, ..." x100 was logged on 2026-09-24). A transcript that is
mostly one repeated n-gram is dropped; a short run of repeats inside an
otherwise normal sentence is collapsed ("skip skip skip skip" -> "skip").
"""

import re
from collections import Counter
from typing import List, Optional

_WORD = re.compile(r"[\w']+")


def _tokens(text: str) -> List[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


def repetition_ratio(text: str, max_n: int = 4) -> float:
    """Share of tokens covered by the most repeated n-gram (n = 1..max_n)."""
    toks = _tokens(text)
    if len(toks) < 2:
        return 0.0
    best = 0.0
    for n in range(1, max_n + 1):
        if len(toks) < 2 * n:
            break
        grams = Counter(tuple(toks[i:i + n]) for i in range(0, len(toks) - n + 1))
        gram, count = grams.most_common(1)[0]
        if count < 2:
            continue
        # Non-overlapping coverage of that n-gram.
        covered, i = 0, 0
        while i <= len(toks) - n:
            if tuple(toks[i:i + n]) == gram:
                covered += n
                i += n
            else:
                i += 1
        best = max(best, covered / len(toks))
    return best


def collapse_repeats(text: str, keep: int = 1, min_run: int = 4) -> str:
    """Collapse runs of the same word repeated ``min_run``+ times."""
    words = (text or "").split()
    out, i = [], 0
    while i < len(words):
        j = i
        base = re.sub(r"[^\w']", "", words[i]).lower()
        while j + 1 < len(words) and re.sub(r"[^\w']", "", words[j + 1]).lower() == base and base:
            j += 1
        run = j - i + 1
        out.extend(words[i:i + (keep if run >= min_run else run)])
        i = j + 1
    return " ".join(out)


_WAKE_MID = re.compile(r"(?:^|[\s,.!?])(?:hey|hi|okay|ok|yo)[,\s]+saint\b[,.!?]*\s*", re.I)


def repeated_unit(text: str) -> Optional[str]:
    """'turn off the mini player turn off the mini player' -> 'turn off the
    mini player' (a request said twice); None if it isn't one phrase repeated."""
    words = (text or "").split()
    toks = [re.sub(r"[^\w']", "", w).lower() for w in words]
    n = len(toks)
    for k in range(min(n, 6), 1, -1):            # most copies first = the smallest unit
        if n % k:
            continue
        size = n // k
        if all(toks[i * size:(i + 1) * size] == toks[:size] for i in range(k)):
            return " ".join(words[:size]).rstrip(",")
    return None


def clean_transcript(text: str) -> Optional[str]:
    """Return the transcript to use, or None if it is a repetition loop."""
    # The user repeated themselves with the wake word in the middle ("Turn off
    # the mini-player Hey SAINT, turn off the mini-player"): what follows the
    # last wake word is the request.
    parts = [m for m in _WAKE_MID.finditer(text or "") if m.start() > 0]
    if parts and len(_tokens(text[parts[-1].end():])) >= 2:
        # Keep the wake word itself: passive listening only acts on "Hey SAINT, ...".
        text = text[parts[-1].start():].lstrip(" ,.!?")
    unit = repeated_unit(text)
    toks = _tokens(text)
    if len(toks) > 8 and repetition_ratio(text) >= 0.6:
        # A whole sentence said twice is kept once; a word looping is noise.
        return unit if unit and len(_tokens(unit)) >= 3 else None
    if unit:
        return unit
    return collapse_repeats(text) if len(toks) > 3 else text

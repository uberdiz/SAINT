"""
modules/lang/segments.py

Split a sentence into stretches of one language so each can be spoken with that
language's voice: "Reproduciendo Bad Bunny, and I'll queue up more" is Spanish,
then English. Used by text-to-speech; a sentence in one language comes back as
a single segment.
"""

import re
from typing import List, Tuple

from modules.lang.detect import _WORD, detect


def segments(text: str, hint: str = "") -> List[Tuple[str, str]]:
    """[(language, text), ...] covering all of ``text`` in order."""
    if not text or not text.strip():
        return []
    det = detect(text, sticky=hint)
    main = det.primary if det.primary != "und" else (hint or "en")
    if not det.mixed:
        return [(main, text)]
    spans = [(m.start(), m.end()) for m in _WORD.finditer(text)]
    if len(spans) != len(det.tags):
        return [(main, text)]
    langs: List[str] = []
    cur = main
    for _, tag in det.tags:
        cur = tag or cur
        langs.append(cur)
    # A single stray word isn't worth a voice change: fold one-word runs into the language around them.
    i = 0
    while i < len(langs):
        j = i
        while j < len(langs) and langs[j] == langs[i]:
            j += 1
        if j - i == 1 and 0 < i and j < len(langs) and langs[i - 1] == langs[j]:
            langs[i] = langs[i - 1]
        i = j
    out: List[Tuple[str, str]] = []
    start = 0
    for k in range(1, len(langs) + 1):
        if k == len(langs) or langs[k] != langs[k - 1]:
            end = len(text) if k == len(langs) else spans[k][0]
            piece = text[start:end]
            if piece.strip():
                out.append((langs[k - 1], piece))
            start = end
    return out or [(main, text)]

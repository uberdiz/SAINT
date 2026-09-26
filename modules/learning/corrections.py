"""
modules/learning/corrections.py

Learning from "no, I meant ...".

SAINT remembers the last request it handled. When the next thing the user
says corrects it —

    "play my kpop playlist"   (SAINT played a public playlist)
    "no, I meant play my 'yuh' playlist on Spotify"

— the corrected command runs, and if it works, the first request is taught:
next time "play my kpop playlist" plays "yuh" straight away.
"""

import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

WINDOW_SEC = 150          # a correction has to follow the request it corrects

_MEANT = re.compile(
    r"\bi\s+(?:meant|mean|said|wanted|asked(?:\s+for)?|was\s+asking)\s+(?:for\s+)?(?:you\s+)?(?:to\s+)?"
    r"(?P<cmd>.+)$", re.I)
_WRONG = re.compile(
    r"^(?:no+|nope|nah|wrong(?:\s+one)?|not\s+(?:that|this)(?:\s+one)?|that'?s\s+(?:wrong|not\s+(?:it|right|"
    r"what\s+i\s+(?:meant|asked(?:\s+for)?|wanted))))(?:[,.!:;]+\s*|\s+)(?:no+[,.!]*\s*)*"
    r"(?:(?:please|just|instead)\s+)?(?P<cmd>.+)$", re.I)
# What a correction's command may start with (so "no, it's fine" isn't one).
_VERB = re.compile(
    r"^(?:open|close|quit|launch|start|run|play|pause|resume|skip|click|double|right|press|type|minimi[sz]e|"
    r"maximi[sz]e|show|hide|move|put|set|turn|switch|mute|unmute|go|take|search|clean|clear|extract|lock|"
    r"save|restore|scroll|snap|focus|use)\b", re.I)


@dataclass
class Turn:
    text: str
    intent: str
    ok: Optional[bool]
    at: float


class CorrectionTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._last: Optional[Turn] = None

    def note(self, text: str, intent: str, ok: Optional[bool]):
        with self._lock:
            self._last = Turn(text, intent, ok, time.time())

    @property
    def last(self) -> Optional[Turn]:
        with self._lock:
            t = self._last
        if t and time.time() - t.at > WINDOW_SEC:
            return None
        return t

    def clear(self):
        with self._lock:
            self._last = None

    def detect(self, text: str) -> Optional[str]:
        """The command the user actually wanted, if ``text`` corrects the last
        request; otherwise None."""
        prev = self.last
        if prev is None or prev.intent.startswith(("meta.", "confirmation", "choice", "learning.")):
            return None
        t = (text or "").strip().strip("\"'“”")
        cmd = None
        for m in (_WRONG.match(t), _MEANT.search(t)):
            if m:
                c = re.sub(r"^[\s,.;:!-]*(?:(?:for\s+)?you\s+to\s+|to\s+)?", "", m.group("cmd"), flags=re.I)
                if _VERB.match(c):
                    cmd = c
                    break
        if not cmd:
            return None
        cmd = re.sub(r"[“”\"]", "", cmd).strip(" .!?,")
        cmd = re.sub(r"(?<!\w)'([^']+)'(?!\w)", r"\1", cmd)          # 'yuh' -> yuh
        if len(cmd) < 3 or cmd.lower() == prev.text.lower().strip(" .!?"):
            return None
        return cmd


corrections = CorrectionTracker()

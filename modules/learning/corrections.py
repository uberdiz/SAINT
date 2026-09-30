"""
modules/learning/corrections.py

Learning from "no, I meant ...".

SAINT remembers the last request it handled. When the next thing the user
says corrects it —

    "play my gym playlist"   (SAINT played a public playlist)
    "no, I meant play my 'moe' playlist on Spotify"
    "I meant my moe playlist"               (just the thing, not the action)
    "I said Claude btw"                     (after "switch to Glod")

— the corrected command runs, and if it works, the first request is taught:
next time "play my gym playlist" plays "moe" straight away.
"""

import difflib
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

WINDOW_SEC = 150          # a correction has to follow the request it corrects

_MEANT = re.compile(
    r"\bi\s+(?:meant|mean|said|wanted|asked(?:\s+for)?|was\s+asking|was\s+talking\s+about)\s+(?:for\s+)?"
    r"(?:you\s+)?(?:to\s+)?(?P<cmd>.+)$", re.I)
_WRONG = re.compile(
    r"^(?:no+|nope|nah|wrong(?:\s+one)?|not\s+(?:that|this)(?:\s+one)?|that'?s\s+(?:wrong|not\s+(?:it|right|"
    r"what\s+i\s+(?:meant|asked(?:\s+for)?|wanted))))(?:[,.!:;]+\s*|\s+)(?:no+[,.!]*\s*)*"
    r"(?:(?:please|just|instead)\s+)?(?P<cmd>.+)$", re.I)
# What a correction's command may start with (so "no, it's fine" isn't one).
_VERB = re.compile(
    r"^(?:open|close|quit|launch|start|run|play|pause|resume|skip|click|double|right|press|type|minimi[sz]e|"
    r"maximi[sz]e|show|hide|move|put|set|turn|switch|mute|unmute|go|take|search|clean|clear|extract|lock|"
    r"save|restore|scroll|snap|focus|use|delete|rename|make|create)\b", re.I)
# Not corrections, even after "no": "no, it's fine", "no no no", "no thanks", "no, bro",
# "no, don't do that. Forget that." (both were fitted into a song request on 2026-09-29).
_NOT_A_THING = re.compile(r"^(?:it'?s|that'?s|i'?m|you'?re|we'?re|thanks|thank you|worries|problem|way|never ?mind|"
                          r"no+|nah|nope|stop|wait|okay|ok|sorry|good|fine|cancel|bro|bruh|dude|man|dawg|dog|"
                          r"brother|girl|sis|fam|lol|lmao|wtf|what|why|how|huh|ugh|oh|damn|dang|come on|"
                          r"seriously|not even|don'?t|do not|forget|undo|leave|never|you|that|this|wrong|"
                          r"please|just)\b", re.I)


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
        request; otherwise None.

        "no, close the finals"            -> "close the finals"
        "I meant my moe playlist"         -> the last request with that playlist
        "I said Claude btw"               -> the last request with the misheard word fixed
        "no, the folder you just made"    -> "open the folder you just made"
        """
        prev = self.last
        if prev is None or prev.intent.startswith(("meta.", "confirmation", "choice", "learning.watch",
                                                   "learning.done", "learning.list", "learning.forget")):
            return None
        from modules.agent.router import spell_out
        t = spell_out((text or "").strip().strip("\"'“”"))
        cmd = None
        for m in (_MEANT.search(t), _WRONG.match(t)):
            if not m:
                continue
            c = re.sub(r"^[\s,.;:!-]*(?:(?:for\s+)?you\s+to\s+|to\s+)?", "", m.group("cmd"), flags=re.I)
            c = _unquote(c)
            after = re.match(r"^[^,;]{0,15}[,;]\s*(?P<rest>.+)$", c)   # "I asked for, switch to spotify"
            if after and _VERB.match(after.group("rest")):
                c = after.group("rest")
            if _VERB.match(c):
                cmd = c
                break
            if _NOT_A_THING.match(c):
                continue
            fixed = substitute(prev.text, c)
            if fixed:
                cmd = fixed
                break
        if not cmd:
            return None
        cmd = _unquote(cmd)
        if len(cmd) < 3 or cmd.lower() == prev.text.lower().strip(" .!?"):
            return None
        return cmd


def _unquote(s: str) -> str:
    s = re.sub(r"[“”\"]", "", s).strip(" .!?,")
    return re.sub(r"(?<!\w)'([^']+)'(?!\w)", r"\1", s)          # 'moe' -> moe


# Nouns a correction can swap: "I meant my moe *playlist*".
_NOUNS = ("playlist", "song", "track", "album", "artist", "folder", "file", "screenshot", "window", "app", "game",
          "tab", "video", "screen", "monitor", "drive", "download", "channel", "station", "page", "site")
_LEAD = re.compile(r"^(?P<v>(?:please\s+)?(?:(?:switch|go|change|move|put|send|turn|navigate|head)\s+(?:over\s+)?to|"
                   r"open(?:\s+up)?|play|close|launch|start|run|show(?:\s+me)?|find|search(?:\s+for)?|pull up|"
                   r"bring up|focus(?:\s+on)?|click(?:\s+on)?|double click(?:\s+on)?|delete|move|put|queue|shuffle))\b",
                   re.I)
_TAIL = re.compile(r"(?:\s*,?\s*(?:btw|by the way|instead|though|not that one|lol|please|for me|actually))+$", re.I)
_SMALL = {"the", "my", "a", "an", "to", "on", "in", "of", "for", "me", "your", "and", "it"}


def substitute(prev: str, said: str) -> Optional[str]:
    """Fit a correction that names only the right *thing* into the last request.

    "play my gym playlist" + "my moe playlist"   -> "play my moe playlist"
    "switch to glod"        + "Claude"            -> "switch to Claude"
    "open that folder for me" + "the folder you just extracted"
                                                  -> "open the folder you just extracted for me"
    "open bloxstrap"        + "Roblox"            -> "open Roblox"
    """
    from modules.desktop.window_match import sounds_like
    said = _TAIL.sub("", said.strip(" .!?,")).strip()
    prev = prev.strip(" .!?")
    if not said or not prev or len(said.split()) > 7:
        return None
    lead = _LEAD.match(prev)
    if not lead:
        return None                       # only requests to *do* something are corrected this way
    words = said.split()
    rest = prev[lead.end():]
    # 1. The same kind of thing: swap the noun phrase ("my gym playlist" -> "my moe playlist").
    head = words[-1].lower()
    if head in ("one", "ones") and len(words) > 1:          # "my moe one" = "my moe playlist"
        prev_noun = next((n for n in _NOUNS if re.search(rf"\b{n}\b", rest, re.I)), None)
        if prev_noun:
            said = " ".join(words[:-1] + [prev_noun])
            words, head = said.split(), prev_noun
    noun = next((n for n in _NOUNS if head in (n, n + "s") or
                 (len(head) > 4 and difflib.SequenceMatcher(None, n, head).ratio() >= 0.8)), None)
    if noun is None:
        noun = next((n for n in _NOUNS if re.search(rf"\b{n}s?\b", said, re.I)), None)
    if noun and re.search(rf"\b{noun}s?\b", rest, re.I):
        if head not in (noun, noun + "s") and len(head) > 4 and \
                difflib.SequenceMatcher(None, noun, head).ratio() >= 0.8:
            said = said[:len(said) - len(words[-1])] + noun          # "playlisy" -> "playlist"
        m = re.search(rf"\b(?:(?:my|the|a|an|your|that|this)\s+)?(?:[\w'.-]+\s+){{0,3}}?{noun}s?\b", rest, re.I)
        if m:
            return (prev[:lead.end()] + rest[:m.start()] + said + rest[m.end():]).strip()
    # 2. A misheard name: replace the word(s) that sound most like it ("glod" -> "Claude").
    if len(words) <= 3:
        tokens = list(re.finditer(r"[\w'.-]+", rest))
        best, best_score = None, 0.0
        for i in range(len(tokens)):
            for j in (i, i + 1):
                if j >= len(tokens):
                    continue
                span = rest[tokens[i].start():tokens[j].end()]
                if tokens[i].group(0).lower() in _SMALL or tokens[j].group(0).lower() in _SMALL:
                    continue
                sc = max(sounds_like(span, said), difflib.SequenceMatcher(None, span.lower(), said.lower()).ratio())
                if sc > best_score:
                    best, best_score = (tokens[i].start(), tokens[j].end()), sc
        if best and best_score >= 0.6:
            a, b = best
            return (prev[:lead.end()] + rest[:a] + said + rest[b:]).strip()
    # 3. Otherwise the same action on the new thing ("open bloxstrap" + "Roblox" -> "open Roblox").
    if len(words) <= 5:
        return f"{lead.group('v')} {said}".strip()
    return None


corrections = CorrectionTracker()

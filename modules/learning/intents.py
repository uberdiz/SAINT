"""
modules/learning/intents.py

Talking to SAINT about what it has learned:

    "what have you learned"               the last few skills
    "forget that" / "unlearn that"        the skill learned in the last 10 minutes
    "forget how to open disk cleanup"     a named skill
    "watch me" / "let me show you"        watch the user do the last failed request
    "let me show you how to open X"       ... or the one named
    "done" / "that's it" / "got it"       (while watching) stop and learn
"""

import re
import time
from typing import Optional, Tuple

from modules.learning.skills import norm, skills

_LIST = re.compile(r"^(?:what|which)\s+(?:(?:new\s+)?(?:things|skills|tricks)\s+)?(?:have|did)\s+you\s+(?:learn(?:ed|t)?)"
                   r"(?:\s+(?:recently|lately|so far|from me|today))?$|^(?:list|show)\s+(?:me\s+)?(?:your|the)\s+"
                   r"(?:learned|learnt)\s+(?:skills|things)$|^what (?:skills|tricks) do you (?:know|have)$")
_FORGET_LAST = re.compile(r"^(?:forget|unlearn|undo)\s+(?:that|it|what you (?:just )?learn(?:ed|t)|that skill|"
                          r"the last (?:thing|skill) you learn(?:ed|t))$|^(?:don'?t|do not) (?:do|learn) that(?: again)?$|"
                          r"^that'?s (?:wrong|not right),? forget (?:it|that)$")
_FORGET_NAMED = re.compile(r"^(?:forget|unlearn)\s+how\s+(?:to\s+)?(?P<what>.+)$|"
                           r"^forget\s+(?:the|your)\s+(?P<what2>.+?)\s+skill$")
_WATCH = re.compile(r"^(?:watch me|watch (?:and learn|how i do it|what i do)|let me show you|i'?ll show you|"
                    r"i will show you|let me teach you|i'?ll teach you)(?:\s+how(?:\s+to)?(?:\s+(?P<what>.+?))?)?"
                    r"(?:\s+(?:then|now|okay|ok))?$")
_DONE = re.compile(r"^(?:done|i'?m done|all done|that'?s it|that'?s how(?: you do it)?|got it\??|finished|"
                   r"there you go|like that|stop watching|ok(?:ay)? done|that'?s all)$")


def _t(text: str) -> str:
    return norm(text).strip(" ?!.")


def is_done(text: str) -> bool:
    return bool(_DONE.match(_t(text)))


def parse(text: str, last_failed: str = "") -> Optional[Tuple[str, str]]:
    """(kind, argument) for a learning command, else None."""
    t = _t(text)
    if not t:
        return None
    if _LIST.match(t):
        return "list", ""
    if _FORGET_LAST.match(t):
        if skills.last_learned is not None and time.time() - skills.last_learned_at < 600:
            return "forget_last", ""
        return None                         # "forget that" about a memory, not a skill
    m = _FORGET_NAMED.match(t)
    if m:
        what = (m.group("what") or m.group("what2") or "").strip()
        if skills.find(what) is not None:
            return "forget", what
        return None
    m = _WATCH.match(t)
    if m:
        return "watch", (m.group("what") or last_failed or "").strip()
    return None


def run(kind: str, arg: str) -> str:
    from modules.learning import demonstration
    if kind == "list":
        recent = skills.recent(5)
        if not recent:
            return "I haven't learned anything new yet. When I can't do something, show me once and I'll remember."
        parts = [f"“{s.said or s.phrase}” means {s.describe()}" for s in recent]
        return "Here's what I've learned: " + "; ".join(parts) + "."
    if kind == "forget_last":
        s = skills.last_learned
        skills.forget(s.id)
        return f"Forgotten. “{s.said or s.phrase}” is back to how it was."
    if kind == "forget":
        s = skills.find(arg)
        skills.forget(s.id)
        return f"Okay, I've forgotten how to {s.phrase}."
    if kind == "watch":
        if not arg:
            return "Tell me what you're about to show me, like “let me show you how to open disk cleanup”."
        if not demonstration.watch_for(arg):
            return "I can't watch the screen on this computer."
        return f"Okay, I'm watching. Show me how to {arg}, then say “done”."
    return ""

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
_FORGET_LAST = re.compile(r"^(?:forget|unlearn)\s+(?:that|it|what you (?:just )?learn(?:ed|t)|that skill|"
                          r"the last (?:thing|skill) you learn(?:ed|t))$|"
                          r"^(?:don'?t|do not)\s+(?:remember|save|learn|keep|do)\s+(?:that|it|this)(?:\s+again)?\b.*$|"
                          r"^that'?s (?:wrong|not right),? forget (?:it|that)$|^(?:that was|you learned (?:that|it)) wrong$")
# "Undo that" right after something was learned (not minutes later, when it means another thing).
_UNDO = re.compile(r"^undo (?:that|it)$")
_FORGET_NAMED = re.compile(r"^(?:forget|unlearn)\s+how\s+(?:to\s+)?(?P<what>.+)$|"
                           r"^forget\s+(?:the|your)\s+(?P<what2>.+?)\s+skill$")
_WATCH = re.compile(
    r"^(?:(?:can|could|will) you\s+|please\s+|(?:ok(?:ay)?|so|now)[,\s]+)?"
    r"(?:watch me(?: do (?:it|this|that))?|watch (?:and learn|how i do (?:it|this)|what i (?:do|wanted you to do|mean))|"
    r"let me show you|i'?ll show you|i will show you|let me teach you|i'?ll teach you|learn from me|"
    r"(?:i'?m going to|i'?m gonna|i'?ll|let me) (?:do|show you) (?:it|this|that|the action)(?: (?:myself|now))?"
    r"(?:[,.]?\s*(?:so )?(?:can you|could you|you can|and you|you should)? ?(?:watch|learn)(?: me)?(?: (?:do it|and learn|so you (?:can )?learn))?)?)"
    r"(?:\s+how(?:\s+to)?(?:\s+(?P<what>.+?))?)?"
    r"(?:\s+(?:then|now|okay|ok|so you (?:can )?learn|and learn))?\??$")
_DONE = re.compile(r"^(?:ok(?:ay)?[,\s]+|so[,\s]+|and[,\s]+)?(?:(?:i'?m|i am|we'?re|we are)\s+)?(?:all\s+)?"
                   r"(?:done|finished|set)(?:\s+now)?$|"
                   r"^(?:that'?s (?:it|how(?: you do it)?|all|how i do it)|got it\??|there you go|like that|i did it|"
                   r"stop watching|you can stop(?: watching)?|ok(?:ay)? done|done now)$")
# "Save that as game time" / "add that as an automation called game time"
_SAVE_AS = re.compile(r"^(?:save|add|keep|remember|store)\s+(?:that|this|it|what you (?:just )?did)\s+"
                      r"(?:as|to)\s+(?:an?\s+|my\s+)?(?:new\s+)?(?:automation|shortcut|command|skill|routine)?\s*"
                      r"(?:called|named|for|as)?\s*[\"“']?(?P<name>[^\"”']+?)[\"”']?$")
_MAKE_SKILL = re.compile(r"^(?:make|create|add|set up)\s+(?:an?\s+|a new\s+)?(?:automation|shortcut|command|skill|"
                         r"routine)\s+(?:called|named|for)\s+[\"“']?(?P<name>.+?)[\"”']?\s+(?:that|which|to)\s+"
                         r"(?:does|runs|will)?\s*(?P<steps>.+)$")


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
    if _FORGET_LAST.match(t) or _UNDO.match(t):
        window = 90 if _UNDO.match(t) else 600
        if skills.last_learned is not None and time.time() - skills.last_learned_at < window:
            return "forget_last", ""
        return None                         # "forget that" about a memory, not a skill
    m = _SAVE_AS.match(t)
    if m and m.group("name").strip() and m.group("name").strip() not in ("automation", "shortcut", "command"):
        return "save_as", m.group("name").strip()
    m = _MAKE_SKILL.match(t)
    if m:
        return "make", m.group("name").strip() + "\n" + m.group("steps").strip()
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
    if kind == "save_as":
        return _save_as(arg)
    if kind == "make":
        name, steps = arg.split("\n", 1)
        return _make(name, steps)
    if kind == "watch":
        if not arg:
            return "Tell me what you're about to show me, like “let me show you how to open disk cleanup”."
        if not demonstration.watch_for(arg):
            return "I can't watch the screen on this computer."
        return f"Okay, I'm watching. Show me how to {arg}, then say “done”."
    return ""


def _split_steps(text: str):
    parts = re.split(r"\s*(?:,\s*)?(?:\band then\b|\bthen\b|\band\b)\s+", text.strip(" .!?"))
    out, verb = [], ""
    for p in (p.strip(" ,.") for p in parts):
        if not p:
            continue
        first, _, rest = p.partition(" ")
        base = _base_verb(first)
        if base != first.lower() or base in _STEP_VERBS:
            verb = base
            p = f"{base} {rest}".strip()
        elif verb:                       # "open steam and discord" -> "open discord"
            p = f"{verb} {p}"
        out.append(p)
    return out


_STEP_VERBS = {"open", "close", "play", "pause", "resume", "skip", "switch", "launch", "start", "mute", "unmute",
               "turn", "set", "show", "minimize", "maximize", "move", "put", "press", "click", "go", "take", "search",
               "lock", "focus", "restore", "save"}


def _base_verb(word: str) -> str:
    """'opens' -> 'open', 'switches' -> 'switch', 'presses' -> 'press'."""
    w = word.lower()
    for cut in ("es", "s"):
        if w.endswith(cut) and w[:-len(cut)] in _STEP_VERBS:
            return w[:-len(cut)]
    return w


def _check(steps):
    from modules.learning.planner import understood
    bad = [s for s in steps if not understood(s)]
    return bad


def _save_as(name: str) -> str:
    """'Save that as game time': the last thing SAINT did becomes a named command."""
    from modules.learning.corrections import corrections
    last = corrections.last
    if last is None or not last.ok or last.intent.startswith(("learning.", "meta.", "confirmation", "llm")):
        return "Do the thing first, then say “save that as …” and I'll remember it under that name."
    if skills.learn(name, [last.text], "saved") is None:
        return f"I can't use “{name}” as a name — pick something more specific."
    return f"Saved. Saying “{name}” will now {last.text.strip(' .!?')[:1].lower() + last.text.strip(' .!?')[1:]}."


def _make(name: str, steps_text: str) -> str:
    steps = _split_steps(steps_text)
    if not steps:
        return "Tell me what it should do, like “make a shortcut called gaming time that opens Steam and Discord”."
    bad = _check(steps)
    if bad:
        return f"I don't know how to “{bad[0]}” yet, so I didn't save it. You can show me: say “let me show you how to {bad[0]}”."
    if skills.learn(name, steps, "saved") is None:
        return f"I can't use “{name}” as a name — pick something more specific."
    return f"Done — “{name}” will {', then '.join(steps)}. You can change it on the Automations page."

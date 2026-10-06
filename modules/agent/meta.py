"""
modules/agent/meta.py

Commands about SAINT itself that must work *while* SAINT is busy and never
go to the LLM:

    stop / cancel / abort / shut up / be quiet      → stop what's running now
    stop everything / cancel everything             → also background work
    what are you doing?                              → describe current work
    silent mode (for 30 minutes) / be quiet for 1 h  → act, but don't talk
    you can talk again / normal mode                 → end silent mode
    talk louder / your voice is too quiet            → SAINT's own voice volume (not Spotify / Windows)
    voice volume 60 / set your voice to 60%

``match_meta`` only matches the *whole* utterance (with fillers like "okay",
"saint", "please"), so "stop the music" and "I'm done gaming" are untouched.
"""

import re
from dataclasses import dataclass
from typing import Optional

_FILL = r"(?:(?:hey|ok(?:ay)?|yo|um+|uh+|just|please|saint|now|right now|already|dude|man|oh|thank you|thanks|bro|friend|buddy|pal|mate|bud|dawg|homie|guys?)[\s,.!]*)*"

_STOP_ALL = re.compile(
    rf"^{_FILL}(?:stop|cancel|abort|kill|halt)\s+(?:everything|it all|all of it|all tasks|all the tasks|"
    rf"all background (?:tasks|work)|whatever you'?re doing)[\s,.!]*{_FILL}$", re.I)
_STOP = re.compile(
    rf"^{_FILL}(?:stop(?:\s+(?:it|that|now|talking|speaking|saying\s+that|what you'?re doing))?|cancel(?:\s+(?:it|that))?|"
    rf"abort(?:\s+(?:it|that))?|shut\s+up|be\s+quiet|quiet|enough|halt|stop\s+stop)[\s,.!]*{_FILL}$", re.I)
_STATUS = re.compile(
    rf"^{_FILL}(?:what\s+are\s+you\s+(?:doing|up\s+to|working\s+on)|what'?s\s+(?:going\s+on|happening|"
    rf"taking\s+so\s+long)|are\s+you\s+(?:still\s+)?(?:working|busy|doing\s+something)|status(?:\s+update)?)"
    rf"(?:\s+(?:right\s+)?now)?[\s,.!?]*$", re.I)
_SILENT_ON = re.compile(
    rf"^{_FILL}(?:(?:go\s+|turn\s+on\s+|enable\s+|switch\s+to\s+)?(?:silent|quiet)\s+mode"
    rf"|be\s+(?:quiet|silent)|stay\s+quiet|stop\s+talking|don'?t\s+(?:talk|answer\s+me|speak))"
    rf"(?:\s+(?:for|during)\s+(?:the\s+next\s+)?(?P<n>\d+|an?|one|two|three|half\s+an)\s*"
    rf"(?P<u>minutes?|mins?|hours?|hrs?))?[\s,.!]*$", re.I)
_SILENT_OFF = re.compile(
    rf"^{_FILL}(?:(?:you\s+can\s+)?(?:talk|speak)\s+(?:again|to\s+me\s+again)|normal\s+mode|"
    rf"(?:turn\s+off|stop|end|disable|exit|leave)\s+(?:the\s+)?(?:silent|quiet)\s+mode|unmute\s+yourself)"
    rf"[\s,.!]*$", re.I)

_VOICE_VOLUME = re.compile(
    rf"^{_FILL}(?:(?:talk|speak)\s+(?P<a>louder|up|quieter|softer|lower|more\s+quietly|more\s+softly)"
    rf"|(?:turn|make)\s+(?:your\s+voice|yourself)\s+(?P<b>up|down|louder|quieter|softer)"
    rf"|your\s+voice\s+is\s+too\s+(?P<c>quiet|loud|soft|low)"
    rf"|(?:set\s+|change\s+|put\s+)?(?:your\s+)?voice\s+(?:volume\s+)?(?:to\s+|at\s+)?(?P<n>\d{{1,3}})\s*(?:%|percent)?)"
    rf"[\s,.!]*{_FILL}$", re.I)
VOICE_STEP = 20

_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "half an": 0.5}


@dataclass
class Meta:
    kind: str                 # stop | stop_all | status | silent_on | silent_off | voice_set | voice_step
    minutes: float = 0.0
    value: float = 0.0        # voice_set: percent; voice_step: +/- percent


def match_meta(text: str) -> Optional[Meta]:
    t = " ".join((text or "").strip().split())
    if not t:
        return None
    if _STOP_ALL.match(t):
        return Meta("stop_all")
    m = _SILENT_ON.match(t)
    # A timed "be quiet for 30 minutes" / any explicit "silent mode" is silent
    # mode; a bare "be quiet" / "stop talking" is a stop.
    if m and (m.group("n") or re.search(r"\b(silent|quiet)\s+mode\b|don'?t\s+(?:talk|answer|speak)", t, re.I)):
        n = (m.group("n") or "").lower()
        minutes = 60.0
        if n:
            value = float(n) if n.isdigit() else _NUM.get(n, 1)
            minutes = value * (60 if m.group("u").lower().startswith("h") else 1)
        return Meta("silent_on", minutes)
    if _SILENT_OFF.match(t):
        return Meta("silent_off")
    if _STOP.match(t):
        return Meta("stop")
    if _STATUS.match(t):
        return Meta("status")
    m = _VOICE_VOLUME.match(t)
    if m:
        if m.group("n"):
            return Meta("voice_set", value=float(m.group("n")))
        word = (m.group("a") or m.group("b") or m.group("c") or "").lower()
        louder = word in ("louder", "up", "quiet", "soft", "low")    # "too quiet" -> louder
        return Meta("voice_step", value=VOICE_STEP if louder else -VOICE_STEP)
    return None


def run_meta(meta: Meta) -> str:
    """Apply a meta command that doesn't need the conversation controller.
    Returns the reply text."""
    from core.activity import activity
    from core.cancel import cancel
    from modules.agent.confirm import confirmations, choices
    from modules.voice.output_policy import output_policy
    if meta.kind in ("stop", "stop_all"):
        cancel.trip("all" if meta.kind == "stop_all" else "current")
        confirmations.clear("dismissed")
        choices.clear()
        from modules.learning.lesson import lessons
        if lessons.stop() and meta.kind == "stop":
            return "Okay, I stopped."
        if meta.kind == "stop_all":
            return "Stopped everything."
        return "Okay."
    if meta.kind == "status":
        return activity.describe()
    if meta.kind == "silent_on":
        output_policy.set_silent(meta.minutes)
        mins = int(round(meta.minutes))
        span = (f"{mins // 60} hour{'s' if mins // 60 != 1 else ''}" if mins >= 60 and mins % 60 == 0
                else f"{mins} minute{'s' if mins != 1 else ''}")
        return f"Silent mode for {span}. I'll still speak up for questions, errors and reminders."
    if meta.kind == "silent_off":
        was = output_policy.silent
        output_policy.clear_silent()
        return "I'm back." if was else "I wasn't in silent mode."
    if meta.kind in ("voice_set", "voice_step"):
        from modules.voice.output_policy import VOLUME_MAX, set_voice_volume, voice_volume
        before = int(round(voice_volume() * 100))
        new = set_voice_volume(meta.value if meta.kind == "voice_set" else before + meta.value)
        if new == before:
            return f"My voice is already at {new}%" + (" — that's as loud as it goes." if new >= VOLUME_MAX
                                                        else ".")
        return f"My voice is at {new}% now." if new else "My voice is muted — say “voice volume 80” to hear me."
    return ""

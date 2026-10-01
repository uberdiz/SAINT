"""
modules/agent/router.py

Deterministic intent routing.

Common commands are recognised here with explicit parsers and executed
through the tool registry — no LLM round-trip, so they are fast and SAINT can
never "pretend" an action happened: every reply is built from the tool's real
result (or its real error).

Each parser returns an ``Intent`` (name + a ``run`` closure) or None. The
agent tries: pending confirmation -> reminders/automations -> memory ->
multi-step split ("open Chrome and search for cats") -> single intents.
Anything that doesn't match falls through to the LLM (which may still call
tools, see modules/agent/llm.py).
"""

import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional

from core.config import config
from core.events import event_bus, EventType
from modules.agent.confirm import confirmations, PendingAction
from modules.automation.tools import get_tool_registry, ToolResult

log = logging.getLogger("saint.agent")


@dataclass
class Reply:
    text: str
    ok: bool = True
    expects_reply: bool = False


@dataclass
class Intent:
    name: str
    run: Callable[[], Reply]
    domain: str = ""


# ---------------------------------------------------------------------- #
# Tool execution helpers
# ---------------------------------------------------------------------- #
def call(tool: str, **kwargs) -> ToolResult:
    return get_tool_registry().execute(tool, **kwargs)


def run_tool(tool: str, describe: str, on_ok: Callable[[object], str], **kwargs) -> Reply:
    """Execute a tool; handle confirmation-required and errors uniformly."""
    res = call(tool, **kwargs)
    if res.success:
        return Reply(on_ok(res.result))
    if res.error_code == "CONFIRM_REQUIRED":
        def run_confirmed():
            r2 = get_tool_registry().execute(tool, _confirmed=True, **kwargs)
            return on_ok(r2.result) if r2.success else (r2.error or f"I couldn't {describe}.")
        confirmations.ask(PendingAction(description=describe, run=run_confirmed, tool=tool))
        return Reply(f"Do you want me to {describe}?", ok=True, expects_reply=True)
    return Reply(res.error or f"I couldn't {describe}.", ok=False)


# "right" is only filler as "Right, ..." — "right click the desktop" is a command.
_FILLERS = re.compile(r"^(?:oh my (?:gosh|god|goodness)|all right|actually|whoa|woah|oh|um+|uh+|so|okay|ok|well|hmm+|and|but|"
                      r"also|wait|alright|right(?=[,.!])|yes|yeah|yep|sure|just|wow|dude|bro)[,.!\s]+",
                      re.I)


def spell_out(text: str) -> str:
    """Letters spelled out loud: "M-O-E" -> "moe", and "open my MO playlist,
    spelled M-O-E" -> "open my moe playlist" (the spelled word replaces the
    misheard one)."""
    t = re.sub(r"\b[A-Za-z](?:-[A-Za-z]){1,}\b", lambda m: m.group(0).replace("-", "").lower(), text or "")
    t = re.sub(r"\bspelled\s+((?:[A-Za-z]\s+){1,}[A-Za-z])\b",
               lambda m: "spelled " + m.group(1).replace(" ", "").lower(), t, flags=re.I)
    m = re.search(r"[,.;!?]?\s*(?:(?:it'?s|that'?s|which is|pronounced|pronoun\w*)\s+(?:\w+\s+)?)?spelled\s+"
                  r"([a-z0-9]+)[.!?]*\s*$", t, re.I)
    if not m:
        return t
    import difflib
    word, rest = m.group(1).lower(), t[:m.start()].rstrip(" ,.;")
    skip = {"open", "play", "my", "the", "a", "an", "i", "meant", "no", "to", "switch", "start", "launch", "close",
            "playlist", "folder", "app", "song", "window", "on", "in", "put", "said"}
    cands = [w for w in re.findall(r"[A-Za-z0-9']+", rest) if w.lower() not in skip]
    if not cands:
        return rest
    best = max(cands, key=lambda w: (w[0].lower() == word[0], difflib.SequenceMatcher(None, w.lower(), word).ratio()))
    return re.sub(rf"\b{re.escape(best)}\b", word, rest, count=1)


def _clean(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^(?:hey\s+)?saint[,.!\s]+", "", t, flags=re.I)
    # Spoken variants of the same thing.
    t = re.sub(r"\b(?:double[- ]left|left[- ]double)[- ]click", "double click", t, flags=re.I)
    t = re.sub(r"^left[- ]click\b", "click", t, flags=re.I)
    t = re.sub(r"\brecycl(?:ing|ed)\s+bin\b", "recycle bin", t, flags=re.I)
    t = re.sub(r"\bdisk,?\s+clean[\s-]?up\b", "disk cleanup", t, flags=re.I)
    t = re.sub(r"\b(?:voice\s?meeter|voice\s?meter|voicemeter)\b", "voicemeeter", t, flags=re.I)
    t = spell_out(t)
    # "Use my browser, make a new tab, and search for X" (2026-09-29, went to the model, which
    # answered with code): the lead-in only says where; the steps after it say what.
    t = re.sub(r"^(?:use|in|on)\s+(?:my|the)\s+(?:web\s+)?browser\s*,\s*(?=(?:and\s+)?(?:make|open|"
               r"create|start|search|go|new)\b)", "", t, flags=re.I)
    for _ in range(3):   # "Actually, um, what time is it?"
        t2 = _FILLERS.sub("", t)
        if t2 == t:
            break
        t = t2
    # A stutter: "Close, close, CS2" -> "close CS2" (logged 2026-09-26, rejected as chatter).
    t = re.sub(r"^(\w+)(?:[,.\s]+\1\b)+[,.]?", r"\1", t, flags=re.I)
    # SAINT only ever recycles, so "move/put X in the recycle bin" is "delete X".
    t = re.sub(r"^(?:move|put|send|throw)\s+(.+?)\s+(?:in|into|to)\s+(?:the\s+)?(?:recycle bin|trash|bin)$",
               r"delete \1", t.rstrip(".!"), flags=re.I)
    # A bare playlist name answers "which playlist?" / repairs a mishearing: "My gym playlist."
    t = re.sub(r"^(?:my|the)\s+([\w' &-]{1,40}?)\s+playlist[.!]*$", r"play my \1 playlist", t, flags=re.I)
    # "Smaller." / "Bigger." right after a window action.
    t = re.sub(r"^(bigger|smaller|wider|narrower|taller|shorter)[.!]*$", r"make it \1", t, flags=re.I)
    t = re.sub(r"^(?:can you|could you|would you|will you|please|can u|i want you to|i'd like you to|"
               r"i want to|go ahead and|let's|lets)\s+", "", t, flags=re.I)
    t = re.sub(r"^(?:please)\s+", "", t, flags=re.I)
    t = re.sub(r"\s+(?:please|for me|now|thanks|thank you)[.!?]*$", "", t, flags=re.I)
    return t.strip().rstrip(".!")


def _lower(text: str) -> str:
    return _clean(text).lower()


# ====================================================================== #
# System (clock, capabilities)
# ====================================================================== #
_DISMISS = re.compile(r"^(?:never ?mind|nevermind|cancel(?: that| it)?|stop(?: that| it)?|forget (?:it|that|about it)|"
                      r"no(?:pe)?|nah|no thanks|not now|that'?s all|that'?s it|nothing|i'?m good|all good|"
                      r"thanks|thank you|thank you saint|ok(?:ay)?|cool|great|got it)$")


def parse_system(text: str) -> Optional[Intent]:
    t = _lower(text)
    if _DISMISS.match(t.strip(" .!?,")):
        def run_dismiss():
            from modules.agent.confirm import choices
            from core.cancel import cancel
            if not t.startswith(("thank", "ok", "cool", "great", "got it", "no", "nah", "that", "nothing",
                                 "i'm good", "im good", "all good")):
                cancel.trip("current")
            confirmations.clear("dismissed")
            choices.clear()
            return Reply("You're welcome." if t.startswith("thank") else "Okay.")
        return Intent("dismiss", run_dismiss, "system")
    if re.search(r"^(what(?:'s| is)? the time|what time is it|tell me the time|time)\??$", t):
        return Intent("time", lambda: Reply(f"It's {datetime.now().strftime('%I:%M %p').lstrip('0')}."), "system")
    if re.search(r"^(what(?:'s| is)? (?:the |today'?s )?(?:date|day)(?: today)?|what day is (?:it|today)|"
                 r"what(?:'s| is) today)\??$", t):
        return Intent("date", lambda: Reply(f"It's {datetime.now().strftime('%A, %B %d').replace(' 0', ' ')}."),
                      "system")
    if re.search(r"^what can you do|^what are you able to do|^help$|^what do you do", t):
        return Intent("capabilities", lambda: Reply(
            "I can play and control Spotify, launch your Steam games, check and clean up your drives, extract "
            "archives with WinRAR, open, move and arrange apps, control volume, your mic and audio output, "
            "set reminders, save and restore workspaces, tell you when something finishes, read your "
            "clipboard and notifications, and control my own window. Just ask."), "system")
    return None


# ====================================================================== #
# Memory
# ====================================================================== #
_PERSONAL_KEYS = re.compile(
    r"\b(name|birthday|favou?rite|wife|husband|partner|girlfriend|boyfriend|fianc\w*|dog|cat|pet|son|"
    r"daughter|kid|mom|mum|dad|mother|father|brother|sister|job|occupation|major|school|university|"
    r"college|team|hobby|hometown|nickname|pronouns?|best friend|boss|project)\b", re.I)
def parse_memory(text: str) -> Optional[Intent]:
    from modules.memory.service import extract_fact, second_person, normalize_key
    raw = _clean(text)
    t = raw.lower()

    # --- inspect ---------------------------------------------------------
    if re.search(r"^(what do you (know|remember) about me|what have you (stored|remembered|saved)|"
                 r"list (my |all |your )?memories|show (me )?(my |your )?memories)", t):
        def run_list():
            res = call("memory.list")
            if not res.success:
                return Reply(res.error, ok=False)
            items = res.result["memories"]
            if not items:
                return Reply("I don't have anything stored about you yet.")
            facts = [second_person(m["content"]) for m in items[:8]]
            more = f" — and {len(items) - 8} more in the Memory page" if len(items) > 8 else ""
            return Reply("Here's what I remember: " + "; ".join(facts) + more + ".")
        return Intent("memory.list", run_list, "memory")

    # --- forget -----------------------------------------------------------
    m = re.match(r"^(?:forget|delete|erase|remove)\s+(?:that\s+|about\s+|the fact that\s+)?(.+)$", t)
    if m and m.group(1).strip() in ("it", "that", "this", "about it"):
        m = None                       # "forget it" is a dismissal, not a memory deletion
    # "Delete the other installers" is about files. Only "forget ..." or an
    # explicit "... from your memory" removes a memory.
    if m and not t.startswith("forget") and not re.search(
            r"\b(?:from (?:your |my )?memor(?:y|ies)|what you (?:know|remember)|that i told you|you remember)\b", t):
        m = None
    if m and not re.search(r"\b(reminder|timer|alarm|automation|song|track|playlist|window|app|workspace|layout|"
                           r"alias|file|folder|files|archive|download|downloads|zip|rar|game|screenshot)s?\b", t):
        target = m.group(1).strip()
        if re.fullmatch(r"(everything|all (of )?(it|that|my memories|your memories)|all memories|everything about me)",
                        target):
            return Intent("memory.forget_all", lambda: run_tool(
                "memory.forget_all", "erase everything I remember about you",
                lambda r: f"Done — I erased {r['deleted']} memories."), "memory")
        query = re.sub(r"^(my|what my)\s+", "", target)

        def run_forget():
            res = call("memory.forget", query=query)
            if res.success:
                return Reply("Okay, I've forgotten that.")
            return Reply(res.error, ok=False)
        return Intent("memory.forget", run_forget, "memory")

    # --- explicit statements ("my favorite X is Y", "remember that ...") -----
    explicit = re.match(r"^(?:please\s+)?(?:remember|note|keep in mind|don't forget)\s+(?:that\s+)?(.+)$", raw, re.I)
    fact = extract_fact(raw)
    if (explicit or fact) and config.get("memory.auto_extract", True) or explicit:
        if explicit and not fact:
            content = explicit.group(1).strip()
            if re.search(r"\b(remind|reminder)\b", content, re.I):
                return None

            def run_note():
                res = call("memory.remember", content=content[0].upper() + content[1:], category="fact")
                if not res.success:
                    return Reply(res.error, ok=False)
                return Reply(f"Got it — I'll remember that {second_person(content)}.")
            return Intent("memory.remember", run_note, "memory")
        if fact and fact.get("generic") and not explicit and not _PERSONAL_KEYS.search(fact["key"]):
            fact = None   # "my internet is down" is not something to memorise
        if fact:
            def run_fact():
                res = call("memory.remember", content=fact["content"], key=fact["key"], value=fact["value"],
                           category=fact["category"])
                if not res.success:
                    return Reply(res.error, ok=False)
                r = res.result
                if r.get("updated") and r.get("previous") and str(r["previous"]).lower() != fact["value"].lower():
                    return Reply(f"Updated — your {fact['key']} is now {fact['value']} (it was {r['previous']}).")
                return Reply(f"Got it — I'll remember that your {fact['key']} is {fact['value']}.")
            return Intent("memory.remember", run_fact, "memory")

    # --- recall -------------------------------------------------------------
    rq = None
    m = re.match(r"^(?:what(?:'s| is| are)|what was|tell me|do you (?:know|remember))\s+(?:what\s+)?my\s+(.+?)(?:\s+is|\s+are)?\??$", t)
    if m:
        rq = m.group(1)
    m2 = re.match(r"^(?:what|which)\s+(.+?)\s+do i\s+(like|love|prefer|use|enjoy)(?:\s+most)?\??$", t)
    if m2:
        rq = f"favorite {m2.group(1)} {m2.group(2)}"
    m3 = re.match(r"^(?:where do i (live|work)|when is my birthday|what(?:'s| is) my name|who am i)\??$", t)
    if m3:
        rq = {"live": "home location live", "work": "employer work"}.get(m3.group(1) or "", t)
    if rq and not re.search(r"\b(reminders?|playlist|song|music|schedule|screen|window|games?|folders?|files?|"
                            r"drives?|downloads?|space|storage|notifications?|aliases|workspaces?|clipboard)\b", rq):
        query = rq

        def run_recall():
            res = call("memory.recall", query=query)
            if not res.success:
                return Reply(res.error, ok=False)
            hits = res.result["memories"]
            if not hits:
                subject = second_person("my " + normalize_key(query)) if not query.startswith("favorite") else \
                    "that"
                return Reply(f"I don't have anything stored about {subject}. Tell me and I'll remember it.")
            best = hits[0]
            if best["key"] and best["value"]:
                return Reply(f"Your {best['key']} is {best['value']}.")
            return Reply(best["spoken"][0].upper() + best["spoken"][1:] + ".")
        return Intent("memory.recall", run_recall, "memory")
    return None


# ====================================================================== #
# Automations / reminders
# ====================================================================== #
def parse_automation(text: str) -> Optional[Intent]:
    from modules.automation.timeparse import extract_reminder, parse_schedule, describe
    raw = _clean(text)
    t = raw.lower()

    if re.search(r"^(what|which|list|show|read)\b.*\b(reminders?|timers?|alarms?|automations?|scheduled)\b|"
                 r"^(do i have|any) (any )?(reminders?|timers?|automations?)", t):
        def run_list():
            res = call("automation.list")
            if not res.success:
                return Reply(res.error, ok=False)
            items = [a for a in res.result["automations"] if a["status"] == "active"]
            if not items:
                return Reply("You don't have any active reminders or automations.")
            parts = [f"{a['title']} — {a['when']}" for a in items[:5]]
            more = f", plus {len(items) - 5} more" if len(items) > 5 else ""
            return Reply(f"You have {len(items)}: " + "; ".join(parts) + more + ".")
        return Intent("automation.list", run_list, "automation")

    m = re.match(r"^(?:cancel|delete|remove|stop|turn off|clear)\s+(?:the\s+|my\s+|that\s+)?"
                 r"(?:(.+?)\s+)?(reminder|timer|alarm|automation)s?(?:\s+(?:to|about|for)\s+(.+))?$", t)
    if m:
        query = (m.group(3) or m.group(1) or "").strip()
        if query in ("all", "all my", "every"):
            query = ""

        def run_cancel():
            res = call("automation.cancel", query=query) if query else call("automation.cancel", query="")
            if res.success:
                c = res.result["cancelled"][0]
                return Reply(f"Cancelled: {c['title']}.")
            return Reply(res.error, ok=False, expects_reply=res.error_code == "AMBIGUOUS")
        return Intent("automation.cancel", run_cancel, "automation")

    rem = extract_reminder(raw)
    if rem is not None:
        if rem["schedule"] is None:
            return Intent("automation.reminder_needs_time",
                          lambda: Reply(f"When should I remind you to {rem['message'].lower()}?", ok=False,
                                        expects_reply=True), "automation")
        schedule, message = rem["schedule"], rem["message"]

        def run_reminder():
            from modules.automation.scheduler import scheduler
            try:
                a = scheduler.create("reminder", message, schedule)
            except ValueError as e:
                return Reply(str(e), ok=False)
            except Exception as e:
                log.exception("reminder.create_failed")
                return Reply(f"I couldn't save that reminder: {e}", ok=False)
            what = "timer" if rem.get("timer") else "reminder"
            return Reply(f"Okay — {what} set for {a.describe()}: {message}.")
        return Intent("automation.create_reminder", run_reminder, "automation")

    # "every weekday at 9 play my focus playlist" -> scheduled command
    if re.search(r"\b(every|each|daily|at \d|tomorrow|tonight|in \d+|in an? )\b", t):
        schedule, rest = parse_schedule(raw)
        cond = None
        mc = re.search(r"\bif\s+spotify\s+is\s+(not\s+)?playing\b", rest, re.I)
        if mc:
            cond = {"type": "spotify_not_playing" if mc.group(1) else "spotify_playing"}
            rest = (rest[:mc.start()] + rest[mc.end():]).strip(" ,")
        if schedule and rest and route_single(rest) is not None:
            command = rest

            def run_sched():
                from modules.automation.scheduler import scheduler
                try:
                    a = scheduler.create("command", command, schedule, condition=cond)
                except ValueError as e:
                    return Reply(str(e), ok=False)
                note = f" (only if Spotify is {'not ' if cond and cond['type'].endswith('not_playing') else ''}playing)" \
                    if cond else ""
                return Reply(f"Scheduled: I'll {command.lower()} {a.describe()}{note}.")
            return Intent("automation.schedule_command", run_sched, "automation")
    return None


# ====================================================================== #
# Spotify
# ====================================================================== #
@dataclass
class SpotifyIntent:
    kind: str
    tool: str
    kwargs: dict


# ---- music concepts (phrasing-independent) -------------------------------------------
# "Change what's playing": a request to move on from the current track, however
# it is phrased. Never a song title.
_CHANGE_TRACK = re.compile(
    r"^(?:(?:play|put on|give me|find|throw on|queue up|switch to|change to|try|go to|start)\s+(?:me\s+)?"
    r"(?:a\s+|an\s+|some\s+)?(?:different|another|other|new|the next|next)(?:\s+(?:song|track|tune|one|thing|music|songs|tracks))?"
    r"|(?:play|put on|give me|find|throw on|try)\s+(?:me\s+)?(?:something|anything|some(?:thing)?)\s+(?:else|different|new)"
    r"|(?:change|switch|swap|mix)\s+(?:up\s+)?(?:the\s+|this\s+|that\s+)?(?:song|track|tune|music|songs|tracks|it(?:\s+up)?|things up)"
    r"|(?:skip|next)(?:\s+(?:it|this|that|this one|that one|the song|this song|that song|the track|this track|song|track|"
    r"one|ahead|forward|to the next(?: one| song| track)?))?"
    r"|(?:skip|move on from)\s+(?:this|that|it)\s+(?:song|track|one)"
    r"|next\s+(?:song|track|one))$")
# "...and I don't like it": change the track AND learn from it.
_REJECT_TRACK = re.compile(
    r"^(?:i'?m\s+|i am\s+)?not\s+(?:really\s+|really\s+)?(?:feeling|vibing with|into|liking|loving)\s+"
    r"(?:this|it|this one|that|that one|this song|this track|the song)"
    r"|^(?:i\s+)?(?:don'?t|do not)\s+(?:want|wanna)\s+(?:to\s+)?(?:hear|listen to)\s+(?:this|it|that)(?:\s+(?:one|song|track))?"
    r"|^(?:this|that)\s+(?:song|track|one)\s+(?:is\s+)?(?:boring|bad|trash|annoying|not it)"
    r"|^(?:ugh|nah|no),?\s+(?:skip|next|change)(?:\s+(?:it|this|this one))?$")
_SIMILAR_TO = re.compile(
    r"^(?:(?:play|put on|give me|find|queue up|recommend|suggest|i want|i'd like|how about)\s+(?:me\s+)?)?"
    r"(?:something|anything|some\s+(?:songs|music|tracks|stuff)|songs|music|tracks|stuff|more)\s+"
    r"(?:like|similar to|in the style of|that sounds like|along the lines of)\s+(.+)$")
_BY_ARTIST = re.compile(
    r"^(?:play|put on|give me|find|queue up|i want)\s+(?:me\s+)?(?:something|anything|a song|some(?:thing)?|"
    r"some\s+songs?|some\s+music|a track|songs|music)\s+(?:by|from)\s+(.+)$")
_DESKTOP_CONTEXT = re.compile(r"\b(video|youtube|tab|window|screen|monitor|display|browser|page|website|netflix|"
                              r"twitch|desktop|app|mouse|cursor)\b")


def _stt_music_fix(lower: str) -> str:
    """Common speech-to-text slips at the start of music commands
    ("Place something like DAMN" -> "play ...")."""
    if not _DESKTOP_CONTEXT.search(lower):
        lower = re.sub(r"^(?:place|plays|played|blay|pay|lay)\s+(?=(?:something|some|me|a |the |my |music|songs?|"
                       r"anything|[a-z]))", "play ", lower)
    return lower


_MOOD_WORDS = {"energetic": "energetic", "upbeat": "energetic", "hype": "energetic", "harder": "energetic",
               "hyped": "energetic", "turnt": "energetic", "darker": "darker", "dark": "darker",
               "chill": "chill", "chiller": "chill", "calmer": "chill", "calm": "chill", "mellow": "chill",
               "mellower": "chill", "relaxed": "chill", "sad": "sad", "sadder": "sad", "happier": "happy",
               "happy": "happy"}
_DJ_MOOD = re.compile(
    r"^(?:(?:make it|play|give me|put on|go|let'?s go|switch to|something|play something|give me something)\s+)?"
    r"(?:something\s+)?(?:a\s+(?:bit|little)\s+|way\s+|much\s+)?(?:more\s+)?"
    r"(energetic|upbeat|hype|hyped|harder|turnt|darker|dark|chill|chiller|calmer|calm|mellow|mellower|relaxed|"
    r"sad|sadder|happier|happy)(?:\s+(?:music|songs?|stuff|vibes?|tracks?))?$")
_NO_MORE = re.compile(r"^(?:no more|ban|block|stop recommending|don'?t play(?: me)?(?: any)?(?: more)?)\s+(?:of\s+)?"
                      r"(?:songs by\s+|music by\s+)?(.+?)(?:\s+(?:anymore|any more|again|please))?$")


# "Queue more songs like this" / "cue some more like this" / "more like this": keep the
# song that's playing and fill the queue (it used to skip to a new song and add one more).
_QUEUE_SIMILAR = re.compile(
    r"^(?:(?:can you|could you|please)\s+)?(?:queue|cue|q|add|put|throw|line)(?:\s+up)?\s+(?:me\s+)?"
    r"(?:(?P<n>\d+|a few|a couple(?: of)?|some|some more|three|four|five|six|seven|eight|nine|ten)\s+)?(?:more\s+)?"
    r"(?:songs?|tracks?|music|stuff|ones?|bangers)?\s*(?:like|similar to)\s+"
    r"(?:this|that|it|this one|that one|this song|that song|this track|what'?s playing)(?:\s+(?:one|song|track))?"
    r"(?:\s+(?:to|in|into|on)\s+(?:the |my )?queue)?(?:\s+(?:please|for me))?$|"
    r"^(?:(?:play|give me|let'?s (?:hear|get))\s+)?(?:some\s+)?more\s+(?:songs?\s+|music\s+|tracks?\s+|stuff\s+)?"
    r"like\s+(?:this|that)(?:\s+(?:one|song|track))?(?:\s+(?:please|next|after this|after it))?$|"
    r"^keep (?:it )?going with (?:more )?(?:songs? |music )?like (?:this|that)(?: one)?$|"
    r"^(?:add|queue|cue)\s+(?:some\s+)?(?:similar|more)\s+(?:songs?|tracks?|music)(?:\s+(?:to|in)\s+(?:the |my )?queue)?$")
_COUNT_WORDS = {"a few": 5, "a couple": 3, "a couple of": 3, "some": 5, "some more": 5, "three": 3, "four": 4,
                "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}

# Mood -> a queue built from scratch out of the user's own listening (spotify.play_recommended).
_MOOD_SYNONYMS = {
    "sad": "sad", "down": "sad", "depressed": "sad", "heartbroken": "sad", "lonely": "sad", "blue": "sad",
    "emotional": "sad", "in my feelings": "sad",
    "chill": "chill", "chilled": "chill", "relaxed": "chill", "calm": "chill", "mellow": "chill", "tired": "chill",
    "sleepy": "chill", "stressed": "chill", "anxious": "chill", "lazy": "chill", "cozy": "chill", "vibing": "chill",
    "happy": "happy", "good": "happy", "great": "happy", "cheerful": "happy", "upbeat": "happy",
    "hype": "energetic", "hyped": "energetic", "pumped": "energetic", "energetic": "energetic",
    "turnt": "energetic", "gym": "energetic", "workout": "energetic", "working out": "energetic",
    "lifting": "energetic", "running": "energetic",
    "focus": "focus", "focused": "focus", "studying": "focus", "study": "focus", "working": "focus",
    "reading": "focus", "coding": "focus", "productive": "focus",
    "party": "party", "partying": "party", "pregame": "party", "pregaming": "party", "lit": "party",
    "romantic": "romantic", "in love": "romantic", "love": "romantic",
    "angry": "angry", "mad": "angry", "pissed": "angry", "pissed off": "angry", "furious": "angry",
    "dark": "darker", "darker": "darker", "moody": "darker", "gloomy": "darker",
}
_MOOD_ALT = "|".join(sorted((re.escape(k) for k in _MOOD_SYNONYMS), key=len, reverse=True))
_FEELING = re.compile(rf"^(?:i'?m|i am|i feel|feeling|i'?m feeling|i am feeling)\s+(?:so\s+|really\s+|kinda\s+|pretty\s+|"
                      rf"a (?:bit|little)\s+)?(?:in (?:a|the)\s+)?(?P<m>{_MOOD_ALT})(?:\s+(?:mood|mode|vibe))?"
                      rf"(?:\s+(?:right now|today|tonight|rn))?"
                      rf"(?:[,.]?\s+(?:so\s+)?(?:play|put on|queue up|give me|make me)\b.*)?$")
_MOOD_QUEUE = re.compile(
    rf"^(?:play|put on|queue up|make|build|create|give|set up|start)\s+(?:me\s+)?(?:a\s+|an\s+|some\s+)?(?:new\s+)?"
    rf"(?P<m>{_MOOD_ALT})\s+(?:queue|playlist|mix|set|session|music|songs|vibes?)(?:\s+(?:for me|from scratch))?$|"
    rf"^(?:play|put on|queue up)\s+(?:me\s+)?(?:something|some music|music|songs)\s+(?:for|to)\s+(?:the\s+|my\s+)?"
    rf"(?P<m2>gym|workout|working out|study|studying|focus|work|working|party|partying|relax|relaxing|chill|"
    rf"chilling|sleep|coding|reading|pregame|pregaming)$")
_MOOD_AUTO = re.compile(
    r"^(?:play|put on|queue up|make|build|create|give|pick)\s+(?:me\s+)?(?:something|some music|music|songs|a queue|"
    r"a mix|a playlist|a new queue)\s+(?:for|to match|that (?:fits|matches)|based on|for how i'?m feeling|"
    r"that fits how i feel)(?:\s+(?:my|the))?(?:\s+(?:mood|vibe|feeling))?$|"
    r"^(?:match|read|fit) my (?:mood|vibe)$|^(?:make|build|create|start) (?:me )?(?:a )?(?:new )?queue"
    r"(?: from scratch)?(?: for me)?$|^(?:play|put on) something for (?:my|the) (?:mood|vibe)$")
_WORD_NUM = {"ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
             "eighty": 80, "ninety": 90, "hundred": 100, "a hundred": 100, "one hundred": 100}
_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}


def _mood_request(lower: str) -> Optional[str]:
    """'I'm feeling sad', 'make me a chill queue', 'play something for my mood' -> a mood ('auto' = infer)."""
    if _MOOD_AUTO.match(lower):
        return "auto"
    m = _MOOD_QUEUE.match(lower)
    if m:
        return _MOOD_SYNONYMS.get(m.group("m") or "", None) or {
            "gym": "energetic", "workout": "energetic", "working out": "energetic", "party": "party",
            "partying": "party", "pregame": "party", "pregaming": "party", "sleep": "chill", "relax": "chill",
            "relaxing": "chill", "chill": "chill", "chilling": "chill"}.get(m.group("m2") or "", "focus")
    m = _FEELING.match(lower)
    if m:
        from modules.agent.context import desktop_context
        # "I'm tired" is conversation. "I'm feeling sad" while music plays, or "..., play something", is a request.
        if re.search(r"\b(play|put on|queue|give me|make me)\b", lower) or (
                re.search(r"\b(feel|feeling|mood|vibe)\b", lower) and desktop_context.music_is_context()):
            return _MOOD_SYNONYMS[m.group("m")]
    return None


def _spoken_volume(lower: str) -> str:
    """'turn it up to AD' (Whisper's "eighty"), 'volume to seventy five' -> digits."""
    lower = re.sub(r"\bto (?:a\.?d\.?|a d|eddie|80s)$", "to 80", lower)

    def num(m):
        tens = _WORD_NUM.get(m.group(1), 0)
        return str(tens + _UNITS.get(m.group(2) or "", 0))
    return re.sub(r"\b(ten|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|(?:a |one )?hundred)"
                  r"(?:[\s-](one|two|three|four|five|six|seven|eight|nine))?\b(?=\s*(?:%|percent)?$)", num, lower)


def _dj_intent(lower: str) -> Optional[SpotifyIntent]:
    """DJ mode: steer what plays by mood, the last song, novelty or artist bans."""
    if re.fullmatch(r"(?:dj|dj mode|be my dj|start dj mode|you'?re the dj|play dj)", lower):
        return SpotifyIntent("play_for_me", "spotify.play_recommended", {"context": ""})
    m = re.match(r"^bring (?:the )?energy (?:back )?(up|down)$|^(?:turn|crank) up the energy$|^calm it down$", lower)
    if m:
        mood = "chill" if (m.group(1) == "down" or lower.startswith("calm")) else "energetic"
        return SpotifyIntent("dj", "spotify.play_recommended", {"mood": mood})
    m = _DJ_MOOD.match(lower)
    if m and (lower.split()[0] in ("make", "play", "give", "put", "go", "let's", "lets", "switch", "something", "more")
              or lower in _MOOD_WORDS):
        return SpotifyIntent("dj", "spotify.play_recommended", {"mood": _MOOD_WORDS[m.group(1)]})
    if re.match(r"^(?:play |give me |put on )?(?:something|more|songs?|music)\s+(?:more\s+)?like the "
                r"(?:last|previous) (?:song|track|one)$|^back to (?:something like )?the last (?:song|vibe)$", lower):
        return SpotifyIntent("dj", "spotify.play_recommended", {"seed_last": True})
    if re.match(r"^(?:play |give me |find me |put on )?(?:something|songs?|music|stuff)\s+i (?:haven'?t|have not|"
                r"never) (?:heard|listened to)(?: before)?$|^(?:play |give me )?something (?:completely )?new$|"
                r"^surprise me with something new$", lower):
        return SpotifyIntent("dj", "spotify.play_recommended", {"novel": True})
    m = re.match(r"^(?:unban|unblock|allow)\s+(.+?)(?:\s+again)?$", lower)
    if m and m.group(1) not in ("it", "this", "that", "notifications"):
        return SpotifyIntent("unban", "spotify.unban_artist", {"name": m.group(1)})
    if re.match(r"^(?:no[,. ]+)?(?:nope[,. ]+)?(?:never|don'?t|do not)\s+(?:ever\s+)?play\s+(?:that|this|the)\s+playlist"
                r"(?:\s+(?:ever\s+)?again)?(?:\s+ever(?:\s+again)?)?$|^(?:ban|block)\s+(?:that|this|the)\s+playlist$", lower):
        return SpotifyIntent("ban_playlist", "spotify.ban_playlist", {})
    m = _NO_MORE.match(lower)
    if m:
        who = m.group(1).strip()
        if re.fullmatch(r"(?:this|that|the)?\s*(?:artist|guy|rapper|singer|band|one)|him|her|them", who):
            return SpotifyIntent("ban", "spotify.ban_artist", {})
        if who and not re.search(r"\b(song|songs|track|tracks|music|this|that|it|ads?|notifications?|spotify|"
                                 r"playlist|album|videos?|windows?|tabs?)\b", who) and len(who.split()) <= 4:
            return SpotifyIntent("ban", "spotify.ban_artist", {"name": who})
    if re.match(r"^(?:i'?m|i am) (?:done|sick|tired) (?:with|of) (?:this|that) (?:artist|guy|rapper|singer|band)$",
                lower):
        return SpotifyIntent("ban", "spotify.ban_artist", {})
    return None


def spotify_intent(text: str) -> Optional[SpotifyIntent]:
    """Map a music utterance to a Spotify tool. Works without the word 'spotify'."""
    lower = _lower(text)
    lower = re.sub(r"\s+(?:on|in|with) spotify$", "", lower)
    lower = _stt_music_fix(lower)
    wc = len(lower.split())

    def has(p):
        return re.search(p, lower) is not None

    # Commands about a video, a tab, a window... belong to the desktop, not Spotify.
    if _DESKTOP_CONTEXT.search(lower) and not re.search(r"(?:\b(?:on|in|with) spotify|^(?:saint,? )?spotify\b)[.!?]*$",
                                                        text.lower().strip()):
        return None
    from modules.agent.context import desktop_context
    watching = desktop_context.domain() == "browser"

    # --- change the track (a request, never a title) ------------------------
    if _REJECT_TRACK.search(lower):
        return SpotifyIntent("next_reject", "spotify.next", {})
    if _CHANGE_TRACK.match(lower):
        return SpotifyIntent("next", "spotify.next", {})

    # --- "queue more songs like this": keep playing, fill the queue --------------------------
    m = _QUEUE_SIMILAR.match(lower)
    if m:
        n = m.group("n")
        count = int(n) if n and n.isdigit() else _COUNT_WORDS.get(n or "", 5)
        return SpotifyIntent("queue_similar", "spotify.queue_similar", {"count": max(1, min(10, count))})
    if has(r"^(?:stop|turn off|disable|cancel|no more)\s+(?:the\s+)?(?:auto[- ]?queu(?:e|ing)|radio|"
           r"queu(?:e|ing) (?:more )?songs|adding (?:more )?songs)(?:\s+(?:to the queue|automatically))?$"):
        return SpotifyIntent("stop_autoqueue", "spotify.stop_autoqueue", {})
    # --- a queue built from scratch for a mood ("I'm feeling sad", "make me a chill queue") ----
    mood = _mood_request(lower)
    if mood:
        return SpotifyIntent("mood", "spotify.play_recommended", {"mood": mood})

    # --- DJ mode ("more energetic", "something darker", "more like the last song") ----------
    dj = _dj_intent(lower)
    if dj is not None:
        return dj

    # --- "something like X" / "something by X" ------------------------------
    m = _SIMILAR_TO.match(lower)
    if m:
        seed = m.group(1).strip()
        if re.fullmatch(r"(this|that|it|this one|this song|this track|what's playing|what is playing|"
                        r"the current song|what i'm listening to)", seed):
            return SpotifyIntent("similar", "spotify.play_recommended", {"similar_to_current": True})
        raw_m = _SIMILAR_TO.match(_stt_music_fix(_clean(text)))
        seed_orig = raw_m.group(1).strip() if raw_m else seed
        return SpotifyIntent("similar_seed", "spotify.play_recommended", {"seed": seed_orig})
    m = _BY_ARTIST.match(lower)
    if m:
        return SpotifyIntent("play_artist", "spotify.play_query", {"query": m.group(1).strip(), "kind": "artist"})

    # --- replay / seek ------------------------------------------------------------
    if has(r"^(?:replay|restart|start over)(?: (?:this|the|that))?(?: (?:song|track|one))?$|"
           r"^(?:play|start) (?:this|that|it|the song|this song) (?:again|over|from the (?:start|beginning))$|"
           r"^(?:go back to|back to) the (?:start|beginning)(?: of the song)?$|^from the top$"):
        return SpotifyIntent("replay", "spotify.replay", {})
    m = re.search(r"^(?:skip|fast forward|jump|go)\s+(ahead|forward|back|backward|backwards)\s+(\d+)\s*(seconds?|secs?|minutes?|mins?)$",
                  lower) or re.search(r"^(rewind)\s+(\d+)\s*(seconds?|secs?|minutes?|mins?)$", lower)
    if m:
        n = int(m.group(2)) * (60 if m.group(3).startswith("min") else 1)
        sign = -1 if m.group(1) in ("back", "backward", "backwards", "rewind") else 1
        return SpotifyIntent("seek_rel", "spotify.seek", {"seconds": sign * n, "relative": True})
    m = re.search(r"^(?:go|skip|jump|seek) to (\d+)(?::(\d{2})| minutes?(?: and (\d+) seconds?)?)$", lower)
    if m:
        secs = int(m.group(1)) * 60 + int(m.group(2) or m.group(3) or 0)
        return SpotifyIntent("seek", "spotify.seek", {"seconds": secs})

    # --- smart shuffle ------------------------------------------------------------------
    if has(r"\bsmart ?shuffle\b"):
        return SpotifyIntent("smart_shuffle", "spotify.smart_shuffle",
                             {"state": not has(r"\b(off|stop|disable|no)\b")})

    # --- current artist / album -----------------------------------------------------
    if has(r"^(?:who(?:'s| is)? (?:this|singing|playing|the artist)|who sings this|what artist is this|"
           r"who(?:'s| is) this by|who made this)"):
        return SpotifyIntent("current_artist", "spotify.current", {})
    if has(r"^what album is (?:this|that|it)(?: from| on)?|^which album|^what(?:'s| is) the album"):
        return SpotifyIntent("current_album", "spotify.current", {})

    # --- search (without playing) ------------------------------------------------------
    m = re.match(r"^(?:search|look up|find)\s+(?:on\s+)?spotify\s+for\s+(.+)$", _lower(text)) or \
        re.match(r"^search\s+(?:for\s+)?(.+?)\s+on\s+spotify$", _lower(text))
    if m:
        return SpotifyIntent("search", "spotify.search", {"query": m.group(1).strip(), "types": "track,artist,album,playlist"})

    # --- "remove this song from the playlist" (the one it's playing from) ----------------
    if has(r"^(?:remove|delete|take|get rid of|drop)\s+(?:this|that|the|it)?\s*(?:song|track|one)?\s*"
           r"(?:from|off|out of)\s+(?:the|this|that|my)\s+playlist$"):
        return SpotifyIntent("playlist_remove", "spotify.remove_current_from_playlist", {})

    # --- "that's not a chill song": the song doesn't fit the mood -----------------------
    # Right after the queue was read out it's about the next song ("Up next: 2K
    # FREESTYLE" ... "that's not a chill song" got "I haven't done anything yet",
    # 2026-09-30); otherwise the one playing.
    m = _NOT_MOOD.match(lower)
    if m and (m.group("mood") or desktop_context.music_is_context()):
        mood = _NOT_MOOD_WORDS.get((m.group("mood") or "").lower(), "")
        which = "next" if _last_was_queue_list() else "current"
        return SpotifyIntent("not_mood", "spotify.not_mood", {"mood": mood, "which": which})

    # --- queue removal isn't possible through Spotify's API -------------------------------
    if has(r"^(?:remove|delete|take)\b.*\b(?:from|off|out of) (?:the |my )?queue$|^clear (?:the |my )?queue$"):
        return SpotifyIntent("queue_remove", "", {})

    # --- what's queued (read from the API, nothing on screen changes) -------------
    if has(r"^(?:list|show|read|tell me|say|give me|go through)\b.*\b(?:queue|up next|coming up)\b|"
           r"^what(?:'?s| is| are| songs? (?:are|is))\b.*\b(?:(?:in|on) (?:the |my )?queue|queued|up next|coming up)\b|"
           r"^what(?:'?s| is) (?:up )?next\b|^(?:my|the) queue$"):
        return SpotifyIntent("queue_list", "spotify.queue_list", {"limit": 10})

    # --- what's playing (before "play") ---------------------------------
    if has(r"what(?:'?s| is| am i)\b.*\b(playing|listening to|song|track)\b") and not has(r"\b(today|lately|yesterday|this week|been)\b") \
            or has(r"what song is (this|playing|that)") or has(r"what(?:'s| is) this (song|track)") \
            or has(r"\b(currently playing|now playing)\b") or has(r"^who(?:'?s| is) (this|singing|the artist)"):
        return SpotifyIntent("current", "spotify.current", {})

    # --- listening history / taste ------------------------------------------
    if has(r"what (?:have|did) i (?:been )?listen(?:ed|ing)? to|what did i play|my listening history|"
           r"songs? (?:i|i've) (?:played|listened to) today|listening to (?:today|lately|this week)"):
        period = "week" if has(r"\b(week|lately|recently)\b") else "today"
        return SpotifyIntent("history", "spotify.history", {"period": period})
    if has(r"what (?:kind of |sort of )?music do i (?:like|listen to)|my music taste|who(?:'s| is| are) my (?:top|favou?rite) artists?"):
        return SpotifyIntent("taste", "spotify.taste", {})

    # --- feedback -----------------------------------------------------------------
    if has(r"^(i (?:really )?(?:like|love|dig) (?:this|this song|this track|it)|this (?:song|track) is (?:great|good|fire)|"
           r"thumbs up)$"):
        return SpotifyIntent("like", "spotify.feedback", {"signal": 1.0})
    # Anchored: "I hate this game" (logged 2026-09-29, mid-match) is not about the song.
    if has(r"^(i (?:don'?t|do not) like (?:this|this song|this track|it)|i hate (?:this|this song|this track|it)|"
           r"thumbs down|this (?:song|track) (?:sucks|is bad)|don'?t (?:play|recommend) (?:this|songs like this))"
           r"(?:\s+(?:song|track|one|at all|anymore|any more))?$"):
        return SpotifyIntent("dislike", "spotify.feedback", {"signal": -1.0})

    # --- add current to playlist ------------------------------------------------
    m = re.search(r"^(?:add|save|put) (?:this|this song|this track|it|the current song|what's playing) (?:to|in|into|on) (?:my |the )?(.+?)(?: playlist)?$", lower)
    if m and re.fullmatch(r"(?:the )?(?:left|right|top|bottom|middle|center|full ?screen|other side|side|front|back)"
                          r"(?: side| half| corner)?", m.group(1).strip()):
        m = None          # "put it on the left" / "put it in fullscreen" are window commands
    if m:
        return SpotifyIntent("add_to_playlist", "spotify.add_current_to_playlist", {"playlist": m.group(1).strip()})

    # --- queue ----------------------------------------------------------------------
    m = re.search(r"^(?:queue(?: up)?|cue(?: up)?|add) (.+?) (?:to (?:the|my) queue|next)$", lower) or \
        re.search(r"^(?:queue|cue)(?: up)? (.+)$", lower) or re.search(r"^play (.+?) next$", lower)
    if m and not has(r"^play (the )?next\b"):
        return SpotifyIntent("queue", "spotify.queue", {"query": m.group(1).strip()})

    # --- previous / back --------------------------------------------------------
    if has(r"\b(previous|last) (song|track|one)\b") or (has(r"^go back\b") and (
            desktop_context.music_is_context() or has(r"\b(song|track)\b"))) or has(r"\bplay (the )?(previous|last) (song|track|one)\b") \
            or (has(r"\bprevious\b") and wc <= 4) or has(r"\bback a (song|track)\b") or has(r"^(rewind|replay that)$"):
        return SpotifyIntent("previous", "spotify.previous", {})

    # --- next / skip ------------------------------------------------------------
    if has(r"\b(skip|next)\b") and (has(r"\b(song|track|this|it|ahead|one)\b") or has(r"\bspotify\b") or wc <= 3):
        return SpotifyIntent("next", "spotify.next", {})

    # --- pause ----------------------------------------------------------------------
    if watching and lower in ("pause", "resume", "play", "unpause", "pause it", "play it", "resume it"):
        return None
    if has(r"^pause\b") or has(r"\bpause (the |my )?(music|song|spotify|playback|it)\b") \
            or has(r"\bstop (the |my )?(music|song|spotify|playback|playing)\b") or has(r"\b(music|spotify) (off|stop)\b"):
        return SpotifyIntent("pause", "spotify.pause", {})

    # --- volume ---------------------------------------------------------------------
    vl = _spoken_volume(lower)
    vol = re.search(r"\bvolume\b.*?(\d{1,3})", vl) or re.search(r"\bset (?:the )?(?:music |spotify )?volume (?:to )?(\d{1,3})", vl) \
        or re.search(r"\b(?:turn|put|set|bring) (?:it|this|the music|spotify|the volume|the song)? ?(?:up |down |back )?to (\d{1,3})", vl) \
        or re.search(r"^(?:turn|put|bring) (?:it )?(?:up|down|back) to (\d{1,3})", vl)
    if vol:
        return SpotifyIntent("volume_set", "spotify.volume", {"percent": max(0, min(100, int(vol.group(1))))})
    # "Turn Spotify down" / "turn the music up a little" is Spotify; a bare "turn it
    # down" is the whole PC (Voicemeeter) unless audio.turn_it_means is "music".
    # "Turn it back up" after "turn Spotify down" is Spotify again.
    last_target, last_points = desktop_context.volume_target()
    musical = has(r"\b(music|song|spotify|track|tune)\b") or last_target == "spotify" or (
        config.get("audio.turn_it_means", "system") == "music" and desktop_context.music_is_context())
    from modules.agent.desktop_intents import volume_step_points
    step = volume_step_points(lower, int(config.get("spotify.volume_step", 10)))
    if has(r"\bback\b") and last_target == "spotify" and last_points:
        step = last_points
    if musical and has(r"\b(volume up|louder|turn (it|this|the music|spotify|the volume|the song) (back )?(a (little )?bit )?up|turn up (the |my )?(music|volume|song|spotify)|crank it|(raise|increase) (the )?volume)\b"):
        return SpotifyIntent("volume_up", "spotify.volume_step", {"direction": "up", "step": step})
    if musical and has(r"\b(volume down|quieter|softer|turn (it|this|the music|spotify|the volume|the song) (back )?(a (little )?bit )?down|turn down (the |my )?(music|volume|song|spotify)|(lower|decrease|reduce) (the )?volume)\b"):
        return SpotifyIntent("volume_down", "spotify.volume_step", {"direction": "down", "step": step})

    # --- shuffle / repeat -------------------------------------------------------
    if has(r"\bshuffle\b"):
        return SpotifyIntent("shuffle", "spotify.shuffle", {"state": not has(r"\b(off|stop|disable|no)\b")})
    if has(r"\brepeat\b") and not has(r"^repeat (that|after)"):
        mode = "off" if has(r"\b(off|stop|disable|no)\b") else ("track" if has(r"\b(one|this|track|song|single)\b") else "context")
        return SpotifyIntent("repeat", "spotify.repeat", {"state": mode})

    # --- resume -------------------------------------------------------------------
    if has(r"^(resume|unpause|continue)\b") and (wc <= 3 or has(r"\b(music|song|spotify|playback|playing)\b")) \
            or lower in ("play", "play music", "play spotify", "play the music", "start the music", "keep playing"):
        return SpotifyIntent("resume", "spotify.play", {})
    # "Press play on Spotify" / "hit play" / "play my Spotify" (logged 2026-09-28: went to the
    # planner, which tried a key press, and "my Spotify" was taken for a playlist name).
    if has(r"^(?:press|hit|click|tap|push)(?: the)? play(?: button)?(?: (?:on|in) (?:my |the )?spotify)?$") \
            or has(r"^(?:play|start) (?:on |in )?(?:my |the )?spotify(?: again)?$"):
        return SpotifyIntent("resume", "spotify.play", {})

    # --- recommendations -------------------------------------------------------
    if has(r"\b(something|anything|more|songs?|music) (similar|like this|like that)\b") or has(r"^more like this$"):
        play = not has(r"^(recommend|suggest)")
        return SpotifyIntent("similar", "spotify.play_recommended" if play else "spotify.recommend",
                             {"similar_to_current": True})
    if has(r"^(recommend|suggest)\b.*\b(song|music|something|track|artist|album)\b|^what should i listen to"):
        return SpotifyIntent("recommend", "spotify.recommend", {"context": ""})
    if has(r"^play (me )?(something|some music|some songs|music|songs) (i(?:'d| would)? (like|love|enjoy)|for me|good|you think i'?d like)") \
            or has(r"^(surprise me|play my (kind of |type of )?music|play (something|anything) (i like|good))$") \
            or has(r"^play (something|anything)$"):
        ctx = re.sub(r"^play (me )?(something|some music|music)\s*", "", lower)
        ctx = re.sub(r"\b(i(?:'d| would)? (like|love|enjoy)|for me|good|you think i'?d like)\b", "", ctx).strip()
        return SpotifyIntent("play_for_me", "spotify.play_recommended", {"context": ctx})
    if has(r"^play (me )?something (upbeat|chill|relaxing|calm|energetic|sad|happy|mellow|to focus|for studying|for working out)"):
        mood = re.sub(r"^play (me )?something\s*", "", lower)
        return SpotifyIntent("play_for_me", "spotify.play_recommended", {"context": mood})

    # --- play ... --------------------------------------------------------------------
    m = re.match(r"^(?:play|put on|start|listen to|i want to (?:hear|listen to))\s+(.+)$", lower)
    if not m:
        return None
    q = m.group(1).strip()
    raw_q = re.match(r"^(?:play|put on|start|listen to|i want to (?:hear|listen to))\s+(.+)$", _clean(text), re.I)
    q_orig = re.sub(r"\s+on spotify$", "", raw_q.group(1).strip(), flags=re.I) if raw_q else q

    if re.match(r"^(my )?(liked songs|liked music|favou?rites|favou?rite songs|saved songs|library)$", q):
        return SpotifyIntent("liked", "spotify.play_liked", {})
    if re.match(r"^(?:my|a|one of my|the)\s+playlists?$", q):
        # No name given: ask which (it used to play a stranger's playlist called "£").
        return SpotifyIntent("choose_playlist", "spotify.playlists", {})
    # "a playlist called Dominican Dembow", "a Spanish playlist", "my gym playlist"
    pm = re.match(r"^(?:my |the |a |an |some )?playlist (?:called |named |titled )?(.+)$", q_orig, re.I) \
        or re.match(r"^(?:my |the |a |an |some )?(.+?) playlist$", q_orig, re.I) \
        or re.match(r"^my (?!music$|songs$)(.+)$", q_orig, re.I)
    if pm:
        args = {"query": pm.group(1).strip(), "kind": "playlist"}
        if re.match(r"^my\s", q_orig, re.I):
            args["own_only"] = True            # "my X playlist" is one of the user's own
        return SpotifyIntent("play_playlist", "spotify.play_query", args)
    am = re.match(r"^(?:the )?album (.+)$", q_orig, re.I) or re.match(r"^(.+?) (?:the )?album$", q_orig, re.I)
    if am:
        return SpotifyIntent("play_album", "spotify.play_query", {"query": am.group(1).strip(), "kind": "album"})
    ar = re.match(r"^(?:(?:some |more )?(?:songs|music|tracks|stuff) (?:by|from)|the artist|artist) (.+)$", q_orig, re.I)
    if ar:
        return SpotifyIntent("play_artist", "spotify.play_query", {"query": ar.group(1).strip(), "kind": "artist"})
    tr = re.match(r"^(?:the )?(?:song|track) (.+)$", q_orig, re.I)
    if tr:
        return SpotifyIntent("play_track", "spotify.play_query", {"query": tr.group(1).strip(), "kind": "track"})
    from modules.spotify.tools import GENRES
    gm = re.match(r"^(?:some |a bit of |a little )?(.+?)(?: music| songs| tunes| vibes)?$", q_orig, re.I)
    if gm and gm.group(1).lower().strip() in GENRES:
        return SpotifyIntent("play_genre", "spotify.play_query", {"query": gm.group(1).strip(), "kind": "genre"})
    if re.match(r"^(music|some music|some songs|songs|a song|spotify|my spotify|the music)$", q):
        return SpotifyIntent("resume", "spotify.play", {})
    some = re.match(r"^some (.+)$", q_orig, re.I)
    return SpotifyIntent("play", "spotify.play_query", {"query": (some.group(1) if some else q_orig).strip(),
                                                        "kind": "auto"})


_NOT_MOOD_WORDS = {"chill": "chill", "relaxing": "chill", "calm": "chill", "mellow": "chill", "laid back": "chill",
               "sad": "sad", "happy": "happy", "upbeat": "happy", "hype": "energetic", "energetic": "energetic",
               "workout": "energetic", "gym": "energetic", "dark": "darker", "party": "party", "focus": "focus",
               "study": "focus", "romantic": "romantic", "angry": "angry"}
_NOT_MOOD = re.compile(
    r"^(?:no+[,.!]*\s+|nah[,.!]*\s+)?(?:(?:that'?s|thats|that is|this is|this'?s|it'?s|its|that one'?s)\s+"
    r"(?:not|not,? that'?s not)|(?:that|this|it|that one)\s+(?:isn'?t|ain'?t))\s+"
    r"(?:a\s+|really\s+|very\s+|exactly\s+|that\s+)?"
    r"(?:(?P<mood>chill|relaxing|calm|mellow|laid back|sad|happy|upbeat|hype|energetic|workout|gym|dark|party|"
    r"focus|study|romantic|angry)(?:\s+(?:song|track|one|music|vibe|enough))?|the\s+vibe|my\s+vibe|the\s+mood|"
    r"what i'?m feeling)[.!?]*$")


def _last_was_queue_list() -> bool:
    try:
        from modules.learning.feedback import feedback
        turns = feedback.recent_turns(90.0)
    except Exception:
        return False
    return bool(turns) and turns[-1]["intent"] == "spotify.queue_list"


def _spotify_reply(si: SpotifyIntent, r) -> str:
    k = si.kind
    if k == "not_mood":
        what = f"{r.get('track') or 'That one'}" + (f" by {r['artist']}" if r.get("artist") else "")
        mood = r.get("mood")
        fits = f"isn't {mood}" if mood else "doesn't fit"
        if r.get("which") == "next":
            return f"Got it — {what} {fits}. I'll skip it when it comes up" + \
                (f" and keep it out of {mood} mixes." if mood else ".")
        return f"Got it — {what} {fits}. Skipped" + (f", and I'll keep it out of {mood} mixes." if mood else ".")
    if k == "playlist_remove":
        return f"Took {r.get('track') or 'it'} out of {r.get('playlist') or 'the playlist'} and skipped it."
    if k == "queue_list":
        q = r.get("queue") or []
        if not q:
            return "Your queue is empty." if r.get("current") else "Nothing is playing, so there's no queue."
        songs = [f"{t['name']} by {t['artist']}" if t.get("artist") else t["name"] for t in q[:5]]
        text = "Up next: " + (", ".join(songs[:-1]) + ", then " + songs[-1] if len(songs) > 1 else songs[0]) + "."
        if len(q) > 5:
            text += f" And {len(q) - 5} more after that."
        return text
    if k == "current":
        if not r.get("track"):
            return "Spotify isn't playing anything right now."
        state = "Playing" if r.get("is_playing") else "Paused on"
        return f"{state} {r['track']} by {r['artists']}."
    if k == "pause":
        return "Paused."
    if k == "resume":
        return "Resuming."
    if k == "next":
        return "Skipped." if not r.get("track") else f"Skipped. Now playing {r['track']}."
    if k == "next_reject":
        return "Got it, skipping that one — I'll play less like it."
    if k == "current_artist":
        return f"That's {r['artists']}." if r.get("track") else "Spotify isn't playing anything right now."
    if k == "current_album":
        return (f"{r['track']} is from {r['album']}." if r.get("album") else f"That's {r['track']}.") \
            if r.get("track") else "Spotify isn't playing anything right now."
    if k == "replay":
        return f"Starting {r['track']} again." if r.get("track") else "Starting it again."
    if k in ("seek", "seek_rel"):
        mm, ss = divmod(int(r.get("position_s", 0)), 60)
        return f"Jumped to {mm}:{ss:02d}."
    if k == "smart_shuffle":
        if r.get("fallback"):
            return ("Smart Shuffle is only available in the Spotify app, which isn't open, so I turned on "
                    "regular shuffle instead." if r.get("shuffle") else "Shuffle off.")
        if r.get("unsupported"):
            return ("Spotify only does Smart Shuffle on a playlist or your Liked Songs. "
                    + ("I'm already queueing songs like this one." if r.get("autoqueue") else
                       "Say “queue more songs like this” and I'll keep similar songs coming."))
        if not r.get("verified", True):
            return "I tried to switch Smart Shuffle, but Spotify didn't confirm the change."
        return "Smart Shuffle is on." if r.get("smart") else "Smart Shuffle is off."
    if k == "search":
        tracks = (r.get("tracks") or {}).get("items") or []
        tracks = [x for x in tracks if x]
        if not tracks:
            return "I didn't find anything for that on Spotify."
        top = ", ".join(f"{x['name']} by {', '.join(a['name'] for a in x.get('artists', [])[:1])}" for x in tracks[:3])
        return f"On Spotify I found {top}."
    if k == "previous":
        return "Going back."
    if k in ("volume_set", "volume_up", "volume_down"):
        return f"Volume {r['percent']}%."
    if k == "shuffle":
        return "Shuffle on." if r["state"] else "Shuffle off."
    if k == "repeat":
        return {"off": "Repeat off.", "track": "Repeating this track.", "context": "Repeat on."}[r["state"]]
    if k == "like":
        return f"Noted — you like {r['track']} by {r['artist']}. I'll factor that in."
    if k == "dislike":
        return f"Noted — I'll steer away from songs like {r['track']}."
    if k == "add_to_playlist":
        return f"Added {r['track']} by {r['artist']} to {r['playlist']}."
    if k == "queue":
        return f"Queued {r['name']}" + (f" by {r['artist']}." if r.get("artist") else ".")
    if k == "queue_similar":
        first = r["names"][0] if r.get("names") else ""
        return (f"Queued {r['count']} songs like {r['seed']}" + (f", starting with {first}" if first else "")
                + ". I'll keep adding more as they play.")
    if k == "stop_autoqueue":
        return "Okay, I'll stop adding songs." if r.get("stopped") else "I wasn't adding songs to the queue."
    if k == "mood":
        mood = r.get("mood") or "your"
        return (f"Here's a {mood} queue from your own listening: {r['name']} by {r['artist']}, "
                f"with {r['count'] - 1} more lined up. I'll keep it going.")
    if k == "liked":
        return f"Playing your liked songs ({r['count']} tracks, shuffled)."
    if k == "similar_seed":
        return f"Playing {r['name']} by {r['artist']}, then {r['count'] - 1} more — {r.get('basis', '')}."
    if k in ("play_for_me", "similar"):
        basis = f" Picked from {r['basis']}." if r.get("basis") else ""
        return f"Playing {r['name']} by {r['artist']}, then {r['count'] - 1} more.{basis}"
    if k == "dj":
        return f"Switching it up: {r['name']} by {r['artist']}, then {r['count'] - 1} more."
    if k == "ban":
        return f"Got it — no more {r['banned']}."
    if k == "ban_playlist":
        return f"Okay, I won't play {r['banned']} again."
    if k == "unban":
        return f"{r['unbanned']} can come back in the mix." if r.get("was_banned") else \
            f"{r['unbanned']} wasn't blocked."
    if k == "history":
        if not r["count"]:
            return ("I haven't recorded any listening today yet." if r["period"] == "today"
                    else "I don't have listening history for that period yet.")
        tracks = r["tracks"]
        artists = [a["artist"] for a in r["top_artists"][:3]]
        when = "Today" if r["period"] == "today" else "Lately"
        s = f"{when} you've played {r['count']} tracks"
        if artists:
            s += ", mostly " + ", ".join(artists)
        s += f". Most recently {tracks[0]['track']} by {tracks[0]['artist']}."
        return s
    if k == "taste":
        top = r["top_artists"] or r["spotify_top_artists"]
        if not top:
            return "I don't have enough listening history to describe your taste yet."
        s = "You listen to " + ", ".join(top[:4]) + " the most"
        if r["top_genres"]:
            s += ", mostly " + ", ".join(r["top_genres"][:3])
        return s + "."
    if k == "recommend":
        recs = r["recommendations"]
        if not recs:
            return "I don't have enough listening history yet to recommend something."
        return f"I'd suggest {recs[0]['name']} by {recs[0]['artists']}."
    # play_*
    kind = r.get("kind")
    if kind == "artist":
        return f"Playing {r['name']}."
    if kind == "playlist":
        owner = "" if r.get("owned") else " (a public playlist)"
        return f"Playing the {r['name']} playlist{owner}."
    if kind == "album":
        return f"Playing the album {r['name']} by {r['artist']}."
    if kind == "genre":
        return f"Playing {r['genre']} — the {r['name']} playlist." if r.get("name") and "tracks" not in r["name"] \
            else f"Playing some {r['genre']}, starting with {r.get('first')}."
    if r.get("radio"):
        return f"Playing {r['name']} by {r['artist']}, and I'll queue up more like it."
    return f"Playing {r['name']} by {r['artist']}."


def _ask_playlist(result) -> Reply:
    """"Play my playlist" with no name: offer the user's own playlists."""
    names = [p["name"] for p in (result or {}).get("playlists", []) if p.get("name")][:5]
    if not names:
        return Reply("I couldn't find any playlists of yours on Spotify.", ok=False)
    if len(names) == 1:
        res = call("spotify.play_query", query=names[0], kind="playlist", own_only=True)
        return Reply(f"Playing {names[0]}." if res.success else res.error, ok=res.success)
    from modules.agent.confirm import ChoiceOption, PendingChoice, choices

    def play(name):
        res = call("spotify.play_query", query=name, kind="playlist", own_only=True)
        return f"Playing {name}." if res.success else res.error
    question = f"Which playlist? {', '.join(names[:-1])} or {names[-1]}?"
    choices.ask(PendingChoice(question, [ChoiceOption(n, n, n) for n in names], play))
    return Reply(question, expects_reply=True)


def parse_spotify(text: str) -> Optional[Intent]:
    si = spotify_intent(text)
    if si is None:
        return None

    def run():
        from core.module_manager import module_manager
        sp = module_manager.get("spotify")
        if sp is None:
            return Reply("Spotify support isn't installed.", ok=False)
        ok, reason = sp.availability()
        if not ok:
            return Reply(reason, ok=False)
        from modules.agent.context import desktop_context
        desktop_context.note_domain("spotify")
        if si.kind == "queue_remove":
            return Reply("Spotify doesn't let apps remove songs from the queue — you can do that in the "
                         "Spotify app. I can skip the current song or queue something else.", ok=False)
        if si.kind == "next_reject":
            call("spotify.feedback", signal=-0.7, reason="user rejected the track")
        res = call(si.tool, **si.kwargs)
        if not res.success and si.kind in ("next", "next_reject") and res.error_code in (
                "NO_PLAYBACK", "NO_ACTIVE_DEVICE", "NO_DEVICE"):
            # Nothing to skip: "play something different" means play something.
            res = call("spotify.play_recommended")
            if res.success:
                return Reply(f"Nothing was playing, so I put on {res.result['name']} by {res.result['artist']}.")
        if si.kind in ("next", "next_reject") and res.success:
            time.sleep(0.8)                    # verify: what's playing now?
            cur = call("spotify.current")
            if cur.success and cur.result.get("track"):
                res.result = dict(res.result or {}, track=f"{cur.result['track']} by {cur.result['artists']}")
        if not res.success:
            if res.error_code == "CONFIRM_REQUIRED":
                return run_tool(si.tool, si.kind.replace("_", " "), lambda r: _spotify_reply(si, r), **si.kwargs)
            if si.kind in ("pause", "resume"):
                # Spotify rejects pausing what's already paused (and vice versa):
                # say what the state actually is.
                cur = call("spotify.current")
                if cur.success and si.kind == "pause" and not cur.result.get("is_playing"):
                    return Reply("Spotify is already paused.")
                if cur.success and si.kind == "resume" and cur.result.get("is_playing"):
                    return Reply("It's already playing.")
            if res.error_code == "NOT_MINE":
                # Offer the public playlist instead of silently playing a stranger's.
                public = dict(si.kwargs, own_only=False)

                def play_public():
                    r2 = call(si.tool, **public)
                    return _spotify_reply(si, r2.result) if r2.success else (r2.error or "I couldn't find one.")
                confirmations.ask(PendingAction(f"play a public {si.kwargs['query']} playlist", play_public,
                                                tool=si.tool))
                return Reply(f"{res.error} Want me to play a public one? If yours has another name, say "
                             f"“no, I meant my … playlist” and I'll remember it.", ok=True, expects_reply=True)
            return Reply(res.error, ok=False)
        if si.kind == "choose_playlist":
            return _ask_playlist(res.result)
        reply = Reply(_spotify_reply(si, res.result))
        if si.kind == "search":
            items = [x for x in ((res.result.get("tracks") or {}).get("items") or []) if x]
            if items:
                first = items[0]
                from modules.agent.confirm import ChoiceOption, PendingChoice, choices

                def play_item(item):
                    choices.clear()
                    r2 = call("spotify.play", uri=item["uri"])
                    return f"Playing {item['name']}." if r2.success else r2.error

                def play_found():
                    return play_item(first)
                confirmations.ask(PendingAction(f"play {first['name']}", play_found, tool="spotify.play"))
                # "Yes" plays the first; "the second one" / "play the third song" picks from the list.
                choices.ask(PendingChoice(reply.text, [
                    ChoiceOption(x.get("name", ""), " ".join([x.get("name", "")] + [
                        a.get("name", "") if isinstance(a, dict) else str(a) for a in (x.get("artists") or [])]
                        + [str(x.get("artist") or "")]), x) for x in items[:5]], play_item))
                reply.text += " Want me to play the first one?"
                reply.expects_reply = True
        if si.kind == "recommend" and res.result["recommendations"]:
            uris = [x["uri"] for x in res.result["recommendations"]]
            first = res.result["recommendations"][0]

            def play_it():
                r2 = call("spotify.play", uri=uris[0])
                return f"Playing {first['name']}." if r2.success else r2.error
            confirmations.ask(PendingAction(f"play {first['name']}", play_it, tool="spotify.play"))
            reply.text += " Want me to play it?"
            reply.expects_reply = True
        return reply
    return Intent(f"spotify.{si.kind}", run, "spotify")


# ====================================================================== #
# Desktop / screen
# ====================================================================== #
_BROWSERS = ("chrome", "msedge", "firefox", "brave", "opera", "vivaldi")


def parse_desktop(text: str) -> Optional[Intent]:
    raw = _clean(text)
    t = raw.lower()

    # screenshot / screen understanding
    if re.search(r"^(take|grab|capture) (a )?(screenshot|screen ?shot|screen capture)", t):
        return Intent("screen.capture", lambda: run_tool(
            "screen.capture", "take a screenshot",
            lambda r: f"Screenshot saved ({r['width']} by {r['height']})."), "desktop")
    if re.search(r"^(what(?:'s| is) on (my|the) screen|what am i looking at|what(?:'s| is) this window|"
                 r"describe (my|the) screen|what do you see)", t):
        return Intent("screen.context", _describe_screen, "desktop")
    if re.search(r"^(read (out )?(what(?:'s| is) on )?(my|the|this) (screen|window|page)|read (it|this) (to|for) me|"
                 r"what does (it|the screen|this|this page) say)", t):
        return Intent("screen.read", _read_screen, "desktop")
    m = re.match(r"^where(?:'s| is| are)\s+(?:the\s+)?(.+?)(?:\s+on (?:my|the) screen)?$", t)
    if m and re.search(r"\b(box|bar|button|field|link|tab|menu|icon|search|input|toggle|checkbox)\b", m.group(1)):
        what = m.group(1)
        return Intent("screen.locate", lambda: _locate_on_screen(what), "desktop")
    if re.search(r"^(what|which) (button|thing|option|link) (should|do) i (click|press|choose|pick)|^what should i click", t):
        return Intent("screen.buttons", _suggest_buttons, "desktop")
    if re.search(r"^(what|which) (windows|apps|applications|programs) (are|do i have) open", t):
        def run_list():
            res = call("desktop.list_windows")
            if not res.success:
                return Reply(res.error, ok=False)
            wins = [w for w in res.result["windows"] if w["title"] != "SAINT"]
            names = []
            for w in wins:
                n = w["title"].split(" - ")[-1].strip() or w["process"]
                if n not in names:
                    names.append(n)
            return Reply(f"You have {len(wins)} windows open: " + ", ".join(names[:8]) + ".")
        return Intent("desktop.list_windows", run_list, "desktop")

    # web search
    m = re.match(r"^(?:search|google|look up)(?: the web| google| online)?(?: for)? (.+?)(?: on (?:google|the web))?$", t)
    if m and not re.search(r"\b(spotify|song|playlist)\b", t):
        query = raw[m.start(1):m.end(1)]
        return Intent("desktop.web_search", lambda: _web_search(query), "desktop")

    # open / launch
    m = re.match(r"^(?:open|launch|start|run|fire up|boot up)\s+(?:up\s+)?(?:the\s+|my\s+)?(.+?)(?:\s+app(?:lication)?)?$", t)
    if m and not re.search(r"\b(timer|reminder|playlist|song|music)\b", t):
        name = m.group(1)
        if name in ("it", "that", "this") or not _looks_like_app_name(name) or \
                re.match(r"^(?:that|this|those|these)\s+(?:folder|file|screenshot|picture|download|archive|one)s?\b",
                         name) or \
                re.search(r"\byou (?:just )?(?:made|created|extracted|took|moved|downloaded|found)\b", name):
            return None      # "open that folder" is about something SAINT did (refer_intents)

        def ok(r):
            if r.get("reused"):
                from modules.vision.screen import app_label
                return f"Switched to {app_label(r)}" + (
                    f" (you have {r['count']} of its windows open)." if r.get("count", 1) > 1 else ".")
            if r.get("window"):
                return f"Opened {r['app']}."
            return f"Launched {r['app']}; its window hasn't appeared yet."

        def run_open():
            reply = run_tool("desktop.open_app", f"open {name}", ok, name=name)
            many = re.search(r"Did you mean (.+,.+)\?$", reply.text) if not reply.ok else None
            if many:
                # "Did you mean Disk Cleanup, Windows Backup, Windows Security?" — "the first one" opens it.
                from modules.agent.confirm import ChoiceOption, PendingChoice, choices
                names = [n.strip() for n in re.split(r",\s*|\s+or\s+", many.group(1)) if n.strip()]
                choices.ask(PendingChoice(reply.text, [ChoiceOption(n, n, n) for n in names],
                                          lambda other: run_tool("desktop.open_app", f"open {other}", ok,
                                                                 name=other).text))
                reply.expects_reply = True
                return reply
            guess = re.search(r"Did you mean ([^,?]+)\?$", reply.text) if not reply.ok else None
            if guess:
                # "Did you mean Opera Browser?" is a question: "yes" opens it.
                other = guess.group(1).strip()
                confirmations.ask(PendingAction(
                    f"open {other}", lambda: run_tool("desktop.open_app", f"open {other}", ok, name=other).text,
                    tool="desktop.open_app"))
                reply.expects_reply = True
            return reply
        return Intent("desktop.open_app", run_open, "desktop")

    # close
    m = re.match(r"^(?:close|quit|exit|shut down|kill)\s+(?:the\s+|my\s+)?(.+?)(?:\s+(?:app|application|window))?$", t)
    if m and not re.search(r"\b(reminder|timer|alarm)\b", t):
        name = "saint" if m.group(1) in ("yourself", "you") else m.group(1)
        return Intent("desktop.close_app", lambda: close_window(name), "desktop")

    # go to a website ("go to YouTube", "navigate to github.com")
    m = re.match(r"^(?:go to|navigate to|visit|browse to)\s+(?:the\s+)?(?:website\s+)?(.+?)(?:\s+(?:website|site|page))?"
                 r"(?:\s+in (?:my|the) browser)?$", t)
    if m:
        url = _site_url(m.group(1))
        if url:
            return Intent("desktop.open_site", lambda: _open_site(url), "desktop")

    # snap to a side ("move the window to the left side of the screen")
    m = re.match(r"^(?:move|snap|put|push|drag|send)\s+(?:the\s+|this\s+|my\s+)?(.*?)\s*(?:window\s+)?(?:to|over to|on)\s+the\s+"
                 r"(left|right)(?:\s+(?:side|half))?(?:\s+of\s+(?:the|my)\s+(?:screen|monitor|display))?$", t)
    if m:
        window = m.group(1).strip() or "this"
        window = "this" if window in ("this", "it", "that", "window", "this window", "the window") else window
        side = m.group(2)

        def run_snap():
            return run_tool("desktop.arrange_window", f"snap {window} {side}",
                            lambda r: f"Moved {_app_label(r)} to the {side} side.",
                            window=window, action=f"snap_{side}")
        return Intent("desktop.arrange_window", run_snap, "desktop")

    # move to monitor
    m = re.match(r"^(?:move|send|put|throw)\s+(?:the\s+|my\s+)?(.+?)\s+(?:window\s+)?(?:to|onto|on)\s+(?:the\s+|my\s+)?"
                 r"(?:(first|second|third|other|next|left|right|main|primary|1|2|3|4|one|two|three)\s+)?(?:monitor|screen|display)"
                 r"(?:\s+(\d))?$", t)
    if m:
        window = m.group(1)
        window = "this" if window in ("this", "it", "this window", "that", "that window", "the window", "window") else window
        which = m.group(3) or m.group(2) or "next"

        def run_move():
            return run_tool("desktop.move_window", f"move {window} to monitor {which}",
                            lambda r: f"Moved {_app_label(r)} to monitor {r['monitor']}.",
                            window=window, monitor=which)
        return Intent("desktop.move_window", run_move, "desktop")

    # minimize everything ("minimize all my windows", "minimize the screen",
    # "minimize my browser and everything else")
    if re.match(r"^(?:minimi[sz]e|hide)\s+(?:all|everything|every window|it all|all of (?:it|them)|"
                r"(?:all\s+(?:of\s+)?)?(?:my|the)\s+(?:windows|apps|programs|stuff|screens?|desktop)|"
                r"all\s+(?:the\s+)?(?:windows|apps|programs))(?:\s+(?:on|off)\s+(?:my|the)\s+(?:screens?|desktop))?$", t) \
            or re.match(r"^minimi[sz]e\s+.+?\s+and\s+(?:everything|all)(?:\s+else)?$", t):
        return Intent("desktop.minimize_all", lambda: run_tool(
            "desktop.minimize_all", "minimize your windows",
            lambda r: "Minimized everything." if r.get("minimized") else "Everything was already minimized."),
            "desktop")

    # arrange
    m = re.match(r"^(maximi[sz]e|minimi[sz]e|restore|center|centre)\s+(?:the\s+)?(.+?)(?:\s+window)?$", t) or \
        re.match(r"^snap\s+(?:the\s+)?(.+?)(?:\s+window)?\s+(?:to\s+)?(?:the\s+)?(left|right)$", t)
    if m:
        if t.startswith("snap"):
            window, action = m.group(1), f"snap_{m.group(2)}"
        else:
            verb = m.group(1)
            action = {"maxim": "maximize", "minim": "minimize", "resto": "restore", "cente": "center",
                      "centr": "center"}[verb[:5]]
            window = m.group(2)
        window = "this" if window in ("this", "it", "that", "this window", "window") else window

        def run_arrange():
            return run_tool("desktop.arrange_window", f"{action.replace('_', ' ')} {window}",
                            lambda r: {"maximize": "Maximized.", "minimize": "Minimized.", "restore": "Restored.",
                                       "center": "Centered."}.get(action, f"Snapped {action[5:]}."),
                            window=window, action=action)
        return Intent("desktop.arrange_window", run_arrange, "desktop")

    # switch / focus
    m = re.match(r"^(?:switch to|go to|focus(?: on)?|bring up|show me|pull up|jump to|bring)\s+(?:the\s+|my\s+)?(.+?)"
                 r"(?:\s+(?:window|app))?(?:\s+to the front)?$", t)
    if m and not re.search(r"\b(reminders?|memories|playlist|settings page|next song|previous)\b", t):
        name = m.group(1)
        monitor = None
        mm = re.search(r"\s+(?:on|in)\s+(?:my|the)\s+(second|other|2nd|left|right|main|primary|first|1st|third|"
                       r"3rd|1|2|3|secondary|laptop)\s+(?:screen|monitor|display)$", name)
        if mm:
            monitor, name = mm.group(1), name[:mm.start()].strip()
            name = re.sub(r"\s+(?:window|app)$", "", name) or "this"
        # A whole request ("downloads folder, click the first download, and
        # extract it ...") is never a window name.
        if not _looks_like_app_name(name):
            return None

        def run_focus():
            kwargs = {"name": name, **({"monitor": monitor} if monitor else {})}
            res = call("desktop.focus_window", **kwargs)
            if res.success:
                return Reply(f"Switched to {_app_label(res.result)}.")
            if res.error_code == "NOT_FOUND" and not monitor and _foreground_is_browser():
                # "Go to Astral Games" on a web page means the link, not a window.
                r2 = call("desktop.click_element", name=name)
                if r2.success:
                    return Reply(f"Clicked {r2.result.get('clicked', name)}.")
            return Reply(res.error or f"I couldn't switch to {name}.", ok=False)
        return Intent("desktop.focus_window", run_focus, "desktop")

    # type
    m = re.match(r"^(?:type|write|enter)\s+(?:out\s+)?(.+?)(?:\s+(?:in|into|in the|into the|on)\s+(?:the\s+)?(.+?))?"
                 r"(?:\s+and (?:press|hit) enter)?$", raw, re.I)
    if m and t.split()[0] in ("type", "write", "enter"):
        text_to_type = m.group(1).strip().strip('"“”')
        target = m.group(2)
        enter = bool(re.search(r"and (press|hit) enter$", t))
        if target and target.lower() in ("it", "this", "here"):
            target = None
        if target and (len(target.split()) > 4 or re.search(r"[?]|\b(?:i|me|my room|how|what|why|would|could|"
                                                            r"should|through|because|about|doing)\b", target, re.I)):
            # "type I want to turn off the lights in my room through my PC, how would I..." is
            # one sentence to type, not text for a box called "my room through my PC..." (2026-09-29).
            text_to_type = re.sub(r"\s+and (?:press|hit) enter$", "", raw[m.start(1):], flags=re.I).strip().strip('"“”')
            target = None
        kwargs = {"text": text_to_type, "press_enter": enter}
        if target:
            kwargs["target"] = target
        where = f" into the {target}" if target else ""

        def run_type():
            return run_tool("desktop.type_text", f"type that{where}",
                            lambda r: f"Typed it{where}." + (" Pressed Enter." if enter else ""), **kwargs)
        return Intent("desktop.type_text", run_type, "desktop")

    # press keys
    m = re.match(r"^(?:press|hit|tap)\s+(?:the\s+)?(.+?)(?:\s+key)?$", t)
    if m:
        # "Press F to full screen" / "hit space so it pauses": the purpose isn't a key.
        keys = re.sub(r"\s+(?:key\s+)?(?:to|so(?: that| it)?|in order to|for|then|and then)\s+.+$", "", m.group(1))
        keys = re.sub(r"\s+key$", "", keys).replace(" plus ", "+").replace(" and ", "+")
        if not _is_key_combo(keys):
            return None

        def run_press():
            return run_tool("desktop.press_keys", f"press {keys}", lambda r: f"Pressed {r['keys']}.", keys=keys)
        return Intent("desktop.press_keys", run_press, "desktop")

    # click an element
    m = re.match(r"^click(?: on)?\s+(?:the\s+)?(.+?)(?:\s+(?:button|link|tab))?$", t)
    if m:
        name = m.group(1)

        def run_click():
            from modules.agent.desktop_intents import _click
            return _click(name)
        return Intent("desktop.click_element", run_click, "desktop")
    return None


def close_window(name: str) -> Reply:
    """Close a window the user named. The real window is found *before*
    asking, so the question names it ("Close THE FINALS?") and "yes" can't
    fail on a misheard name; a game that ignores the close request is
    offered a force quit."""
    from modules.vision.screen import app_label
    deictic = name in ("this", "it", "that", "this window", "that window", "the window", "this app", "that app",
                       "this one", "that one")
    target, label = name, "this window" if deictic else name
    if deictic:
        # Name the window "that" means before asking: the one SAINT just opened
        # or worked in, else the one in front ("Close Disk Cleanup?").
        try:
            from modules.desktop.controller import desktop
            from modules.agent.context import desktop_context
            hwnd = desktop_context.window(max_age=120)
            w = next((x for x in desktop.list_windows() if x.hwnd == hwnd), None) if hwnd else None
            w = w or desktop.target_window()
            if w is not None and not desktop._is_own(w):
                label = app_label({"title": w.title, "process": w.process}) or "this window"
                target = f"hwnd:{w.hwnd}"
        except Exception:
            pass
    else:
        try:
            from modules.desktop.controller import desktop
            w = desktop.find_for_close(name)
            label = "SAINT" if desktop._is_own(w) else app_label({"title": w.title, "process": w.process}) or name
            target = f"hwnd:{w.hwnd}"
        except Exception as e:
            if getattr(e, "code", "") in ("NOT_FOUND", "NO_WINDOW"):
                return Reply(str(e), ok=False)
            # Ambiguous or unexpected: let the tool ask / report it.

    def ok(r):
        if r.get("closed"):
            return f"Closed {label if label != 'this window' else app_label(r) or 'it'}."
        confirmations.ask(PendingAction(f"force {label} to quit", lambda: run_tool(
            "desktop.force_quit", f"force {label} to quit", lambda r2: f"Forced {label} to quit.",
            _confirmed=True, name=target).text, tool="desktop.force_quit"))
        return f"I asked {label} to close, but it's still open. Want me to force it to quit?"
    reply = run_tool("desktop.close_app", f"close {label}", ok, name=target)
    if confirmations.pending is not None and confirmations.pending.tool == "desktop.force_quit":
        reply.expects_reply = True
    return reply


def _app_label(r) -> str:
    from modules.vision.screen import app_label
    return app_label(r)


def _foreground_is_browser() -> bool:
    try:
        from modules.desktop.controller import desktop
        w = desktop.target_window()
        return bool(w and desktop.is_browser(w))
    except Exception:
        return False


_KEY_NAMES = {"enter", "return", "space", "spacebar", "tab", "esc", "escape", "backspace", "delete", "del", "home",
              "end", "pageup", "pagedown", "page up", "page down", "up", "down", "left", "right", "insert",
              "ctrl", "control", "alt", "shift", "win", "windows", "cmd", "super", "capslock", "caps lock",
              "printscreen", "print screen", "prtsc", "volumeup", "volumedown", "volumemute", "playpause",
              "nexttrack", "prevtrack", "menu", "apps", "up arrow", "down arrow", "left arrow", "right arrow",
              "numlock", "scrolllock", "pause"}


def _is_key_combo(keys: str) -> bool:
    """'f', 'ctrl+shift+t', 'alt f4', 'space' — not 'f to full screen'."""
    parts = [p.strip() for p in re.split(r"\s*\+\s*|\s+(?=\S)", keys.strip()) if p.strip()]
    if not parts or len(parts) > 4:
        return False
    joined = keys.strip().lower()
    if joined in _KEY_NAMES:
        return True
    for p in parts:
        if len(p) == 1 or p in _KEY_NAMES or re.fullmatch(r"f(?:[1-9]|1[0-9]|2[0-4])", p) or \
                re.fullmatch(r"num(?:pad)?\s?\d", p):
            continue
        return False
    return True


_COMMAND_VERB = re.compile(r"\b(?:search|click|play|type|press|scroll|turn|pause|go to|navigate|close|find|"
                           r"minimi[sz]e|maximi[sz]e|move|put|make|tabs?)\b")


def _looks_like_app_name(name: str) -> bool:
    """'discord', 'task manager', 'visual studio code' — not 'browser search
    youtube for x, click the first video' (a whole request, which the
    multi-step splitter or the LLM should handle)."""
    return len(name.split()) <= 4 and "," not in name and not _COMMAND_VERB.search(name)


def _describe_screen() -> Reply:
    """'What's on my screen?' / 'What am I looking at?' — the window the user is
    looking at, what else is open, and its controls/text (desktop_intents)."""
    from modules.agent.desktop_intents import _describe
    return _describe()


def _site_url(name: str) -> Optional[str]:
    # Delegate to the canonical site table in modules.desktop.browser so aliases
    # (youtube, google, etc.) stay defined in one place.
    from modules.desktop.browser import site_url
    return site_url(name)


def _browser_target():
    """The browser window the user is using (never SAINT's own), brought to the front."""
    try:
        from modules.desktop.controller import desktop
        w = desktop.target_window()
        if w and any(b in w.process.lower() for b in _BROWSERS):
            return desktop.target_window(activate=True)
    except Exception:
        pass
    return None


def _open_site(url: str) -> Reply:
    import os
    reg = get_tool_registry()
    site = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    if _browser_target():
        r1 = reg.execute("desktop.press_keys", keys="ctrl+l")
        if r1.success:
            time.sleep(0.1)
            r2 = reg.execute("desktop.type_text", text=url, press_enter=True)
            if r2.success:
                return Reply(f"Opening {site}.")
            return Reply(r2.error, ok=False)
        return Reply(r1.error, ok=False)
    try:
        os.startfile(url)  # type: ignore[attr-defined]
    except Exception as e:
        return Reply(f"I couldn't open your browser: {e}", ok=False)
    event_bus.emit_event(EventType.DESKTOP_ACTION, {"action": "open_site", "url": url})
    return Reply(f"Opening {site}.")


def _read_screen() -> Reply:
    res = call("screen.read")
    if not res.success:
        return Reply(res.error, ok=False)
    r = res.result
    lines = r.get("text") or []
    name = (r.get("window") or "that window").split(" - ")[-1]
    if not lines:
        return Reply(f"I can't read any text in {name}: it doesn't expose its text to Windows, "
                     "and no vision model is set up to read the pixels.", ok=False)
    return Reply(f"In {name}: " + ". ".join(lines[:12]) + ".")


def _locate_on_screen(what: str) -> Reply:
    res = call("screen.locate", name=what)
    if not res.success:
        return Reply(res.error, ok=False)
    r = res.result
    return Reply(f"The {r['name'] or what} is at the {r['where']} of {r['window'].split(' - ')[-1]}, "
                 f"around x {r['x']}, y {r['y']}.")


def _suggest_buttons() -> Reply:
    res = call("screen.context")
    if not res.success:
        return Reply(res.error, ok=False)
    ctx = res.result
    aw = ((ctx.get("active_window") or {}).get("title") or "that window").split(" - ")[-1]
    btns = []
    for e in ctx.get("active_window_elements", []):
        if e.get("type") in ("Button", "SplitButton", "Hyperlink", "MenuItem", "TabItem") and e.get("name") \
                and e["name"] not in btns:
            btns.append(e["name"])
    if not btns:
        return Reply(f"I can't see any labelled buttons in {aw}.", ok=False)
    return Reply(f"In {aw} I can see: " + ", ".join(btns[:10]) +
                 ". Tell me what you're trying to do and I'll say which one, or ask me to click it.",
                 expects_reply=True)


def _web_search(query: str) -> Reply:
    import urllib.parse
    reg = get_tool_registry()
    if _browser_target():
        r1 = reg.execute("desktop.press_keys", keys="ctrl+l")
        if r1.success:
            r2 = reg.execute("desktop.type_text", text=query, press_enter=True)
            if r2.success:
                return Reply(f"Searching for {query}.")
            return Reply(r2.error, ok=False)
        return Reply(r1.error, ok=False)
    import os
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
    try:
        os.startfile(url)  # type: ignore[attr-defined]
    except Exception as e:
        return Reply(f"I couldn't open your browser: {e}", ok=False)
    event_bus.emit_event(EventType.DESKTOP_ACTION, {"action": "web_search", "query": query})
    return Reply(f"Searching the web for {query}.")


# ====================================================================== #
# Routing
# ====================================================================== #
def parse_youtube(text: str) -> Optional[Intent]:
    """YouTube player control ("theater mode", "1.5x speed", "skip ahead 30
    seconds", "captions on", "set quality to 1080p"). Bare commands ("faster",
    "mute", "pause") only mean the video while a YouTube tab is in front."""
    from modules.desktop import youtube
    t = _clean(text).lower()
    if not t or len(t.split()) > 12:
        return None
    parsed = youtube.parse(t)
    if parsed is None and len(t.split()) <= 7 and youtube.is_watching():
        parsed = youtube.parse(t, youtube_context=True)
    if parsed is None:
        return None
    action, value = parsed

    def run():
        from modules.agent.desktop_intents import _tool
        return _tool("browser.youtube", f"do that on YouTube", lambda r: r.get("said") or "Done.",
                     retry=run, action=action, **({"value": str(value)} if value is not None else {}))
    return Intent(f"youtube.{action}", run, "browser")


def parse_desktop_nl(text: str) -> Optional[Intent]:
    """Generalised desktop / screen / browser understanding (desktop_intents)."""
    from modules.agent.desktop_intents import parse
    return parse(text)


# --- Current-information / web questions -------------------------------------
# "What's happening in the news?" is a factual question, not a browser command.
# When no web provider is configured SAINT answers honestly instead of opening
# a browser tab the user didn't ask for. "Search Google for X" / "Open YouTube"
# still fall through to the desktop parser, which does automate the browser.
_CURRENT_INFO = re.compile(
    r"^(?:whats?(?:'s| is| are)?\s+(?:the\s+)?(?:latest|newest|current|recent|today'?s?)\s+"
    r"(?:news|headlines|updates?|happening|going on)"
    r"|whats?(?:'s| is)?\s+happening\s+(?:in the world|today|right now|now)"
    r"|what\s+happened\s+(?:today|yesterday|this week|with|to)\b"
    r"|(?:what|who|when|where)\s+(?:is|are|was|were)\s+the\s+(?:latest|current|newest)\b"
    r"|(?:latest|any)\s+news\s+(?:about|on|for|regarding)\b"
    r"|whats?(?:'s| is)?\s+the\s+(?:weather|forecast|temperature)\b"
    r"|who\s+won\s+(?:the\s+)?\b"
    r"|(?:what|any)\s+(?:updates?|news)\s+on\b"
    r"|(?:(?:use|using|with|on)\s+(?:duckduckgo|the web|google)\s+(?:and|to)\s+)?(?:find|get|tell me|give me|look up)"
    r"\s+(?:the\s+|me\s+(?:the\s+)?)?(?:latest|newest|recent|current)\s+(?:news|headlines|updates?)\b)")


def parse_web(text: str) -> Optional[Intent]:
    t = _lower(text)
    if "search" in t or "google" in t or "youtube" in t or "on my browser" in t or "in my browser" in t:
        return None                       # explicit browser action — desktop parser handles it
    if not _CURRENT_INFO.search(t):
        return None
    query = _clean(text)

    def run_web():
        provider = (config.get("web.provider", "duckduckgo_browser") or "duckduckgo_browser").lower()
        # Default: open DuckDuckGo in the user's existing browser. Cheap,
        # keyless, and the user can read/skim the results themselves.
        if provider in ("duckduckgo_browser", "browser"):
            import urllib.parse
            url = "https://duckduckgo.com/?q=" + urllib.parse.quote_plus(query)
            reply = _open_site(url)
            if reply.ok:
                reply.text = f"Looking up '{query}' on DuckDuckGo."
            return reply
        if provider == "none":
            return Reply("Web search is turned off in Settings > Web.", ok=False)
        # API-backed providers (duckduckgo instant-answer API, tavily, serpapi).
        try:
            from modules.web.search import answer_current
        except Exception as e:
            return Reply(f"Web search isn't wired up yet ({e}).", ok=False)
        try:
            answer = answer_current(query)
        except Exception as e:
            log.warning("web.search_failed %s", e)
            return Reply("I couldn't reach the web just now.", ok=False)
        if not answer:
            name = {"duckduckgo": "DuckDuckGo", "tavily": "Tavily", "serpapi": "Google"}.get(provider, "The web search")
            return Reply(f"{name} didn't return anything for that.", ok=False)
        return Reply(answer)
    return Intent("web.current", run_web, "web")


def parse_saint_ui(text: str) -> Optional[Intent]:
    from modules.agent.saint_intents import parse_saint_ui as parse
    return parse(text)


def parse_steam(text: str) -> Optional[Intent]:
    from modules.agent.steam_intents import parse_steam as parse
    return parse(text)


def parse_files_task(text: str) -> Optional[Intent]:
    from modules.agent.files_intents import parse_files_task as parse
    return parse(text)


def parse_files(text: str) -> Optional[Intent]:
    from modules.agent.files_intents import parse_files as parse
    return parse(text)


def parse_winctl(text: str) -> Optional[Intent]:
    from modules.agent.winctl_intents import parse_winctl as parse
    return parse(text)


def parse_social(text: str) -> Optional[Intent]:
    from modules.agent.social_intents import parse_social as parse
    return parse(text)


def parse_taskmgr(text: str) -> Optional[Intent]:
    from modules.agent.taskmgr_intents import parse_taskmgr as parse
    return parse(text)


def parse_extras(text: str) -> Optional[Intent]:
    from modules.agent.extras_intents import parse_extras as parse
    return parse(text)


_SINGLE_PARSERS = [parse_system, parse_saint_ui, parse_web, parse_youtube, parse_steam, parse_files, parse_taskmgr,
                   parse_winctl, parse_spotify, parse_extras, parse_desktop_nl, parse_desktop]


_OPEN_PATH = re.compile(r"^(?:(?:hey\s+)?saint[,.!\s]+)?(?:open|launch|start|run)\s+(?:(?P<label>.+?)\s+"
                        r"(?:at|from|in|with)\s+)?[\"'“]?(?P<path>[a-z]:[\\/][^\"'”]+?)[\"'”]?[.!]?$", re.I)


def parse_open_path(text: str) -> Optional[Intent]:
    """'Open Bloxstrap at "C:\\Users\\me\\Downloads\\Bloxstrap.exe"' starts that
    exact file (a scene step typed by the user, 2026-09-30) — it used to fail
    and get "worked out" into a Steam search."""
    m = _OPEN_PATH.match((text or "").strip())
    if not m:
        return None
    import os
    path = m.group("path").strip()
    label = (m.group("label") or "").strip() or os.path.basename(path)

    def run():
        if not os.path.exists(path):
            return Reply(f"I can't find {path} — has it been moved or deleted?", ok=False)

        def ok(r):
            return f"Opened {label}." if r.get("window") else f"Started {label}."
        return run_tool("desktop.open_app", f"open {label}", ok, name=path)
    return Intent("desktop.open_path", run, "desktop")


def parse_link(text: str) -> Optional[Intent]:
    from modules.agent.link_intents import parse_link as parse
    return parse(text)


def route_single(text: str) -> Optional[Intent]:
    for parser in _SINGLE_PARSERS:
        try:
            intent = parser(text)
        except Exception:
            log.exception("router.parser_failed %s", parser.__name__)
            intent = None
        if intent:
            return intent
    return None


def route(text: str) -> Optional[Intent]:
    # parse_web runs before parse_automation so "what's the weather tomorrow" is
    # answered, not scheduled.
    # parse_files_task: "go to Downloads, click the first download and extract it
    # to my games folder" is ONE extraction, not three unrelated steps.
    # parse_refer: "delete it" / "open that folder" about something SAINT just
    # made or found (modules/agent/recent.py) — before memory, so "delete the
    # junk" never means "forget a memory".
    from modules.agent.refer_intents import parse_refer
    # parse_link first: "send this prompt to Gian's PC on Claude: open the door and lock it" is one
    # request, whatever words the prompt itself contains.
    # parse_task: "continue what we were doing", "do the same for Discord", "set up my gaming workspace".
    from modules.agent.task_intents import parse_task
    for parser in (parse_link, parse_task, parse_open_path, parse_social, parse_web, parse_files_task, parse_refer,
                   parse_automation, parse_taskmgr, parse_memory):
        try:
            intent = parser(text)
        except Exception:
            log.exception("router.parser_failed %s", parser.__name__)
            intent = None
        if intent:
            return intent
    # "Open YouTube on my main screen" = open it, then move it there (logged
    # 2026-09-28: said three times, never understood).
    m = _ON_SCREEN.match(_clean(text))
    if m and route_single(m.group(1)) is not None:
        placed = _route_composite(f"{m.group(1)} then move it to my {m.group(2)}")
        if placed:
            return placed
    composite = _route_composite(text)
    if composite:
        return composite
    return route_single(text)


_ON_SCREEN = re.compile(r"^((?:open|launch|start|pull up|bring up)\s+.+?)\s+on\s+(?:my\s+|the\s+)?"
                        r"((?:main|primary|first|second|third|left|right|other|middle|\d)\s*"
                        r"(?:screen|monitor|display))$", re.I)


_RETRYABLE = ("can't see", "isn't visible", "hasn't loaded", "hasn't changed", "nothing visibly changed",
              "didn't let me", "not found", "couldn't find")


def _observe(label: str) -> str:
    """Cheap observation of the desktop between steps (window + title), logged
    so a failed plan can be diagnosed."""
    try:
        from modules.desktop.controller import desktop
        w = desktop.target_window()
        obs = f"{w.process}:{w.title[:60]}" if w else "no window"
    except Exception as e:
        obs = f"unavailable ({e})"
    log.info("agent.observe %s -> %s", label, obs)
    event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "observe", "step": label, "observation": obs})
    return obs


def run_plan(intents: List[Intent], start: int = 0, replies: Optional[List[str]] = None,
             task: Optional[str] = None) -> Reply:
    """Execute a multi-step plan: OBSERVE -> ACT -> VERIFY -> (retry) -> next.

    Each step's tool verifies its own effect (window focused/moved, page
    title changed after navigation or a click, playback changed). A failed
    step is re-observed and retried once after the UI has had time to settle
    (pages loading, windows appearing); a second failure stops the plan and
    says exactly where. If a step needs the user to choose (e.g. which
    browser window), the rest of the plan continues after the answer.

    ``task``: the task_memory id recording each step, so "continue what we
    were doing" can pick up a plan that stopped (modules/agent/task_memory.py).
    """
    from modules.agent.confirm import choices
    from core.activity import activity
    from core.cancel import cancel
    replies = replies if replies is not None else []
    tok = cancel.token()
    activity.begin("a multi-step request", [step_label(it.name) for it in intents])
    try:
        return _run_plan_steps(intents, start, replies, tok, choices, activity, task)
    finally:
        activity.end()


_GERUND = {"open": "opening", "search": "searching", "click": "clicking", "move": "moving", "type": "typing",
           "play": "playing", "set": "setting", "press": "pressing", "close": "closing", "focus": "focusing",
           "arrange": "arranging", "scroll": "scrolling", "extract": "extracting", "launch": "launching",
           "save": "saving", "restore": "restoring", "run": "running", "pause": "pausing", "resume": "resuming",
           "next": "skipping", "previous": "going back", "volume": "changing the volume", "navigate": "opening",
           "new": "opening a new", "minimize": "minimizing", "maximize": "maximizing", "place": "placing",
           "locate": "looking for", "read": "reading", "recycle": "recycling", "compress": "compressing"}


def step_label(intent_name: str) -> str:
    """'desktop.open_app' -> 'opening an app' (for "what are you doing?")."""
    name = intent_name.split(".", 1)[-1] if "." in intent_name else intent_name
    words = name.replace("_", " ").split()
    if not words:
        return intent_name
    first = _GERUND.get(words[0])
    if first:
        return " ".join([first] + words[1:])
    return "working on " + " ".join(words)


def _run_plan_steps(intents, start, replies, tok, choices, activity, task=None) -> Reply:
    from modules.agent.task_memory import task_memory, RUNNING, DONE, FAILED
    for i in range(start, len(intents)):
        it = intents[i]
        if tok.cancelled:
            log.info("agent.plan.cancelled before step %d/%d", i + 1, len(intents))
            task_memory.finish(task, "stopped", "you said stop")
            return Reply(" ".join(replies + ["Stopped."]).strip(), ok=False)
        activity.step(i)
        task_memory.step(task, i, RUNNING)
        before = _observe(f"before {it.name}") if it.domain in ("desktop", "browser") else ""
        t0 = time.perf_counter()
        r = it.run()
        log.info("agent.step %d/%d %s ok=%s ms=%.0f", i + 1, len(intents), it.name, r.ok,
                 (time.perf_counter() - t0) * 1000)
        if not r.ok and it.domain in ("desktop", "browser") and any(k in r.text.lower() for k in _RETRYABLE):
            # Reassess instead of blindly repeating: wait for the UI, look again, retry once.
            time.sleep(1.2)
            if tok.cancelled:
                task_memory.finish(task, "stopped", "you said stop")
                return Reply(" ".join(replies + ["Stopped."]).strip(), ok=False)
            after = _observe(f"retry {it.name}")
            log.info("agent.retry %s (screen %s)", it.name, "changed" if after != before else "unchanged")
            r = it.run()
        if r.expects_reply and choices.pending is not None and i < len(intents) - 1:
            # Park the rest of the plan behind the user's answer.
            pending = choices.pending
            original = pending.run

            def resume(value, original=original, nxt=i + 1):
                first = original(value)
                task_memory.step(task, nxt - 1, DONE)
                rest = run_plan(intents, nxt, [], task=task)
                return (first + " " + rest.text).strip()
            pending.run = resume
            task_memory.finish(task, "waiting", r.text)
            return Reply(" ".join(replies + [r.text]), ok=True, expects_reply=True)
        if r.expects_reply and confirmations.pending is not None and i < len(intents) - 1:
            # Same for a yes/no question ("You don't have a browser open. Should I open Opera?").
            action = confirmations.pending
            original_run = action.run

            def resume_yes(original_run=original_run, nxt=i + 1):
                first = original_run()
                task_memory.step(task, nxt - 1, DONE)
                rest = run_plan(intents, nxt, [], task=task)
                return (first + " " + rest.text).strip()
            action.run = resume_yes
            task_memory.finish(task, "waiting", r.text)
            return Reply(" ".join(replies + [r.text]), ok=True, expects_reply=True)
        if it.name == "screen.locate" and i + 1 < len(intents) and intents[i + 1].name == "desktop.click_last":
            r.text = re.sub(r'\s*Say "click it" if you want me to\.$', "", r.text)
        replies.append(r.text)
        if not r.ok:
            task_memory.step(task, i, FAILED, r.text)
            task_memory.finish(task, "failed", r.text)
            if i < len(intents) - 1:
                replies.append("I stopped there.")
            return Reply(" ".join(replies), ok=False)
        task_memory.step(task, i, DONE)
        if r.expects_reply:
            task_memory.finish(task, "waiting", r.text)
            return Reply(" ".join(replies), ok=True, expects_reply=True)
        if it.domain in ("desktop", "browser") and i < len(intents) - 1:
            time.sleep(0.4)          # let the UI settle before observing again
    task_memory.finish(task, "done")
    return Reply(" ".join(replies))


def _route_composite(text: str) -> Optional[Intent]:
    """'open my browser, search YouTube for X and click the first video' ->
    a plan of intents executed by run_plan (observe / act / verify)."""
    cleaned = _clean(text)
    parts = [p.strip() for p in re.split(r",?\s+(?:and then|then|and also|after that|and)\s+|,\s+", cleaned, flags=re.I)
             if p.strip()]
    # "search for Backboard Defense codes, training pack codes": text after a search / type
    # step that doesn't start a new command belongs to that step's words.
    merged: List[str] = []
    for p in parts:
        if merged and re.match(r"^(?:search|google|look up|type|write)\b", merged[-1], re.I) \
                and not _NEXT_VERB.match(" " + p) and not re.match(r"^(?:then|and)\b", p, re.I):
            merged[-1] = f"{merged[-1]}, {p}"
        else:
            merged.append(p)
    parts = merged
    if len(parts) > 8:
        return None
    # Parse each step in the context the earlier steps will create: after
    # "search YouTube for X", "pause" / "turn it down" mean the video.
    from modules.agent.context import desktop_context
    saved = desktop_context.domain()
    intents: List[Intent] = []
    sources: List[dict] = []                 # the words each step came from (task memory)
    for n, p in enumerate(parts):
        steps = _route_steps(p)
        if steps is None:
            desktop_context.note_domain(saved)
            return None
        for it in steps:
            intents.append(it)
            sources.append({"text": p, "part": n})
            if it.domain in ("browser", "spotify", "steam"):
                desktop_context.note_domain(it.domain)
    desktop_context.note_domain(saved)
    if len(intents) < 2:
        return None
    log.info("agent.plan %s", [i.name for i in intents])

    def run():
        from modules.agent.task_memory import task_memory
        task = task_memory.begin(text, [dict(src, label=step_label(it.name)) for it, src in zip(intents, sources)])
        return run_plan(intents, task=task)
    return Intent("composite:" + "+".join(i.name for i in intents), run, "composite")


# A new command starting mid-sentence: "open my browser search YouTube for X"
# (speech has no commas). Only tried when the text doesn't parse as one step.
_NEXT_VERB = re.compile(r"\s+(?=(?:search|google|look up|click|double click|right click|play|go to|navigate to|"
                        r"turn|pause|type|press|scroll|put|make|open|close|find|extract|unzip|launch|start|save|"
                        r"restore|lock|mute|tell me|watch|move)\b)", re.I)


def _route_steps(text: str, depth: int = 0) -> Optional[List[Intent]]:
    """One spoken step -> its intents, splitting a run-on step at a second verb."""
    it = route_single(text)
    if it is not None:
        return [it]
    if depth > 4:
        return None
    for m in _NEXT_VERB.finditer(text):
        left, right = text[:m.start()].strip(), text[m.end():].strip()
        if not left or not right:
            continue
        first = route_single(left)
        if first is None:
            continue
        rest = _route_steps(right, depth + 1)
        if rest is not None:
            return [first] + rest
    return None

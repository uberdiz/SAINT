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


_FILLERS = re.compile(r"^(?:actually|oh|um+|uh+|so|okay|ok|well|hmm+|and|but|also|wait|alright|right)[,.!\s]+",
                      re.I)


def _clean(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^(?:hey\s+)?saint[,.!\s]+", "", t, flags=re.I)
    for _ in range(3):   # "Actually, um, what time is it?"
        t2 = _FILLERS.sub("", t)
        if t2 == t:
            break
        t = t2
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
            "I can play and control Spotify, remember things about you, set reminders and scheduled "
            "automations, open, switch, move and close apps, type and press keys for you, and read what's "
            "in the active window. Just ask."), "system")
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
    if m and not re.search(r"\b(reminder|timer|alarm|automation|song|track|playlist|window|app)\b", t):
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
    if rq and not re.search(r"\b(reminders?|playlist|song|music|schedule|screen|window)\b", rq):
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

    # --- queue removal isn't possible through Spotify's API -------------------------------
    if has(r"^(?:remove|delete|take)\b.*\b(?:from|off|out of) (?:the |my )?queue$|^clear (?:the |my )?queue$"):
        return SpotifyIntent("queue_remove", "", {})

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
    if has(r"^(i (?:don'?t|do not) like (?:this|this song|this track|it)|i hate (?:this|this song)|thumbs down|"
           r"this (?:song|track) (?:sucks|is bad)|don'?t (?:play|recommend) (?:this|songs like this))"):
        return SpotifyIntent("dislike", "spotify.feedback", {"signal": -1.0})

    # --- add current to playlist ------------------------------------------------
    m = re.search(r"^(?:add|save|put) (?:this|this song|this track|it|the current song|what's playing) (?:to|in|into|on) (?:my |the )?(.+?)(?: playlist)?$", lower)
    if m and re.fullmatch(r"(?:the )?(?:left|right|top|bottom|middle|center|full ?screen|other side|side|front|back)"
                          r"(?: side| half| corner)?", m.group(1).strip()):
        m = None          # "put it on the left" / "put it in fullscreen" are window commands
    if m:
        return SpotifyIntent("add_to_playlist", "spotify.add_current_to_playlist", {"playlist": m.group(1).strip()})

    # --- queue ----------------------------------------------------------------------
    m = re.search(r"^(?:queue(?: up)?|add) (.+?) (?:to (?:the|my) queue|next)$", lower) or \
        re.search(r"^queue(?: up)? (.+)$", lower) or re.search(r"^play (.+?) next$", lower)
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
    vol = re.search(r"\bvolume\b.*?(\d{1,3})", lower) or re.search(r"\bset (?:the )?(?:music |spotify )?volume (?:to )?(\d{1,3})", lower) \
        or re.search(r"\bturn (?:it|the music|spotify|the volume) (?:up |down )?to (\d{1,3})", lower)
    if vol:
        return SpotifyIntent("volume_set", "spotify.volume", {"percent": max(0, min(100, int(vol.group(1))))})
    musical = has(r"\b(music|song|spotify|track|tune)\b") or desktop_context.music_is_context()
    if musical and has(r"\b(volume up|louder|turn (it|the music|spotify|the volume|the song) up|turn up (the )?(music|volume|song)|crank it)\b"):
        return SpotifyIntent("volume_up", "spotify.volume_step", {"direction": "up"})
    if musical and has(r"\b(volume down|quieter|softer|turn (it|the music|spotify|the volume|the song) down|turn down (the )?(music|volume|song)|lower the volume)\b"):
        return SpotifyIntent("volume_down", "spotify.volume_step", {"direction": "down"})

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
    pm = re.match(r"^(?:my |the )?(.+?) playlist$", q_orig, re.I) or re.match(r"^(?:the )?playlist (.+)$", q_orig, re.I) \
        or re.match(r"^my (?!music$|songs$)(.+)$", q_orig, re.I)
    if pm:
        return SpotifyIntent("play_playlist", "spotify.play_query", {"query": pm.group(1).strip(), "kind": "playlist"})
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


def _spotify_reply(si: SpotifyIntent, r) -> str:
    k = si.kind
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
    if k == "liked":
        return f"Playing your liked songs ({r['count']} tracks, shuffled)."
    if k == "similar_seed":
        return f"Playing {r['name']} by {r['artist']}, then {r['count'] - 1} more — {r.get('basis', '')}."
    if k in ("play_for_me", "similar"):
        basis = f" Picked from {r['basis']}." if r.get("basis") else ""
        return f"Playing {r['name']} by {r['artist']}, then {r['count'] - 1} more.{basis}"
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
        return f"Playing {r['genre']} — {r['name']}." if r.get("name") and "tracks" not in r["name"] \
            else f"Playing some {r['genre']}, starting with {r.get('first')}."
    return f"Playing {r['name']} by {r['artist']}."


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
            return Reply(res.error, ok=False)
        reply = Reply(_spotify_reply(si, res.result))
        if si.kind == "search":
            items = [x for x in ((res.result.get("tracks") or {}).get("items") or []) if x]
            if items:
                first = items[0]

                def play_found():
                    r2 = call("spotify.play", uri=first["uri"])
                    return f"Playing {first['name']}." if r2.success else r2.error
                confirmations.ask(PendingAction(f"play {first['name']}", play_found, tool="spotify.play"))
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
        if name in ("it", "that", "this"):
            return None

        def run_open():
            def ok(r):
                if r.get("reused"):
                    t = re.sub(r"^\(\d+\)\s*", "", r.get("window") or "")
                    return f"Switched to {t.split(' - ')[-1] or r['app']}" + (
                        f" (you have {r['count']} of its windows open)." if r.get("count", 1) > 1 else ".")
                if r.get("window"):
                    return f"Opened {r['app']}."
                return f"Launched {r['app']}; its window hasn't appeared yet."
            return run_tool("desktop.open_app", f"open {name}", ok, name=name)
        return Intent("desktop.open_app", run_open, "desktop")

    # close
    m = re.match(r"^(?:close|quit|exit|shut down|kill)\s+(?:the\s+|my\s+)?(.+?)(?:\s+(?:app|application|window))?$", t)
    if m and not re.search(r"\b(reminder|timer|alarm)\b", t):
        name = m.group(1)
        label = "this window" if name in ("this", "it", "that", "this window", "the window") else name

        def run_close():
            def ok(r):
                if r.get("closed"):
                    return f"Closed {r['title'].split(' - ')[-1] or label}."
                return f"I asked {label} to close, but it's still open — {r.get('note', '')}".strip()
            return run_tool("desktop.close_app", f"close {label}", ok, name=name)
        return Intent("desktop.close_app", run_close, "desktop")

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
                            lambda r: f"Moved {r['title'].split(' - ')[-1] or 'it'} to the {side} side.",
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
                            lambda r: f"Moved {r['title'].split(' - ')[-1] or 'it'} to monitor {r['monitor']}.",
                            window=window, monitor=which)
        return Intent("desktop.move_window", run_move, "desktop")

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

        def run_focus():
            return run_tool("desktop.focus_window", f"switch to {name}",
                            lambda r: f"Switched to {r['title'].split(' - ')[-1] or name}.", name=name)
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
        keys = m.group(1).replace(" plus ", "+").replace(" and ", "+")

        def run_press():
            return run_tool("desktop.press_keys", f"press {keys}", lambda r: f"Pressed {r['keys']}.", keys=keys)
        return Intent("desktop.press_keys", run_press, "desktop")

    # click an element
    m = re.match(r"^click(?: on)?\s+(?:the\s+)?(.+?)(?:\s+(?:button|link|tab))?$", t)
    if m:
        name = m.group(1)

        def run_click():
            return run_tool("desktop.click_element", f"click {name}", lambda r: f"Clicked {r['clicked']}.", name=name)
        return Intent("desktop.click_element", run_click, "desktop")
    return None


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
    r"|(?:what|any)\s+(?:updates?|news)\s+on\b)")


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
        return Reply(answer or "I couldn't find anything current on that.")
    return Intent("web.current", run_web, "web")


_SINGLE_PARSERS = [parse_system, parse_web, parse_spotify, parse_desktop_nl, parse_desktop]


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
    for parser in (parse_web, parse_automation, parse_memory):
        try:
            intent = parser(text)
        except Exception:
            log.exception("router.parser_failed %s", parser.__name__)
            intent = None
        if intent:
            return intent
    composite = _route_composite(text)
    if composite:
        return composite
    return route_single(text)


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


def run_plan(intents: List[Intent], start: int = 0, replies: Optional[List[str]] = None) -> Reply:
    """Execute a multi-step plan: OBSERVE -> ACT -> VERIFY -> (retry) -> next.

    Each step's tool verifies its own effect (window focused/moved, page
    title changed after navigation or a click, playback changed). A failed
    step is re-observed and retried once after the UI has had time to settle
    (pages loading, windows appearing); a second failure stops the plan and
    says exactly where. If a step needs the user to choose (e.g. which
    browser window), the rest of the plan continues after the answer.
    """
    from modules.agent.confirm import choices
    replies = replies if replies is not None else []
    for i in range(start, len(intents)):
        it = intents[i]
        before = _observe(f"before {it.name}") if it.domain in ("desktop", "browser") else ""
        t0 = time.perf_counter()
        r = it.run()
        log.info("agent.step %d/%d %s ok=%s ms=%.0f", i + 1, len(intents), it.name, r.ok,
                 (time.perf_counter() - t0) * 1000)
        if not r.ok and it.domain in ("desktop", "browser") and any(k in r.text.lower() for k in _RETRYABLE):
            # Reassess instead of blindly repeating: wait for the UI, look again, retry once.
            time.sleep(1.2)
            after = _observe(f"retry {it.name}")
            log.info("agent.retry %s (screen %s)", it.name, "changed" if after != before else "unchanged")
            r = it.run()
        if r.expects_reply and choices.pending is not None and i < len(intents) - 1:
            # Park the rest of the plan behind the user's answer.
            pending = choices.pending
            original = pending.run

            def resume(value, original=original, nxt=i + 1):
                first = original(value)
                rest = run_plan(intents, nxt, [])
                return (first + " " + rest.text).strip()
            pending.run = resume
            return Reply(" ".join(replies + [r.text]), ok=True, expects_reply=True)
        replies.append(r.text)
        if not r.ok:
            if i < len(intents) - 1:
                replies.append("I stopped there.")
            return Reply(" ".join(replies), ok=False)
        if r.expects_reply:
            return Reply(" ".join(replies), ok=True, expects_reply=True)
        if it.domain in ("desktop", "browser") and i < len(intents) - 1:
            time.sleep(0.4)          # let the UI settle before observing again
    return Reply(" ".join(replies))


def _route_composite(text: str) -> Optional[Intent]:
    """'open my browser, search YouTube for X and click the first video' ->
    a plan of intents executed by run_plan (observe / act / verify)."""
    cleaned = _clean(text)
    parts = [p.strip() for p in re.split(r",?\s+(?:and then|then|and also|after that|and)\s+|,\s+", cleaned, flags=re.I)
             if p.strip()]
    if len(parts) < 2 or len(parts) > 8:
        return None
    # Parse each step in the context the earlier steps will create: after
    # "search YouTube for X", "pause" / "turn it down" mean the video.
    from modules.agent.context import desktop_context
    saved = desktop_context.domain()
    intents: List[Intent] = []
    for p in parts:
        it = route_single(p)
        if it is None:
            desktop_context.note_domain(saved)
            return None
        intents.append(it)
        if it.domain in ("browser", "spotify"):
            desktop_context.note_domain(it.domain)
    desktop_context.note_domain(saved)
    log.info("agent.plan %s", [i.name for i in intents])
    return Intent("composite:" + "+".join(i.name for i in intents), lambda: run_plan(intents), "composite")

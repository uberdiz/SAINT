"""
modules/learning/planner.py

"Try harder": when SAINT doesn't recognise a request, or the command it
picked fails ("I couldn't find a window for all my windows"), the local model
rewrites the request as commands SAINT *does* understand:

    "minimize all my windows and double left click the recycling bin"
        -> ["show the desktop", "double click the recycle bin"]
    "open disk, clean up"      -> ["open disk cleanup"]

Every step must be something the router understands (checked before anything
runs), so the model can only choose among real, permission-checked commands —
it never gets raw tools. The plan runs through run_plan (observe, act,
verify, retry once). When it works, the request is saved as a skill so next
time it runs straight away without asking the model.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from core.config import config
from modules.learning.skills import norm, skills

log = logging.getLogger("saint.learning")

# Requests that ask SAINT to *do* something (vs. questions and chat).
_ACTION_START = re.compile(
    r"^(?:open|close|quit|exit|kill|end|launch|start|run|play|pause|resume|skip|click|double|right[- ]click|left[- ]click|"
    r"press|hit|type|minimi[sz]e|maximi[sz]e|show|hide|move|put|make|set|turn|switch|change|mute|unmute|go|take|"
    r"find|search|look up|delete|remove|clean|clear|empty|extract|unzip|install|uninstall|enable|disable|increase|"
    r"decrease|raise|lower|lock|restart|shut|save|restore|copy|paste|select|scroll|snap|drag|download|get|bring|"
    r"full ?screen|zoom|rename|create|new|bring up|pull up|load|focus|reopen|refresh|reload|stop|tidy|fix|organi[sz]e|"
    r"arrange|free up|defrag\w*|check|update|connect|disconnect|pair|record|share|send|print|scan|back ?up|sort|"
    r"zip|compress|mute|minimize|add|queue|like|save|remove|shuffle|repeat|skip)\b", re.I)
_CHATTY = re.compile(r"^(?:what|why|how|who|when|where|which|is|are|do|does|did|can i|should|tell me about)\b", re.I)
_FILLER = re.compile(r"^(?:oh my (?:gosh|god|goodness)[,.!\s]+|"
                     r"(?:oh|ah|um+|uh+|hmm+|wow|dude|bro|yo|okay|ok|so|well|alright|right|hey|now)[,.!\s]+)+", re.I)

_SYSTEM = """You plan actions for SAINT, a voice assistant that controls a Windows PC.
Rewrite the user's request as a short list of SAINT commands, in order.
Rules:
- Use ONLY the command shapes listed below. Swap the names in them (apps, songs, buttons, sites, folders) for what the user wants.
- Do things in the order the user said them. One action per command, at most 6 commands.
- If one listed command already does the whole request, answer with just that one.
- Speech-to-text mishears names: match them to the open windows listed ("the finers" is THE FINALS, "to area" is Terraria, "recycling bin" is the Recycle Bin, "disk, clean up" is Disk Cleanup).
- Never say "this window" or "it" unless the user did. Never type text the user didn't dictate.
- If the request can't be done with these commands, answer with an empty list.
- Answer with JSON only, like {"steps": ["command", "command"]}.

Examples:
Request: minimize all my windows and double left click the recycling bin
{"steps": ["show the desktop", "double click the recycle bin"]}
Request: open disk, clean up
{"steps": ["open disk cleanup"]}
Request: make spotify quieter
{"steps": ["turn down spotify"]}
Request: close the finers   (open windows: Opera, THE FINALS, Spotify)
{"steps": ["close THE FINALS"]}
Request: start my music and open discord
{"steps": ["resume the music", "open discord"]}
Request: write my essay about the civil war
{"steps": []}"""


def grounded(step: str, request: str) -> bool:
    """Reject plan steps the user never asked for: acting on "this window"
    when they named something else, or typing text they didn't dictate."""
    s, r = norm(step), norm(clean(request))
    if re.match(r"^type\b", s) and not re.search(r"\b(?:type|write|enter|fill in|dictate)\b", r):
        return False
    if re.search(r"\b(?:this|that|it)(?:\s+(?:window|one|tab|app))?$", s) and \
            not re.search(r"\b(?:this|that|it|current|here|front)\b", r):
        return False
    # "my left monitor" must stay the left one.
    sides = r"\b(left|right|main|primary|first|second|other)\s+(?:monitor|screen|display)\b"
    said, planned = re.search(sides, r), re.search(sides, s)
    if said and planned and said.group(1) != planned.group(1) and \
            {said.group(1), planned.group(1)} not in ({"main", "primary"}, {"second", "other"}):
        return False
    # "... but youtube" when YouTube was never mentioned.
    m = re.search(r"\b(?:but|except)\s+(?:the\s+|my\s+)?(.+)$", s)
    if m and not all(w in r for w in m.group(1).split()):
        return False
    # Clicking something the user never named ("click Settings" for "click on
    # the loop layer") — the target must sound like something they said.
    m = re.match(r"^(?:double |right |middle )?click(?:\s+on)?\s+(?:the\s+)?(.+?)(?:\s+on\s+(?:my\s+|the\s+)?"
                 r"\w+\s+(?:screen|monitor|display))?$", s)
    if m and not _mentioned(m.group(1), r):
        return False
    # Keys the user didn't ask for ("press delete", "press enter" after opening a folder).
    m = re.match(r"^(?:press|hit)\s+(.+)$", s)
    if m and not re.search(r"\b(?:press|hit|key|shortcut)\b", r):
        return False
    return True


def _mentioned(target: str, request: str) -> bool:
    """Does ``target`` sound like something in ``request``?"""
    import difflib
    from modules.desktop.window_match import sounds_like
    t_words = [w for w in re.findall(r"[a-z0-9]+", target.lower())
               if w not in ("the", "a", "my", "on", "button", "icon", "link", "tab", "desktop", "video")]
    if not t_words:
        return True
    said = re.findall(r"[a-z0-9]+", request.lower())
    said += [a + b for a, b in zip(said, said[1:])]          # "loop layer" ~ "Loopler"
    hits = 0
    for w in t_words:
        if any(w == x or difflib.SequenceMatcher(None, w, x).ratio() >= 0.75 or
               (len(w) > 3 and sounds_like(w, x) >= 0.9) for x in said):
            hits += 1
    return hits >= max(1, (len(t_words) + 1) // 2)


def clean(text: str) -> str:
    return _FILLER.sub("", (text or "").strip()).strip()


def worth_planning(text: str) -> bool:
    """Is this an instruction to do something (worth the model's time)?"""
    t = norm(clean(text))
    if not t or len(t.split()) > 30 or _CHATTY.match(t):
        return False
    return bool(_ACTION_START.match(t))


def understood(command: str) -> bool:
    """Would SAINT act on this command without asking the model?"""
    from modules.agent.meta import match_meta
    from modules.agent.router import route
    if match_meta(command) is not None or skills.match(command) is not None:
        return True
    try:
        return route(command) is not None
    except Exception:
        log.exception("planner.route_failed %r", command)
        return False


def _context() -> str:
    lines = []
    try:
        from modules.desktop.controller import desktop
        from modules.vision.screen import app_label
        wins = desktop.list_windows()
        fg = next((w for w in wins if w.foreground), None)
        names = []
        for w in wins:
            label = app_label({"title": w.title, "process": w.process})
            if label and label not in names:
                names.append(label)
        if fg:
            lines.append(f"In front: {app_label({'title': fg.title, 'process': fg.process})}")
        if names:
            lines.append("Open windows: " + ", ".join(names[:14]))
    except Exception:
        pass
    learned = skills.recent(12)
    if learned:
        lines.append("Things SAINT has already learned:\n" +
                     "\n".join(f"  - {s.phrase} -> {json.dumps(s.steps)}" for s in learned))
    return "\n".join(lines)


def _ask(prompt: str) -> Optional[dict]:
    from modules.ai.module import AIModule
    base = config.get("ai.base_url", "http://localhost:11434")
    try:
        model, _ = AIModule._resolve_model(None, "ollama", base, config.get("ai.model", ""))
        r = requests.post(base.replace("localhost", "127.0.0.1").rstrip("/") + "/api/chat",
                          timeout=float(config.get("learning.planner_timeout_sec", 25)),
                          json={"model": model, "stream": False, "keep_alive": -1, "format": "json",
                                "options": {"temperature": 0.1, "num_predict": 200},
                                "messages": [{"role": "system", "content": _SYSTEM},
                                             {"role": "user", "content": prompt}]})
        if r.status_code != 200:
            log.warning("planner.http %s", r.status_code)
            return None
        return json.loads((r.json().get("message") or {}).get("content") or "{}")
    except Exception as e:
        log.warning("planner.failed %s", e)
        return None


def plan(text: str, failure: str = "", rejected: Optional[List[str]] = None) -> List[str]:
    from modules.learning.catalog import as_prompt
    prompt = f"Commands SAINT understands:\n{as_prompt()}\n\n{_context()}\n\n"
    if failure:
        prompt += f"SAINT already tried this and it failed: \"{failure}\". Find another way.\n"
    if rejected:
        prompt += ("These aren't commands SAINT knows, don't use them: " +
                   "; ".join(f"\"{r}\"" for r in rejected) + "\n")
    prompt += f"Request: {clean(text)}"
    data = _ask(prompt)
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        return []
    out = [re.sub(r"\s+", " ", s).strip(" .") for s in steps if isinstance(s, str) and s.strip()]
    log.info("planner.plan request=%r steps=%r", text[:80], out)
    return out[:6]


@dataclass
class Outcome:
    reply: object                     # router Reply
    steps: List[str] = field(default_factory=list)
    learned: bool = False


def _intents(steps: List[str]):
    from modules.agent.meta import match_meta, run_meta
    from modules.agent.router import Intent, Reply, route
    out = []
    for s in steps:
        m = match_meta(s)
        if m is not None:
            out.append(Intent(f"meta.{m.kind}", lambda m=m: Reply(run_meta(m)), "system"))
            continue
        sk = skills.match(s)
        if sk is not None:
            for sub in sk.steps:
                it = route(sub)
                if it is None:
                    return None
                out.append(it)
            continue
        it = route(s)
        if it is None:
            return None
        out.append(it)
    return out


def attempt(text: str, failure: str = "") -> Optional[Outcome]:
    """Plan, check and run ``text``. None when no workable plan was found
    (nothing has been done in that case)."""
    if not config.get("learning.planner", True):
        return None
    steps = plan(text, failure)
    if not steps:
        return None
    bad = [s for s in steps if not understood(s) or not grounded(s, text)]
    if bad:
        log.info("planner.rejected %r", bad)
        steps = plan(text, failure, rejected=bad)
        if not steps or any(not understood(s) or not grounded(s, text) for s in steps):
            return None
    if failure and [norm(s) for s in steps] == [norm(text)]:
        return None                     # the same thing that just failed
    intents = _intents(steps)
    if not intents:
        return None
    from modules.agent.router import run_plan
    reply = run_plan(intents)
    learned = False
    if reply.ok and not reply.expects_reply:
        learned = skills.learn(text, steps, "planned") is not None
    log.info("planner.ran steps=%r ok=%s learned=%s", steps, reply.ok, learned)
    return Outcome(reply, steps, learned)

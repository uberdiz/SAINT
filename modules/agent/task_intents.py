"""
modules/agent/task_intents.py

Requests about the task itself rather than a single action:

    "continue what we were doing" / "finish it" / "pick up where we left off"
    "do the same thing for Discord" / "do that again for this"
    "what were we doing?" / "where were we?"
    "set up my gaming workspace"
    "move the mini player to my second monitor" / "put SAINT on the left screen"
    "auto gaming mode on/off"

Everything here is deterministic: the steps are routed again from the words
they came from (modules/agent/task_memory.py) and executed by the router.
"""

import logging
import re
import time
from typing import List, Optional

from modules.agent.router import Intent, Reply, _clean, route, run_plan, step_label

log = logging.getLogger("saint.agent.tasks")

_CONTINUE = re.compile(
    r"^(?:(?:please|ok(?:ay)?|now|alright)\s+)?(?:"
    r"(?:continue|resume|carry on|keep going|go on)\s+(?:with\s+)?(?:what\s+(?:we|you)\s+were\s+doing|"
    r"where\s+(?:we|you)\s+left\s+off|the\s+(?:task|job|plan|setup)|it|that|from\s+there)"
    r"|pick\s+(?:it\s+|that\s+)?(?:back\s+)?up(?:\s+where\s+(?:we|you)\s+left\s+off)?"
    r"|(?:finish|complete)\s+(?:it|that|the\s+(?:task|job|plan|rest|setup)|what\s+(?:we|you)\s+(?:were\s+doing|started))"
    r"|(?:do|finish)\s+the\s+rest(?:\s+of\s+it)?"
    r")(?:\s+please)?[.!]?$", re.I)

_STATUS = re.compile(
    r"^(?:what\s+were\s+we\s+(?:doing|working\s+on)|where\s+were\s+we|where\s+did\s+we\s+(?:leave|stop)(?:\s+off)?"
    r"|what(?:'s|\s+is)\s+left(?:\s+to\s+do)?|what\s+did\s+we\s+(?:get\s+)?(?:done|finish)"
    r"|(?:task|job)\s+status|what\s+(?:task|job)\s+(?:are\s+you|were\s+you)\s+(?:on|doing))\??$", re.I)

_SAME = re.compile(
    r"^(?:(?:now|ok(?:ay)?|and)\s+)?(?:do|try)\s+(?:the\s+same(?:\s+thing)?|that|it)(?:\s+again)?\s+"
    r"(?:for|with|to|on)\s+(?P<target>.+?)(?:\s+(?:too|as\s+well))?[.!]?$", re.I)

_WORKSPACE = re.compile(
    r"^(?:(?:please|can\s+you|could\s+you)\s+)?(?:set\s*up|get|make|prepare|ready)\s+(?:me\s+)?(?:my\s+|the\s+|a\s+)?"
    r"(?:gaming|game)\s+(?:workspace|setup|set\s*up|station|desk|layout)(?:\s+(?:ready|up))?(?:\s+please)?[.!]?$"
    r"|^(?:set\s+(?:me\s+)?up\s+for\s+gaming|gaming\s+setup)[.!]?$", re.I)

_PLACE = re.compile(
    r"^(?:move|put|send|place|show)\s+(?:the\s+|my\s+|your\s+)?(?P<what>mini\s*player|music\s+player|"
    r"(?:the\s+)?saint(?:'s)?\s+(?:window|ui|app)|saint|yourself|your\s+window|the\s+overlay|overlay)\s+"
    r"(?:to|on|onto)\s+(?:my\s+|the\s+)?(?P<mon>main|primary|first|second|third|other|left|right|\d)\s*"
    r"(?:monitor|screen|display)?[.!]?$", re.I)

_AUTO = re.compile(r"^(?:turn\s+|switch\s+)?(?P<v1>on|off)?\s*auto(?:matic)?\s+gam(?:e|ing)\s+mode"
                   r"(?:\s+(?P<v2>on|off))?[.!]?$", re.I)

_PRONOUNS = {"it", "that", "this", "them", "those", "these"}
_VERB = (r"(?:open|launch|start|run|close|quit|kill|exit|mute|unmute|move|put|send|minimi[sz]e|maximi[sz]e|focus|"
         r"switch\s+to|go\s+to|play|search(?:\s+for)?|look\s+up|google|find|delete|recycle|extract|unzip|"
         r"compress|zip|install|uninstall|show|hide|pin|unpin|restart|update|turn\s+(?:up|down)|snap|arrange)")
_OBJECT = re.compile(rf"^(?P<verb>{_VERB})\s+(?P<obj>.+?)"
                     r"(?P<tail>\s+(?:to|on|onto|into|in|at|from|with|for|as|by)\s+.+)?$", re.I)


def parse_task(text: str) -> Optional[Intent]:
    t = _clean(text)
    if not t:
        return None
    if _STATUS.match(t):
        return Intent("task.status", _status, "meta")
    if _CONTINUE.match(t):
        return Intent("task.continue", _continue, "meta")
    m = _SAME.match(t)
    if m:
        return Intent("task.same_for", lambda: _same_for(m.group("target")), "meta")
    if _WORKSPACE.match(t):
        return Intent("task.gaming_workspace", gaming_workspace, "ui")
    m = _PLACE.match(t)
    if m:
        what = m.group("what").lower()
        what = "mini_player" if "player" in what else "overlay" if "overlay" in what else "saint"
        return Intent("ui.place", lambda: _place(what, m.group("mon").lower()), "ui")
    m = _AUTO.match(t)
    if m and (m.group("v1") or m.group("v2")):
        return Intent("ui.auto_gaming_mode", lambda: _auto(m.group("v1") or m.group("v2")), "ui")
    return None


# ---------------------------------------------------------------------- #
def _status() -> Reply:
    from modules.agent.task_memory import task_memory
    from core.activity import activity
    now = activity.describe()
    if now and not now.lower().startswith(("i'm not", "nothing")):
        return Reply(now)
    return Reply(task_memory.describe())


def _continue() -> Reply:
    from modules.agent.task_memory import task_memory
    task = task_memory.resumable()
    if task is None:
        last = task_memory.current()
        if last and last.get("status") == "done":
            return Reply(f"We already finished {last['title'].lower()} — there's nothing left to continue.")
        return Reply("There's nothing unfinished to pick up. Tell me what you'd like to do.", ok=False)
    steps = task["steps"]
    # Route every step again from its words; the plan is only resumed in place
    # when it still routes to the same number of steps.
    intents: List[Intent] = []
    for part in sorted({s["part"] for s in steps}):
        words = next(s["text"] for s in steps if s["part"] == part)
        it = route(words)
        if it is None:
            return Reply(f"I can't work out “{words}” any more, so I can't continue that task.", ok=False)
        if it.name.startswith("composite"):
            return Reply("That task has changed too much for me to continue it. Say it again and I'll start over.",
                         ok=False)
        intents.append(it)
    if len(intents) != len(steps):
        # A part that ran as several steps: run again each part that isn't finished.
        parts = sorted({s["part"] for s in steps if s["status"] != "done"})
        words = {p: next(s["text"] for s in steps if s["part"] == p) for p in parts}
        intents = [route(words[p]) for p in parts]
        new = task_memory.begin(task["request"], [{"text": words[p], "part": p, "label": step_label(i.name)}
                                                  for p, i in zip(parts, intents)])
        task_memory.finish(task["id"], "stopped", "continued as a new task")
        r = run_plan(intents, task=new)
        return Reply(f"Picking up {task['title'].lower()}. {r.text}".strip(), ok=r.ok, expects_reply=r.expects_reply)
    start = task_memory.remaining(task)[0]
    log.info("task.continue %s from step %d/%d", task["id"], start + 1, len(steps))
    r = run_plan(intents, start=start, task=task["id"])
    return Reply(f"Picking up {task['title'].lower()}. {r.text}".strip(), ok=r.ok, expects_reply=r.expects_reply)


# ---------------------------------------------------------------------- #
def _foreground_app() -> str:
    try:
        import psutil
        import win32gui
        import win32process
        pid = win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow())[1]
        name = psutil.Process(pid).name()
        name = re.sub(r"\.exe$", "", name, flags=re.I)
        return "" if name.lower() in ("saint", "python", "pythonw", "explorer") else name
    except Exception:
        return ""


def substitute(request: str, target: str) -> Optional[str]:
    """'open chrome and move it to my second monitor', 'Discord' ->
    'open Discord and move it to my second monitor'; None when it isn't clear
    what to swap (the request then isn't guessed at)."""
    first = re.split(r",?\s+(?:and then|then|and|after that)\s+|,\s+", request.strip(), maxsplit=1)[0]
    m = _OBJECT.match(first)
    if not m:
        return None
    obj = re.sub(r"^(?:the|my|a|an)\s+", "", m.group("obj").strip(), flags=re.I)
    if not obj or obj.lower() in _PRONOUNS or len(obj.split()) > 5:
        return None
    pattern = re.compile(rf"(?<![\w]){re.escape(obj)}(?![\w])", re.I)
    if not pattern.search(request):
        return None
    return pattern.sub(target, request)


def _same_for(target: str) -> Reply:
    from modules.learning.feedback import feedback
    target = re.sub(r"^(?:the|my)\s+", "", target.strip(), flags=re.I)
    if target.lower() in ("this", "this one", "this window", "this app", "that", "that one", "it"):
        target = _foreground_app()
        if not target:
            return Reply("I can't tell what “this” is — say its name.", ok=False)
    turns = [t for t in feedback.recent_turns(1800.0)
             if not t["intent"].startswith(("task.", "meta.", "ui.place")) and t.get("ok")]
    if not turns:
        return Reply("I don't have a recent request to repeat. Tell me what to do for "
                     f"{target}.", ok=False)
    last = turns[-1]["text"]
    new = substitute(last, target)
    if not new or new.strip().lower() == last.strip().lower():
        return Reply(f"I'm not sure what to swap for {target} in “{last}”. Say the whole request and I'll do it.",
                     ok=False)
    it = route(new)
    if it is None:
        return Reply(f"I'd do “{new}”, but I don't know how to do that one.", ok=False)
    log.info("task.same_for %r -> %r", last, new)
    r = it.run()
    return Reply(r.text, ok=r.ok, expects_reply=r.expects_reply)


# ---------------------------------------------------------------------- #
def _ui(cmd: str, **args):
    from core.ui_link import ui_link
    return ui_link.send(cmd, timeout=3.0, **args)


def _place(what: str, monitor: str) -> Reply:
    ok, msg = _ui("place", what=what, monitor=monitor)
    name = {"mini_player": "The mini player", "overlay": "The overlay"}.get(what, "SAINT")
    if not ok:
        return Reply(msg or "SAINT's window isn't responding.", ok=False)
    return Reply(f"{name} is on {msg or 'that monitor'} now.")


def _auto(value: str) -> Reply:
    from core.config import config
    on = value.lower() == "on"
    config.set("game_mode.enabled", on)
    if on:
        return Reply("Auto Gaming Mode on — Gaming Mode turns on when a game starts.")
    return Reply("Auto Gaming Mode off — games won't switch Gaming Mode on; say “gaming mode on” when you want it.")


def gaming_workspace() -> Reply:
    """Gaming Mode on, SAINT off the game's monitor, mini player and Spotify
    ready — each step checked, and what didn't work said plainly."""
    from core.config import config
    from core.game_mode import game_mode
    from modules.agent.task_memory import task_memory, DONE, FAILED, SKIPPED
    from modules.desktop.controller import desktop

    labels = ["finding the game", "checking monitors", "turning Gaming Mode on", "moving SAINT",
              "showing the mini player", "opening Spotify", "checking it all"]
    task = task_memory.begin("set up my gaming workspace", [{"label": l, "text": "set up my gaming workspace",
                                                              "part": 0} for l in labels], kind="builtin")
    said: List[str] = []

    def mark(i, ok=True, note=""):
        task_memory.step(task, i, DONE if ok is True else (SKIPPED if ok is None else FAILED), note)

    game = game_mode.game if game_mode.running else ""
    mark(0, True if game else None, game or "no game running yet")
    try:
        mons = desktop.monitors()
    except Exception as e:
        mons = []
        mark(1, False, str(e))
    else:
        mark(1)
    game_mode.set_manual(True)
    mark(2, game_mode.active)
    said.append("Gaming Mode is on" + (f" for {game}" if game else ""))

    if len(mons) > 1:
        ok, where = _ui("gaming_workspace")
        if ok and where:
            said.append(f"SAINT is on {where}")
            mark(3)
        elif ok:
            mark(3, None, "already there")
        else:
            mark(3, False, where)
            said.append("I couldn't move SAINT's window")
    else:
        mark(3, None, "one monitor")
        said.append("you have one monitor, so SAINT stays put")

    if game_mode.feature("mini_player"):
        ok, msg = _ui("set", feature="mini_player", value="on")
        mark(4, ok, msg)
        if ok:
            said.append("the mini player is up")
    else:
        mark(4, None, "off in Gaming Mode settings")

    if config.get("game_mode.workspace_spotify", True) and game_mode.feature("spotify"):
        running = False
        try:
            import psutil
            running = any((p.info.get("name") or "").lower() == "spotify.exe" for p in psutil.process_iter(["name"]))
        except Exception:
            pass
        if running:
            mark(5, None, "already open")
        else:
            try:
                from modules.agent.router import call
                res = call("desktop.open_app", name="spotify")
                ok = bool(getattr(res, "success", True))
                mark(5, ok, "" if ok else getattr(res, "error", ""))
                said.append("Spotify is open" if ok else "Spotify didn't open")
            except Exception as e:
                mark(5, False, str(e))
                said.append("Spotify didn't open")
    else:
        mark(5, None, "turned off")

    time.sleep(0.3)
    problems = [s for s in (task_memory.current() or {}).get("steps", []) if s["status"] == FAILED]
    mark(6, not problems)
    task_memory.finish(task, "done" if not problems else "failed",
                       "; ".join(p["label"] for p in problems))
    off = [n for n, v in (("vision", game_mode.feature("vision")),
                          ("screen automation", game_mode.feature("screen_automation"))) if not v]
    if off:
        said.append(" and ".join(off) + " are off while you play")
    text = ", ".join(said[:-1]) + (" and " if len(said) > 1 else "") + said[-1] + "."
    return Reply(text[:1].upper() + text[1:], ok=not problems)

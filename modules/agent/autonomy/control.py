"""
modules/agent/autonomy/control.py

Talking to SAINT about the task itself:

    "pause" / "hold on" / "wait"             pause after the current step (bare "pause" only when a
                                             task is running and no music is playing — otherwise
                                             it's the music hot-word, as before)
    "continue" / "resume" / "keep going"     carry on (a paused, stopped or failed task)
    "stop the task" / "cancel that task"     stop it (it can still be continued)
    "where are you?" / "how far along?"      progress
    "what's next?"                           the next steps
    "why did that fail?" / "what went wrong?"  the last failure and what SAINT tried
    "do that again"                          run the last task again
    "remember how I just did that" / "save that as coding time"   learn the last task
    "actually put Spotify on my main monitor" / "use the other monitor"   change the task

``handle`` returns None when the words aren't about a task SAINT has, so they go on to the rest
of the agent (a bare "continue" with nothing to continue, "pause" for the music).
"""

import re
import time
from dataclasses import dataclass
from typing import Optional

from modules.agent.autonomy.manager import RESUME_MAX_AGE, agent_tasks
from modules.agent.autonomy.model import TaskStatus

_F = r"^(?:(?:ok(?:ay)?|hey|saint|please|um+|uh+|so|now|just)[,\s]+)*"
_E = r"(?:[,\s]+(?:please|saint|for\s+a\s+(?:sec|second|minute|moment)))*[.!?]*$"

_PAUSE_EXPLICIT = re.compile(_F + r"(?:hold on|hang on|hold up|one (?:sec|second|moment)|give me a (?:sec|second|minute|moment)|"
                             r"wait(?: a (?:sec|second|minute|moment))?|pause (?:the task|that|it|what you'?re doing|for now|"
                             r"the setup|working)|stop for a (?:sec|second|minute|moment))" + _E, re.I)
_PAUSE_BARE = re.compile(_F + r"pause" + _E, re.I)
_RESUME = re.compile(_F + r"(?:continue|resume|keep going|go on|carry on|unpause|you can (?:continue|go on|keep going)|"
                     r"go ahead and (?:continue|finish)|(?:continue|resume) (?:the task|it|that|working|the setup)|"
                     r"pick (?:it|that) (?:back )?up|finish (?:it|that|the task))" + _E, re.I)
_STOP = re.compile(_F + r"(?:(?:stop|cancel|abort|quit|end) (?:the |this |that )?(?:task|job|setup|plan|working on (?:it|that))"
                   r"|never ?mind (?:the|that) (?:task|setup))" + _E, re.I)
_WHERE = re.compile(_F + r"(?:where are you(?: at)?(?: with (?:it|that))?|how far (?:along )?are you|what step are you on|"
                    r"how(?:'s| is) (?:it|that|the task) going|what(?:'s| is) the progress|progress(?: report)?|"
                    r"are you done(?: yet)?|is it (?:done|ready)(?: yet)?)" + _E, re.I)
_NEXT = re.compile(_F + r"(?:what(?:'s| is) next|what(?:'s| is) the next step|what are you (?:doing|going to do) next|"
                   r"what(?:'s| is) left)" + _E, re.I)
_FAILURE = re.compile(_F + r"(?:why did (?:that|it|this) (?:fail|not work|break)|why didn'?t (?:that|it|this) work|"
                      r"what (?:failed|went wrong|happened)|what was the (?:problem|error))" + _E, re.I)
_REDO = re.compile(_F + r"(?:do (?:that|it|this) again|(?:run|redo) (?:that|it|this)(?: again)?|one more time|again|"
                   r"repeat that)" + _E, re.I)
_REMEMBER = re.compile(_F + r"(?:remember (?:how|what) (?:i|you|we) just did(?: (?:that|it))?|remember how to do (?:that|this)|"
                       r"learn (?:that|this|how to do that)|remember (?:that|this)(?: as (?P<as1>.+?))?|"
                       r"save (?:that|this)(?: as (?P<as2>.+?))?)" + _E, re.I)
_MODIFY = re.compile(_F + r"(?:actually|instead|rather|no wait|wait no|no,? actually)[,\s]+(?P<rest>.+)$|"
                     + _F + r"(?P<mon>use (?:my |the )?(?:main|primary|first|second|third|left|right|other) "
                     r"(?:monitor|screen|display)(?: instead)?)" + _E, re.I)


@dataclass
class Control:
    kind: str
    arg: str = ""


def match(text: str) -> Optional[Control]:
    t = (text or "").strip()
    if not t:
        return None
    if _STOP.match(t):
        return Control("stop")
    if _PAUSE_EXPLICIT.match(t):
        return Control("pause")
    if _PAUSE_BARE.match(t):
        return Control("pause_bare")
    if _RESUME.match(t):
        return Control("resume")
    if _WHERE.match(t):
        return Control("where")
    if _NEXT.match(t):
        return Control("next")
    if _FAILURE.match(t):
        return Control("failure")
    m = _REMEMBER.match(t)
    if m:
        return Control("remember", (m.group("as1") or m.group("as2") or "").strip(" .\"“”"))
    if _REDO.match(t):
        return Control("redo")
    m = _MODIFY.match(t)
    if m:
        return Control("modify", (m.group("rest") or m.group("mon") or "").strip())
    return None


def _music_playing() -> bool:
    try:
        from ui.reactive import ui_bus                   # the UI's view of what's playing, if it's up
        return bool(ui_bus.now_playing().get("is_playing"))
    except Exception:
        return False


def handle(text: str) -> Optional[dict]:
    """{"text", "intent", "expects_reply", "ok"} when ``text`` controls a task; else None."""
    c = match(text)
    if c is None:
        return None
    m = agent_tasks
    running = m.running()
    current = m.current()
    out = None
    if c.kind == "pause" and running is not None:
        out = m.pause()
    elif c.kind == "pause_bare" and running is not None and not _music_playing():
        out = m.pause()
    elif c.kind == "resume":
        out = m.resume()
    elif c.kind == "stop" and current is not None:
        out = m.stop_active("you said stop")
    elif c.kind == "where" and (current is not None or _recent(m.last())):
        out = m.where()
    elif c.kind == "next" and current is not None:
        out = m.next_step()
    elif c.kind == "failure":
        out = m.failure()
    elif c.kind == "redo" and _recent(m.last(TaskStatus.FINAL), 600):
        res = m.redo()
        if res is not None:
            return {"text": res["text"], "intent": "agent.task.redo", "expects_reply": res.get("expects_reply", False),
                    "ok": res.get("ok", True)}
    elif c.kind == "remember" and _recent(m.last((TaskStatus.COMPLETED,)), 180):
        out = m.remember(c.arg)
    elif c.kind == "modify" and current is not None:
        out = m.modify(c.arg)
    if out is None:
        return None
    return {"text": out, "intent": f"agent.task.{c.kind}", "expects_reply": False, "ok": True}


def _recent(task, max_age: float = RESUME_MAX_AGE) -> bool:
    return task is not None and time.time() - task.updated < max_age

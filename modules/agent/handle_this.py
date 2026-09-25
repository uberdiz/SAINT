"""
modules/agent/handle_this.py

"SAINT, handle this." SAINT reads the window you're looking at, works out
the single most likely next step (click a button, press a key, type a short
answer, open a link) and ASKS before doing it:

    "The dialog says your download finished. Should I click 'Open folder'?"

One action per request, always confirmed, never offered as a tool to the LLM.
"""

import json
import re
from typing import Optional

from modules.agent.confirm import PendingAction, confirmations
from modules.agent.router import Reply, call

_BLOCKED_KEYS = re.compile(r"alt\s*\+\s*f4|ctrl\s*\+\s*w|win\s*\+\s*l|ctrl\s*\+\s*alt|shift\s*\+\s*del|^del(ete)?$|"
                           r"ctrl\s*\+\s*shift\s*\+\s*esc|win\s*\+\s*r", re.I)


def parse_proposal(raw: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("action") not in ("click", "press", "type", "open_url", "none"):
        return None
    return data


def validate(p: dict, screen_text: str) -> Optional[str]:
    """Why the proposal can't be used (None = fine)."""
    action, target = p.get("action"), str(p.get("target", "")).strip()
    if action == "none":
        return "nothing to do"
    if not target:
        return "no target"
    if action == "click" and target.lower() not in (screen_text or "").lower():
        return "that button isn't on screen"
    if action == "press" and (_BLOCKED_KEYS.search(target) or len(target) > 30):
        return "that key combination could close or lose something"
    if action == "type" and len(target) > 200:
        return "too much to type"
    if action == "open_url" and not re.match(r"^https?://", target):
        return "not a web link"
    return None


def handle_this() -> Reply:
    res = call("screen.read")
    if not res.success:
        return Reply(res.error or "I can't read that window.", ok=False)
    r = res.result or {}
    lines = [x for x in (r.get("text") or []) if x and len(x) < 300][:60]
    window = r.get("window") or "the window"
    if not lines:
        return Reply(f"I can't read anything in {window}, so I don't know what to do there.", ok=False)
    screen_text = "\n".join(lines)
    from modules.agent.llm import complete
    raw = complete(
        f"The user is looking at \"{window}\". Its visible text:\n{screen_text}\n\n"
        "The user says \"handle this\". Choose the ONE most sensible next step to deal with what this window is "
        "asking or showing (dismiss a harmless prompt, accept an update, retry a failed step, answer a simple "
        "question). Never delete, buy, send, sign in, or close unsaved work. Reply with JSON only: "
        '{"action": "click" | "press" | "type" | "open_url" | "none", "target": "<exact button text, keys, '
        'text or URL>", "why": "<one short sentence about what the window shows>"}',
        max_tokens=160)
    p = parse_proposal(raw)
    if p is None:
        return Reply("I read the window but couldn't work out a safe next step.", ok=False)
    problem = validate(p, screen_text)
    why = str(p.get("why", "")).strip().rstrip(".")
    if problem:
        lead = f"{why}. " if why else ""
        return Reply(f"{lead}I don't see a safe step to take for you there.")
    action, target = p["action"], str(p["target"]).strip()
    spoken = {"click": f"click “{target}”", "press": f"press {target}",
              "type": f"type “{target}”", "open_url": f"open {target}"}[action]

    def run():
        tool, kwargs = {"click": ("desktop.click_element", {"name": target}),
                        "press": ("desktop.press_keys", {"keys": target}),
                        "type": ("desktop.type_text", {"text": target}),
                        "open_url": ("desktop.open_url", {"url": target})}[action]
        out = call(tool, **kwargs)
        return f"Done — I {spoken.split(' ', 1)[0]}ed it." if out.success and action == "click" else \
            ("Done." if out.success else (out.error or "That didn't work."))
    confirmations.ask(PendingAction(description=spoken, run=run, tool="handle_this"))
    return Reply(f"{why + '. ' if why else ''}Should I {spoken}?", expects_reply=True)

"""
modules/agent/extras_intents.py

Hands-free extras:

  clipboard   "read my clipboard" · "summarize what I copied" · "translate that into Spanish"
              "fix the code I copied" (result goes back on the clipboard — say "paste")
  workspaces  "save this workspace as Coding" · "restore Coding" · "what workspaces do I have"
  watching    "tell me when Claude finishes" · "tell me when this download finishes"
              "tell me when Steam closes" · "what changed?" · "what changed while I was away?"
              "take me back" · "watch my left screen" / "use all screens" · "stop watching"
  developer   "run the tests" · "open the file causing the error" · "explain the error I copied"
  handle      "handle this" — SAINT proposes one step for the window in front and asks first
"""

import re
from typing import Optional

from modules.agent.router import Intent, Reply, _clean, call, run_tool

_WHICH = r"(left|right|main|primary|second|other|first|third|1|2|3|laptop)"
_CLIP = r"(?:what i (?:just )?copied|(?:my |the )?clipboard|the (?:copied|copy) text|(?:that |the )?text i copied|" \
        r"the code i copied|the error i copied|what'?s on my clipboard)"


def _say(res, fallback: str = "Done.") -> Reply:
    if res.success:
        return Reply((res.result or {}).get("summary") or fallback, expects_reply=bool((res.result or {})
                                                                                       .get("expects_reply")))
    return Reply(res.error or fallback, ok=False)


def parse_extras(text: str) -> Optional[Intent]:
    raw = _clean(text)
    t = raw.lower().strip(" .!?")
    if not t:
        return None
    for parser in (_watch, _notifications, _clipboard, _workspace, _dev, _handle):
        it = parser(t, raw)
        if it is not None:
            return it
    return None


# ---------------------------------------------------------------------- #
# Watching
# ---------------------------------------------------------------------- #
def _watch(t: str, raw: str) -> Optional[Intent]:
    m = re.match(rf"^(?:watch|follow|focus on|use|look at|stick to|work on)\s+(?:my\s+|the\s+)?{_WHICH}\s+"
                 rf"(?:screen|monitor|display)(?:\s+only)?$", t)
    if m:
        which = m.group(1)
        return Intent("watch.follow_screen", lambda: _say(call("watch.follow_screen", which=which)), "watch")
    if re.match(r"^(?:watch|follow|use|look at)\s+(?:both|all|every|all my|all of my)\s+(?:the\s+)?(?:screens|monitors|"
                r"displays)$|^(?:stop following|forget) (?:the|that|my) (?:screen|monitor)$", t):
        return Intent("watch.follow_screen", lambda: _say(call("watch.follow_screen", which="all")), "watch")
    if re.match(r"^what\s+(?:has\s+|'s\s+)?changed(?:\s+on\s+(?:my|the)\s+screens?)?"
                r"(?:\s+(?:while|since)\s+i\s+(?:was\s+)?(?:gone|away|out|left|stepped away))?$|"
                r"^what did i miss$|^what happened while i was (?:gone|away|out)$", t):
        since_left = bool(re.search(r"\b(gone|away|out|left|miss)\b", t))
        return Intent("watch.what_changed", lambda: _say(call("watch.what_changed", since_left=since_left)),
                      "watch")
    m = re.match(r"^what(?:'s| has)?\s+changed\s+in\s+the\s+(?:last|past)\s+(\d+)\s+minutes?$", t)
    if m:
        mins = int(m.group(1))
        return Intent("watch.what_changed", lambda: _say(call("watch.what_changed", minutes=mins)), "watch")
    if re.match(r"^(?:take me back|go back to it|bring (?:it|that) (?:back )?up|show me that window)(?:\s+(?:to it|there|"
                r"now))?$", t):
        from modules.watch.watchers import watch_manager
        if watch_manager.last_fired is not None:
            return Intent("watch.take_me_back", lambda: _say(call("watch.take_me_back")), "watch")
    if re.match(r"^(?:stop|cancel|quit)\s+watching(?:\s+(?:it|that|this|everything|all))?$|^cancel (?:the |all )?watch(?:es)?$",
                t):
        return Intent("watch.cancel", lambda: _say(call("watch.cancel")), "watch")
    if re.match(r"^what\s+are\s+you\s+watching$|^(?:list|show)\s+(?:my\s+)?watch(?:es|ers)$", t):
        return Intent("watch.list", lambda: _say(call("watch.list")), "watch")
    m = re.match(r"^(?:tell me|let me know|ping me|notify me|alert me|say something)\s+(?:when|if|once)\s+(?:my\s+|the\s+|this\s+)?"
                 r"(?P<what>.+?)\s+(?:is\s+)?(?P<how>finishes|finished|done|completes|complete|is done|ready|loads|loaded|"
                 r"stops|downloaded|closes|closed|crashes|crashed|quits|exits|changes|changed|updates|updated)$", t)
    if m:
        what, how = m.group("what").strip(), m.group("how")
        if re.search(r"\bdownload", what) or how == "downloaded":
            return Intent("watch.add", lambda: _say(call("watch.add", kind="download")), "watch")
        kind = "closed" if how in ("closes", "closed", "crashes", "crashed", "quits", "exits") else \
            "change" if how in ("changes", "changed", "updates", "updated") else "finished"
        target = "this" if what in ("this", "it", "that", "this window", "the window", "this one") else \
            re.sub(r"^(?:the|my)\s+", "", what)
        return Intent("watch.add", lambda: _say(call("watch.add", kind=kind, target=target)), "watch")
    if re.match(r"^watch (?:this|that)(?: window)?(?: for (?:me|changes))?$|^keep an eye on (?:this|that|it)$", t):
        return Intent("watch.add", lambda: _say(call("watch.add", kind="change", target="this")), "watch")
    return None


# ---------------------------------------------------------------------- #
# Notifications
# ---------------------------------------------------------------------- #
def _notifications(t: str, raw: str) -> Optional[Intent]:
    if re.match(r"^(?:read|check|show|what are)\s+(?:me\s+)?(?:my\s+|the\s+)?(?:new\s+|latest\s+|recent\s+)?"
                r"notifications$|^(?:do i have\s+)?any\s+(?:new\s+)?notifications$|^what did i get$", t):
        return Intent("notifications.read", lambda: _say(call("notifications.read")), "notifications")
    if re.match(r"^(?:clear|dismiss|mark(?:\s+all)?(?:\s+as)?\s+read)\s+(?:all\s+)?(?:my\s+|the\s+)?notifications$", t):
        return Intent("notifications.clear", lambda: _say(call("notifications.clear")), "notifications")
    if re.match(r"^(?:only\s+(?:tell|notify)\s+me\s+(?:about\s+)?important\s+(?:ones|notifications)|"
                r"only important notifications)$", t):
        return Intent("notifications.set_filter", lambda: _say(call("notifications.set_filter", important_only=True)),
                      "notifications")
    if re.match(r"^(?:tell me about|announce|read out)\s+(?:all|every)\s+(?:my\s+)?notifications?$", t):
        return Intent("notifications.set_filter",
                      lambda: _say(call("notifications.set_filter", important_only=False)), "notifications")
    m = re.match(r"^(?:always\s+tell\s+me\s+about|always announce)\s+(.+?)(?:\s+notifications)?$", t)
    if m:
        app = m.group(1)
        return Intent("notifications.set_filter", lambda: _say(call("notifications.set_filter", allow=app)),
                      "notifications")
    m = re.match(r"^(?:never\s+tell\s+me\s+about|stop\s+telling\s+me\s+about|mute|ignore)\s+(.+?)\s+notifications$", t)
    if m:
        app = m.group(1)
        return Intent("notifications.set_filter", lambda: _say(call("notifications.set_filter", deny=app)),
                      "notifications")
    m = re.match(r"^(?:(?:start|turn on)\s+reading\s+(?:my\s+)?notifications(?:\s+out loud)?|read\s+(?:my\s+)?"
                 r"notifications\s+out\s+loud|announce\s+(?:my\s+)?notifications)$|^(?P<off>(?:stop|turn off)\s+reading"
                 r"\s+(?:my\s+)?notifications(?:\s+out loud)?)$", t)
    if m:
        state = "off" if m.group("off") else "on"
        return Intent("notifications.set_filter", lambda: _say(call("notifications.set_filter", announce=state)),
                      "notifications")
    return None


# ---------------------------------------------------------------------- #
# Clipboard
# ---------------------------------------------------------------------- #
def _clipboard(t: str, raw: str) -> Optional[Intent]:
    if re.match(r"^(?:read|say|tell me)\s+(?:me\s+)?(?:what'?s\s+on\s+)?(?:my\s+|the\s+)?clipboard$|"
                r"^what(?:'s| is)\s+(?:on|in)\s+(?:my\s+|the\s+)?clipboard$|^what did i (?:just )?copy$", t):
        return Intent("clipboard.read", _read_clipboard, "clipboard")
    m = re.match(rf"^(?P<verb>summari[sz]e|explain|proofread|rewrite|shorten|simplify|fix|translate|clean up|"
                 rf"turn)\s+(?P<mid>.*?){_CLIP}(?P<tail>.*)$", t)
    if m:
        verb, tail = m.group("verb"), (m.group("mid") + " " + m.group("tail")).strip()
        lang = re.search(r"\b(?:to|into|in)\s+([a-z]+)\b", tail)
        if verb == "turn" and not re.search(r"\binto\b", tail):
            return None
        mode = {"summarise": "summarize"}.get(verb, verb)
        if verb == "turn":
            mode = "email" if "email" in tail else "rewrite"
        if "code" in t and verb == "fix":
            mode = "fix_code"
        if "error" in t and verb == "explain":
            mode = "explain_error"
        language = lang.group(1) if (verb == "translate" and lang) else ""
        return Intent(f"clipboard.{mode}", lambda: _transform(mode, language), "clipboard")
    m = re.match(r"^translate\s+(?:that|this|it)\s+(?:to|into)\s+([a-z]+)$", t)
    if m:
        language = m.group(1)
        return Intent("clipboard.translate", lambda: _transform("translate", language), "clipboard")
    return None


def _read_clipboard() -> Reply:
    res = call("clipboard.read")
    if not res.success:
        return Reply(res.error, ok=False)
    r = res.result
    if r.get("empty"):
        return Reply("Your clipboard is empty.")
    if r.get("secret"):
        return Reply("That looks like a password or key, so I won't read it out loud.")
    from modules.desktop.clipboard import spoken_preview
    text = r["text"]
    preview = spoken_preview(text)
    if len(preview) < len(" ".join(text.split())):
        return Reply(f"It starts: {preview} It's {len(text):,} characters — say “summarize what I copied” "
                     "for the gist.")
    return Reply(preview)


_PROMPTS = {
    "summarize": "Summarize this in two or three short spoken sentences:",
    "explain": "Explain this simply in two or three short spoken sentences:",
    "explain_error": "This is an error message. In two or three short spoken sentences, say what it means and the "
                     "most likely fix:",
    "proofread": "Fix spelling, grammar and punctuation. Return only the corrected text:",
    "rewrite": "Rewrite this to be clearer. Return only the rewritten text:",
    "shorten": "Make this shorter while keeping the meaning. Return only the new text:",
    "simplify": "Rewrite this in simpler words. Return only the new text:",
    "clean up": "Tidy this text up (spacing, punctuation, obvious typos). Return only the text:",
    "fix": "Fix the mistakes in this. Return only the corrected text:",
    "email": "Turn this into a short, friendly email. Return only the email text:",
    "fix_code": "Fix the bugs in this code. Return only the corrected code in one code block, no explanation:",
}
_SPEAK_ONLY = {"summarize", "explain", "explain_error"}


def _transform(mode: str, language: str = "") -> Reply:
    res = call("clipboard.read")
    if not res.success:
        return Reply(res.error, ok=False)
    r = res.result
    if r.get("empty"):
        return Reply("Your clipboard is empty — copy something first.")
    if r.get("secret"):
        return Reply("That looks like a password or key, so I'll leave it alone.")
    text = r["text"][:6000]
    from modules.agent.llm import complete
    if mode == "translate":
        prompt = (f"Translate this into {language or 'English'}. Return only the translation:\n\n{text}")
    else:
        prompt = f"{_PROMPTS.get(mode, _PROMPTS['rewrite'])}\n\n{text}"
    long_output = mode not in _SPEAK_ONLY
    answer = complete(prompt, max_tokens=1200 if long_output else 0, timeout=90 if long_output else 45)
    if not answer:
        return Reply("The language model didn't answer — is Ollama running?", ok=False)
    if not long_output:
        return Reply(answer)
    from modules.desktop.clipboard import strip_fences
    out = strip_fences(answer) if mode == "fix_code" else answer.strip()
    w = call("clipboard.write", text=out)
    if not w.success:
        return Reply(w.error, ok=False)
    what = {"fix_code": "The fixed code", "translate": f"The {language.capitalize() or 'translated'} version",
            "email": "The email"}.get(mode, "The new version")
    return Reply(f"{what} is on your clipboard — say “paste” to put it where your cursor is.")


# ---------------------------------------------------------------------- #
# Workspaces
# ---------------------------------------------------------------------- #
def _workspace(t: str, raw: str) -> Optional[Intent]:
    m = re.match(r"^(?:save|remember|store|snapshot)\s+(?:this|my|the current|the)?\s*(?:workspace|layout|setup|"
                 r"window layout|desktop(?: setup)?|windows)\s+(?:as|called|named|for)\s+(.+)$", t)
    if m:
        name = raw[-len(m.group(1)):].strip(" .") if len(raw) >= len(m.group(1)) else m.group(1)
        return Intent("workspace.save", lambda: _say(call("workspace.save", name=name)), "workspace")
    if re.match(r"^(?:what|which)\s+(?:workspaces|layouts|setups)\s+(?:do i have|have i saved)$|"
                r"^list\s+(?:my\s+)?(?:workspaces|layouts)$", t):
        def run_list():
            res = call("workspace.list")
            if not res.success:
                return Reply(res.error, ok=False)
            items = res.result["workspaces"]
            if not items:
                return Reply("You haven't saved any workspaces. Arrange your windows and say "
                             "“save this workspace as Coding”.")
            return Reply("You have " + ", ".join(i["name"] for i in items) + ".")
        return Intent("workspace.list", run_list, "workspace")
    m = re.match(r"^(?:forget|delete|remove)\s+(?:the\s+|my\s+)?(.+?)\s+(?:workspace|layout|setup)$", t)
    if m:
        name = m.group(1)
        return Intent("workspace.forget", lambda: run_tool(
            "workspace.forget", f"forget {name}", lambda r: f"Forgot the {name} workspace." if r["forgotten"]
            else f"I don't have a workspace called {name}.", name=name), "workspace")
    m = re.match(r"^(?:restore|load|bring back|set up|open|switch to|go to)\s+(?:my\s+|the\s+)?(.+?)\s+"
                 r"(?:workspace|layout|setup)$", t) or re.match(r"^(?:restore|load|bring back)\s+(?:my\s+|the\s+)?(.+)$", t)
    if m:
        name = m.group(1)
        from modules.workspace.store import workspaces
        if workspaces.get(name) is not None or re.search(r"\b(workspace|layout|setup)$", t):
            return Intent("workspace.restore", lambda: _say(call("workspace.restore", name=name)), "workspace")
    return None


# ---------------------------------------------------------------------- #
# Developer mode
# ---------------------------------------------------------------------- #
def _dev(t: str, raw: str) -> Optional[Intent]:
    if re.match(r"^(?:run|start)\s+(?:the\s+|my\s+|all\s+(?:the\s+)?)?(?:unit\s+)?tests?(?:\s+suite)?(?:\s+again)?$|"
                r"^run the test suite$|^test (?:it|the project)$", t):
        return Intent("dev.run_tests", lambda: run_tool("dev.run_tests", "run the tests",
                                                        lambda r: "Running the tests in a new terminal."), "dev")
    if re.match(r"^(?:open|show me|go to|take me to)\s+(?:the\s+)?(?:file|line|code)\s+(?:that'?s\s+|that is\s+)?"
                r"(?:causing|with|from|behind|for|in)\s+(?:the\s+|this\s+|that\s+)?error$|"
                r"^(?:go|jump|take me) to (?:the )?error$", t):
        def run_open():
            res = call("dev.open_error_file")
            if not res.success:
                return Reply(res.error, ok=False)
            import os
            r = res.result
            return Reply(f"Opened {os.path.basename(r['opened'])} at line {r['line']}.")
        return Intent("dev.open_error_file", run_open, "dev")
    return None


# ---------------------------------------------------------------------- #
# Handle this
# ---------------------------------------------------------------------- #
def _handle(t: str, raw: str) -> Optional[Intent]:
    if re.match(r"^(?:handle|deal with|take care of|sort out)\s+(?:this|that|it)(?:\s+(?:for me|window|popup|dialog))?$",
                t):
        from modules.agent.handle_this import handle_this
        return Intent("handle_this", handle_this, "desktop")
    return None

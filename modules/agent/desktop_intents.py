"""
modules/agent/desktop_intents.py

Natural-language desktop, screen and browser commands, resolved against the
live desktop and the short-term context (modules/agent/context.py).

Commands are recognised by *what they ask for* (verb + object + reference),
not by exact phrases: "make it bigger", "make that window larger" and "make
Spotify bigger" are one intent with a resolved window. References ("it",
"that", "this", "there", "the first one", "my browser", "the second screen")
are resolved from the screen and the conversation. When several windows fit
and nothing disambiguates them SAINT asks which one (ChoiceManager) and then
continues with the original command — including the rest of a multi-step
request.

Every action goes through the tool registry (validation, permissions,
logging) and reports what actually happened: tools verify their effect
(focus, position, title change after a click or navigation) and the reply
says so instead of assuming success.
"""

import logging
import re
import time
from typing import Callable, Optional

from modules.agent.confirm import choices, PendingChoice, ChoiceOption
from modules.agent.context import desktop_context
from modules.agent.router import Intent, Reply, call, run_tool, _clean

log = logging.getLogger("saint.agent")

_SCREEN = r"(?:screen|monitor|display)"
_WHICH_MON = r"(?:second|other|2nd|left|right|main|primary|first|1st|third|3rd|1|2|3|secondary|laptop)"
_THIS = {"it", "that", "this", "this window", "that window", "the window", "that one", "this one", "window",
         "the window i'm looking at", "the window i am looking at", "the current window", "the active window",
         "what i'm looking at", "the one i'm looking at", "there"}


def _win(ref: str) -> str:
    """Spoken window reference -> controller query."""
    r = (ref or "").strip().lower()
    r = re.sub(r"\s+(window|app)$", "", r) if r not in _THIS else r
    if r in ("it", "that", "that window", "that one", "there"):
        return "it"
    if r in _THIS or not r:
        return "this"
    r = re.sub(r"^(the|my)\s+", "", r)
    return r


def _title(r) -> str:
    t = (r or {}).get("title") or (r or {}).get("window") or ""
    t = re.sub(r"^\(\d+\)\s*", "", t)
    return (t.split(" - ")[-1] or t)[:50] if t else "it"


# ---------------------------------------------------------------------- #
# Asking which window
# ---------------------------------------------------------------------- #
def _ask_which(retry: Callable[[], Reply]) -> Optional[Reply]:
    """If the last tool failed because several windows fit, ask which one and
    continue with ``retry`` once the user answers."""
    from modules.desktop import controller
    amb = controller.last_ambiguity
    if not amb or time.time() - amb[2] > 5:
        return None
    what, cands, _ = amb
    controller.last_ambiguity = None
    from modules.vision.screen import app_name
    d = controller.desktop
    mons = d.monitors()
    opts = []
    for i, w in enumerate(cands[:6]):
        title = re.sub(r"^\(\d+\)\s*", "", w.title)
        title = re.sub(r"\s*[-–]\s*(Opera|Google Chrome|Microsoft Edge|Mozilla Firefox|Brave)$", "", title)
        where = ""
        if len(mons) > 1:
            m = next((x for x in mons if x.index == w.monitor), None)
            where = f" on {d.monitor_label(m).replace('your ', 'your ')}" if m else ""
        opts.append(ChoiceOption(label=f"{title[:45]}{where}",
                                 keywords=f"{title} {app_name(w.process)} monitor {w.monitor} "
                                          f"{'main primary' if m and m.primary else 'second other'}",
                                 value=w.hwnd))
    spoken = "; ".join(f"{i + 1}, {o.label}" for i, o in enumerate(opts))
    question = f"I found {len(cands)} {what} windows: {spoken}. Which one should I use?"

    def chosen(hwnd):
        try:
            d._activate(d._info(hwnd))
        except Exception as e:
            return f"I couldn't switch to that window: {e}"
        return retry().text

    choices.ask(PendingChoice(question, opts, chosen))
    return Reply(question, ok=True, expects_reply=True)


def _tool(tool: str, describe: str, on_ok, retry: Callable[[], Reply] = None, **kw) -> Reply:
    rep = run_tool(tool, describe, on_ok, **kw)
    if not rep.ok and retry is not None:
        asked = _ask_which(retry)
        if asked:
            return asked
    return rep


# ---------------------------------------------------------------------- #
# Screen understanding
# ---------------------------------------------------------------------- #
def _describe(monitor=None, question: str = "") -> Reply:
    res = call("screen.context", monitor=monitor) if monitor else call("screen.context")
    if not res.success:
        return Reply(res.error, ok=False)
    text = res.result.get("summary") or ""
    if not monitor:
        an = call("screen.analyze", question=question or "Briefly describe what the user is looking at.")
        if an.success and an.result.get("answer"):
            text += " " + an.result["answer"]
    desktop_context.note_domain("desktop")
    return Reply(text or "I can't see anything I can describe there.")


def _whats_open() -> Reply:
    res = call("screen.context")
    if not res.success:
        return Reply(res.error, ok=False)
    from modules.vision.screen import _short_title
    parts = []
    for m in res.result["monitors"]:
        names = [_short_title(w) for w in m["windows"]]
        if names:
            parts.append(f"On {m['label']}: " + ", ".join(names[:6]))
    mins = [_short_title(w) for w in res.result.get("minimized", [])]
    if mins:
        parts.append("minimized: " + ", ".join(mins[:5]))
    return Reply(". ".join(parts) + "." if parts else "Nothing is open right now.")


def _explain_screen(question: str) -> Reply:
    """'What is this error?' — read the window's text and explain it."""
    res = call("screen.context")
    if not res.success:
        return Reply(res.error, ok=False)
    ctx = res.result
    ui = ctx.get("ui") or {}
    sw = ctx.get("subject_window") or {}
    lines = (ui.get("dialogs") or []) + (ui.get("text") or [])
    if not lines:
        return Reply(f"I can't read any text in {sw.get('app', 'that window')}, so I can't tell what the "
                     "message says.", ok=False)
    # Only explain what is actually there: a small model will invent an error
    # if handed unrelated page text, so look for error-like text first.
    errorish = re.compile(r"\b(error|failed|failure|exception|warning|denied|not found|invalid|cannot|can't|"
                          r"couldn't|could not|unable|problem|crash|stopped|missing|timed out|refused|"
                          r"unexpected|fatal|traceback|0x[0-9a-f]{6,})\b", re.I)
    hits = [i for i, x in enumerate(lines) if errorish.search(x)]
    if not hits and not re.search(r"\b(error|warning|alert|problem)\b", sw.get("title", ""), re.I):
        from modules.vision.screen import _short_title
        return Reply(f"I don't see an error or warning in {_short_title(sw)} right now.")
    keep = sorted({j for i in hits for j in (i - 1, i, i + 1, i + 2) if 0 <= j < len(lines)}) or range(len(lines))
    lines = [lines[j] for j in keep]
    from modules.agent.llm import complete
    prompt = (f"The user is looking at a window titled \"{sw.get('title', '')}\" ({sw.get('app', '')}). "
              f"Its visible text is:\n" + "\n".join(f"- {x}" for x in lines[:25]) +
              f"\n\nThe user asks: \"{question}\". Answer in two or three short spoken sentences: what it "
              "means and what to do. Only use the text above; if it doesn't say, say you can't tell.")
    answer = complete(prompt)
    return Reply(answer or "I read the window, but I couldn't work out what it means.", ok=bool(answer))


# ---------------------------------------------------------------------- #
# Element actions
# ---------------------------------------------------------------------- #
def _click(target: str, action: str = "click", window_hwnd: Optional[int] = None) -> Reply:
    verb = {"click": "Clicked", "double_click": "Double-clicked", "right_click": "Right-clicked",
            "middle_click": "Middle-clicked", "hover": "Hovering over"}[action]

    def ok(r):
        name = r.get("clicked") or target
        if action in ("click", "double_click") and not r.get("changed"):
            return f"{verb} {name}, but nothing visibly changed yet."
        return f"{verb} {name}."

    if window_hwnd:
        from modules.desktop import uia
        with uia.in_window(window_hwnd):
            return _tool("desktop.click_element", f"{action.replace('_', ' ')} {target}", ok,
                         name=target, action=action)
    return _tool("desktop.click_element", f"{action.replace('_', ' ')} {target}", ok,
                 retry=lambda: _click(target, action), name=target, action=action)


def _act_on_last(action: str) -> Reply:
    el = desktop_context.element()
    if el is None:
        return Reply("I'm not sure what you mean — tell me what to click.", ok=False)
    name, x, y = el
    if action == "hover":
        r = call("desktop.mouse_move", x=x, y=y)
    else:
        button = {"right_click": "right", "middle_click": "middle"}.get(action, "left")
        r = call("desktop.mouse_click", x=x, y=y, button=button, clicks=2 if action == "double_click" else 1)
    if not r.success:
        return Reply(r.error, ok=False)
    return Reply({"hover": f"Hovering over {name}.", "click": f"Clicked {name}.",
                  "double_click": f"Double-clicked {name}.", "right_click": f"Right-clicked {name}.",
                  "middle_click": f"Middle-clicked {name}."}[action])


def _locate(what: str) -> Reply:
    res = call("screen.locate", name=what)
    if not res.success:
        return Reply(res.error, ok=False)
    r = res.result
    win = r["window"].split(" - ")[-1] or "the window"
    place = f"at the {r['where']} of {win}" if r.get("where") else f"on {win}"
    return Reply(f"Found {r['name'] or what} {place}. Say \"click it\" if you want me to.")


# ---------------------------------------------------------------------- #
# Volume: Spotify vs. the computer, from context
# ---------------------------------------------------------------------- #
def _system_volume(direction: str, steps: int = 5) -> Reply:
    key = {"up": "volumeup", "down": "volumedown", "mute": "volumemute"}[direction]
    import pyautogui
    from modules.desktop.controller import _require
    try:
        _require("allow_keyboard", "Keyboard control")
    except Exception as e:
        return Reply(str(e), ok=False)
    pyautogui.press(key, presses=1 if direction == "mute" else steps, interval=0.02)
    return Reply({"up": "Turned the volume up.", "down": "Turned the volume down.",
                  "mute": "Toggled mute."}[direction])


# ---------------------------------------------------------------------- #
# Parser
# ---------------------------------------------------------------------- #
def parse(text: str) -> Optional[Intent]:
    raw = _clean(text)
    t = raw.lower().strip(" ?!.")
    t = re.sub(r"\s+", " ", t)

    # ---- "in that window, <command>" / "in my browser, <command>" -----------------
    m = re.match(r"^in (that|this|the|my) (window|browser|tab|app|spotify|chrome|opera)[,:]? (.+)$", t)
    if m:
        inner_raw = raw[raw.lower().index(m.group(3)):] if m.group(3) in raw.lower() else m.group(3)
        ref = "it" if m.group(1) == "that" else ("browser" if m.group(2) in ("browser", "tab", "chrome", "opera")
                                               else ("spotify" if m.group(2) == "spotify" else "this"))
        cm = re.match(r"^(double[- ]?click|right[- ]?click|click|tap|press|select|open|hover over)(?: on)? (?:the )?(.+)$",
                      inner_raw.lower())
        if cm:
            action = "double_click" if cm.group(1).startswith("double") else \
                "right_click" if cm.group(1).startswith("right") else "hover" if cm.group(1).startswith("hover") \
                else "click"
            target = cm.group(2)

            def run_in():
                from modules.desktop.controller import desktop
                try:
                    w = desktop.find_window(ref)
                except Exception as e:
                    asked = _ask_which(run_in)
                    return asked or Reply(str(e), ok=False)
                return _click(target, action, window_hwnd=w.hwnd)
            return Intent("desktop.click_in_window", run_in, "desktop")

    # ---- screen questions --------------------------------------------------------------
    m = re.search(rf"^(?:what(?:'s| is| do i have)?|what's|show me what's|tell me what's|describe what's|describe)\s+"
                  rf"(?:currently |open |showing |up |going on )?(?:on|open on|showing on|up on)\s+(?:my|the)\s+"
                  rf"({_WHICH_MON})\s+{_SCREEN}", t) or \
        re.search(rf"^(?:describe|read|look at|check)\s+(?:my|the)\s+({_WHICH_MON})\s+{_SCREEN}", t)
    if m:
        which = m.group(1)
        if t.startswith("read"):
            return None
        return Intent("screen.monitor", lambda: _describe(monitor=which), "desktop")
    if re.search(r"^(?:what(?:'s| is)|what are|which)\s+(?:currently\s+)?(?:open|running)(?: right now)?$|"
                 r"^what (?:windows|apps|applications|programs) (?:are|do i have) (?:open|running)", t):
        return Intent("screen.open_windows", _whats_open, "desktop")
    if re.search(r"^(?:what(?:'s| is| does)|explain|why)\b.*\b(?:this|that|the)\s+(?:error|warning|message|dialog|popup|"
                 r"pop-up|notification|prompt)\b", t) or re.search(r"^what does (?:this|that) (?:mean|say)$", t):
        q = raw
        return Intent("screen.explain", lambda: _explain_screen(q), "desktop")
    m = re.match(r"^(?:find|locate|look for|where(?:'s| is))\s+(?:the\s+)?(.+?)(?:\s+(?:button|link|field|box|icon|"
                 r"menu|tab|option|toggle|switch))?(?:\s+on (?:my|the) screen)?$", t)
    if m and (t.startswith(("find", "locate", "look for")) or re.search(r"\b(button|link|field|box|icon|menu|tab|"
                                                                        r"option|toggle|switch|search)\b", t)):
        what = re.sub(r"^(find|locate|look for|where's|where is)\s+(the\s+)?", "", t)
        what = re.sub(r"\s+on (my|the) screen$", "", what)
        if not re.search(r"\b(my phone|my keys|file|folder|document)\b", what):
            return Intent("screen.locate", lambda: _locate(what), "desktop")

    if re.match(r"^(?:summari[sz]e|sum up|give me (?:a |the )?(?:summary|gist|tl;?dr) of|tl;?dr)\s+"
                r"(?:(?:everything|all(?: the text)?|what(?:'s| is))\s+(?:on|in)\s+)?(?:(?:this|the|my|that)\s+)?(?:current\s+)?(?:web\s*)?(?:page|website|site|article|window|tab|"
                r"screen)(?:\s+i'?m on| i am on)?$|^what(?:'s| is) (?:this|the) (?:page|article|website) about$|"
                r"^tl;?dr(?: this)?$", t):
        return Intent("screen.summarize", _summarize_screen, "desktop")

    # ---- hide everything except X ------------------------------------------------------------
    m = re.match(r"^(?:hide|minimi[sz]e|close)\s+(?:everything|all(?: (?:the|my|of my|other))?(?: (?:windows|tabs|apps|"
                 r"programs|other windows))?|every(?: other)? window)(?: else)?(?: on (?:my|the) screen)?(?: that'?s open)?"
                 r"\s+(?:but|except(?: for)?|besides|apart from|other than|aside from|save)\s+(?:the\s+|my\s+)?(.+)$", t) \
        or re.match(r"^(?:show|leave|keep)\s+(?:me\s+)?only\s+(?:the\s+|my\s+)?(.+?)(?:\s+(?:open|visible|on (?:my|the) "
                    r"screen))?$", t) \
        or re.match(r"^(?:focus on|leave) (?:just|only) (?:the\s+|my\s+)?(.+)$", t)
    if m:
        keep = re.sub(r"\s+(?:open|visible|window|windows)$", "", m.group(1).strip())

        def run_only():
            def ok(r):
                what = " and ".join(r["kept"]) or keep
                note = f" I couldn't find {', '.join(r['missing'])}." if r.get("missing") else ""
                return (f"Hid {r['minimized']} window{'s' if r['minimized'] != 1 else ''}; {what} is still up."
                        if r["minimized"] else f"Only {what} was open already.") + note
            return run_tool("desktop.minimize_others", f"hide everything except {keep}", ok, keep=keep)
        return Intent("desktop.minimize_others", run_only, "desktop")

    # ---- a new browser tab ("open a new tab in my browser") -----------------------------------
    if re.match(r"^(?:open|make|start|create|pull up)?\s*(?:up\s+)?(?:a\s+)?(?:new|fresh|blank)\s+tab"
                r"(?:\s+(?:in|on)\s+(?:my|the)\s+(?:web\s+)?(?:browser|chrome|opera|edge|firefox))?$", t) \
            or re.match(r"^(?:open|make|start)\s+(?:up\s+)?(?:a\s+)?(?:new\s+)?tab\s+(?:in|on)\s+(?:my|the)\s+"
                        r"(?:web\s+)?browser$", t):
        def run_new_tab():
            desktop_context.note_domain("browser")
            return run_tool("desktop.new_tab", "open a new tab",
                            lambda r: "Started your browser." if r.get("started") else "Opened a new tab.")
        return Intent("browser.new_tab", run_new_tab, "browser")

    # ---- act on the element SAINT just found ----------------------------------------------
    m = re.match(r"^(click|double[- ]?click|right[- ]?click|middle[- ]?click|hover over|hover on|tap|press)"
                 r"(?: on)? (it|that|that one|there|this)$", t)
    if m:
        v = m.group(1)
        action = "double_click" if v.startswith("double") else "right_click" if v.startswith("right") else \
            "middle_click" if v.startswith("middle") else "hover" if v.startswith("hover") else "click"
        return Intent("desktop.click_last", lambda: _act_on_last(action), "desktop")

    # ---- element clicks with variants ----------------------------------------------------
    m = re.match(r"^(double[- ]?click|right[- ]?click|middle[- ]?click|hover over|hover on|mouse over)"
                 r"(?: on)? (?:the )?(.+)$", t)
    if m:
        v, target = m.group(1), m.group(2)
        action = "double_click" if v.startswith("double") else "right_click" if v.startswith("right") else \
            "middle_click" if v.startswith("middle") else "hover"
        return Intent("desktop.click_element", lambda: _click(target, action), "desktop")
    m = re.match(r"^(?:click|tap|select|open|play|choose|pick)(?: on)? (?:the )?(first|second|third|fourth|fifth|"
                 r"1st|2nd|3rd|4th|5th|top|last)\s+(video|result|link|search result|song|item|one|article|post)s?$", t)
    if m:
        target = f"{m.group(1)} {m.group(2)}"
        return Intent("desktop.click_result", lambda: _click(target), "desktop")
    m = re.match(r"^click(?: on)? (?:the )?((?:button|link|icon|thing|one)\s+)?(?:in|at|on) (?:the )?(.+)$", t)
    if m and re.search(r"\b(top|bottom|upper|lower|left|right|middle|center|corner)\b", m.group(2)):
        target = t[len("click"):].strip()
        target = re.sub(r"^on\s+", "", target)
        return Intent("desktop.click_region", lambda: _click(target), "desktop")

    # ---- scrolling ----------------------------------------------------------------------
    m = re.match(r"^(?:scroll|go|page)\s+(down|up)(?:\s+(a lot|a little|a bit|more|some|further|all the way))?"
                 r"(?:\s+(?:on|in) (?:the |this |that )?(?:page|window))?$", t) or \
        re.match(r"^(?:scroll)(?: the page)?(?: (down|up))?$", t)
    if m:
        d = (m.group(1) or "down")
        amt = (m.lastindex or 0) >= 2 and m.group(2) or ""
        if "all the way" in (amt or ""):
            key = "end" if d == "down" else "home"
            return Intent("desktop.scroll", lambda: run_tool("desktop.press_keys", f"press {key}",
                                                             lambda r: f"Scrolled to the {'bottom' if d == 'down' else 'top'}.",
                                                             keys=key), "desktop")
        n = {"a lot": 15, "a little": 3, "a bit": 3, "more": 8, "some": 6, "further": 8}.get(amt, 6)
        amount = -n if d == "down" else n
        return Intent("desktop.scroll", lambda: run_tool("desktop.scroll", f"scroll {d}",
                                                         lambda r: f"Scrolled {d}.", amount=amount), "desktop")
    if re.match(r"^(?:scroll|go) to the (top|bottom)(?: of the page)?$", t):
        top = "top" in t
        return Intent("desktop.scroll", lambda: run_tool("desktop.press_keys", "scroll", lambda r:
                                                         f"At the {'top' if top else 'bottom'}.",
                                                         keys="home" if top else "end"), "desktop")

    # ---- navigation / editing keys (resolved to shortcuts, target window verified) ------------
    keymap = [
        (r"^(?:go )?back(?: a page)?$|^go to the previous page$|^previous page$", "alt+left", "Went back."),
        (r"^(?:go )?forward(?: a page)?$|^next page$", "alt+right", "Went forward."),
        (r"^(?:refresh|reload)(?: (?:the|this) (?:page|tab))?$", "f5", "Reloaded."),
        (r"^(?:close|shut) (?:this|the|that) tab$", "ctrl+w", "Closed the tab."),
        (r"^(?:open )?(?:a )?new tab$", "ctrl+t", "Opened a new tab."),
        (r"^(?:reopen|restore) (?:the )?(?:last |closed )?tab$", "ctrl+shift+t", "Reopened the tab."),
        (r"^(?:next|switch) tab$|^go to the next tab$", "ctrl+tab", "Next tab."),
        (r"^previous tab$", "ctrl+shift+tab", "Previous tab."),
        (r"^select (?:all|everything)(?: the text)?$", "ctrl+a", "Selected everything."),
        (r"^copy(?: (?:it|that|this|the text|the selection))?$", "ctrl+c", "Copied."),
        (r"^paste(?: (?:it|that|this))?(?: (?:here|in there))?$", "ctrl+v", "Pasted."),
        (r"^cut(?: (?:it|that|this))?$", "ctrl+x", "Cut."),
        (r"^undo(?: (?:that|it))?$", "ctrl+z", "Undone."),
        (r"^redo(?: (?:that|it))?$", "ctrl+y", "Redone."),
        (r"^(?:zoom in|make (?:the )?text bigger)$", "ctrl+plus", "Zoomed in."),
        (r"^(?:zoom out|make (?:the )?text smaller)$", "ctrl+minus", "Zoomed out."),
        (r"^(?:reset zoom|actual size)$", "ctrl+0", "Zoom reset."),
        (r"^(?:exit|leave|get out of) full ?screen$", "esc", "Left full screen."),
        (r"^(?:show (?:the )?desktop|minimi[sz]e everything|hide (?:all|every) windows?)$", "win+d", "Showing the desktop."),
        (r"^(?:switch|go back) to the (?:last|previous) (?:window|app)$", "alt+tab", "Switched."),
        (r"^(?:pause|play|resume) (?:the )?video$", "k", "Done."),
    ]
    for pat, keys, said in keymap:
        if re.search(pat, t):
            return Intent("desktop.keys", lambda keys=keys, said=said: run_tool(
                "desktop.press_keys", f"press {keys}", lambda r: said, keys=keys), "desktop")
    if re.match(r"^(?:put (?:it|this|that|the video) in |go |make (?:it|this|the video) |enter |switch to )?"
                r"full ?screen(?: mode)?$", t):
        def run_fs():
            from modules.desktop.controller import desktop
            w = desktop.target_window()
            video = w is not None and desktop.is_browser(w) and re.search(r"youtube|twitch|netflix|vimeo", w.title, re.I)
            key = "f" if video else "f11"
            return run_tool("desktop.press_keys", "go full screen", lambda r: "Full screen.", keys=key)
        return Intent("desktop.fullscreen", run_fs, "desktop")

    # ---- media keys for a video when the conversation is about the browser -----------------
    if t in ("pause", "resume", "play", "unpause", "pause it", "play it", "resume it", "stop the video")             and desktop_context.domain() == "browser":
        def run_media():
            from modules.desktop.controller import desktop
            w = desktop.target_window()
            yt = w is not None and re.search(r"youtube", w.title, re.I)
            word = "Paused" if t.startswith(("pause", "stop")) else "Resumed"
            return run_tool("desktop.press_keys", "pause the video", lambda r: f"{word} the video.",
                            keys="k" if yt else "space")
        return Intent("desktop.media", run_media, "browser")

    # ---- volume: the computer (Spotify handles music volume when that's the context) --------
    m = re.match(r"^(?:turn|put|bring)\s+(?:the\s+)?(?:(?:system|computer|pc|video|youtube)\s+)?(?:volume|sound|audio)\s+"
                 r"(up|down)(?:\s+(?:a (?:lot|bit|little)))?$|^(?:turn|put)\s+(?:it|the video)\s+(up|down)$|"
                 r"^(?:turn|bring)\s+(up|down)\s+(?:the\s+)?(?:(?:system|computer|pc|video|youtube)\s+)?"
                 r"(?:volume|sound|audio)(?:\s+(?:a (?:lot|bit|little)))?$|"
                 r"^(lower|raise|reduce|increase)\s+(?:the\s+)?(?:(?:system|computer|pc|video|youtube)\s+)?"
                 r"(?:volume|sound|audio)(?:\s+(?:a (?:lot|bit|little)))?$|"
                 r"^(?:volume|sound)\s+(up|down)$|^(?:make\s+(?:it|this|that|the\s+(?:sound|volume|audio|video))\s+)?(louder|quieter|softer)"
                 r"(?:\s+(?:a (?:lot|bit|little)|please))?$|^(mute|unmute)(?: (?:the )?(?:sound|audio|video|computer))?$", t)
    if m and not desktop_context.music_is_context() and not re.search(r"\b(music|song|spotify|track)\b", t):
        word = next(g for g in m.groups() if g)
        direction = {"louder": "up", "quieter": "down", "softer": "down", "mute": "mute", "unmute": "mute",
                     "lower": "down", "reduce": "down", "raise": "up", "increase": "up"}.get(word, word)
        steps = 10 if "a lot" in t else (2 if re.search(r"a (bit|little)", t) else 5)
        return Intent("desktop.volume", lambda: _system_volume(direction, steps), "desktop")

    # ---- window size / placement ----------------------------------------------------------
    m = re.match(r"^make (?:the )?(.+?) (bigger|larger|smaller|wider|narrower|huge|tiny)$", t)
    if m:
        ref, how = _win(m.group(1)), m.group(2)
        direction = "bigger" if how in ("bigger", "larger", "wider", "huge") else "smaller"
        return Intent("desktop.scale_window", lambda: _tool(
            "desktop.scale_window", f"make {ref} {direction}",
            lambda r: f"Made {_title(r)} {direction}.", retry=None, window=ref, direction=direction), "desktop")
    m = re.match(r"^(?:put|move|place|snap|dock)\s+(?:the\s+)?(.+?)\s+(?:right\s+)?(?:next to|beside|alongside)\s+"
                 r"(?:the\s+|my\s+)?(.+?)(?:\s+on the (left|right))?$", t)
    if m:
        a, b, side = _win(m.group(1)), _win(m.group(2)), m.group(3) or "right"
        return Intent("desktop.place_beside", lambda: _tool(
            "desktop.place_beside", f"put {a} next to {b}",
            lambda r: f"Put {_title({'title': r['window']})} next to {_title({'title': r['beside']})}.",
            window=a, other=b, side=side), "desktop")
    m = re.match(r"^(?:move|put|drag|throw|send)\s+(?:the\s+)?(.+?)\s+(?:over\s+)?(?:there|over there|to the other side)$", t)
    if m:
        ref = _win(m.group(1))

        def run_there():
            from modules.desktop.controller import desktop
            import pyautogui
            p = pyautogui.position()
            mon = next((mm for mm in desktop.monitors() if mm.left <= p.x < mm.right and mm.top <= p.y < mm.bottom), None)
            try:
                w = desktop.find_window(ref)
            except Exception as e:
                return Reply(str(e), ok=False)
            target = str(mon.index) if mon and mon.index != w.monitor else "next"
            return run_tool("desktop.move_window", f"move {ref}", lambda r: f"Moved {_title(r)} over.",
                            window=f"hwnd:{w.hwnd}", monitor=target)
        return Intent("desktop.move_window", run_there, "desktop")
    m = re.match(rf"^(?:move|put|send|throw|drag)\s+(?:the\s+|my\s+)?(.+?)\s+(?:window\s+)?(?:to|onto|on)\s+(?:the\s+|my\s+)?"
                 rf"({_WHICH_MON}|next|other)\s+{_SCREEN}(?:\s+(\d))?$", t)
    if m:
        ref, which = _win(m.group(1)), m.group(3) or m.group(2)
        return Intent("desktop.move_window", lambda: _tool(
            "desktop.move_window", f"move {ref} to the {which} screen",
            lambda r: f"Moved {_title(r)} to {_mon_label(r.get('monitor'))}.",
            retry=lambda: _tool("desktop.move_window", "move it", lambda r: f"Moved {_title(r)} to "
                                f"{_mon_label(r.get('monitor'))}.", window="it", monitor=which),
            window=ref, monitor=which), "desktop")
    m = re.match(r"^(?:put|move|snap|push|dock)\s+(?:the\s+|my\s+)?(.+?)\s+(?:window\s+)?(?:on|to|over to)\s+the\s+"
                 r"(left|right)(?:\s+(?:side|half))?(?:\s+of\s+(?:the|my)\s+(?:screen|monitor|display))?$", t)
    if m:
        ref, side = _win(m.group(1)), m.group(2)
        return Intent("desktop.arrange_window", lambda: _tool(
            "desktop.arrange_window", f"snap {ref} {side}", lambda r: f"Put {_title(r)} on the {side}.",
            retry=lambda: _tool("desktop.arrange_window", "snap it", lambda r: f"Put {_title(r)} on the {side}.",
                                window="it", action=f"snap_{side}"),
            window=ref, action=f"snap_{side}"), "desktop")
    m = re.match(r"^(maximi[sz]e|minimi[sz]e|restore|center|centre)\s+(?:the\s+|my\s+)?(.+?)$", t)
    if m and _win(m.group(2)) in ("it", "this", "browser"):
        verb, ref = m.group(1), _win(m.group(2))
        action = {"maxim": "maximize", "minim": "minimize", "resto": "restore", "cente": "center",
                  "centr": "center"}[verb[:5]]
        return Intent("desktop.arrange_window", lambda: _tool(
            "desktop.arrange_window", f"{action} {ref}",
            lambda r: {"maximize": "Maximized", "minimize": "Minimized", "restore": "Restored",
                       "center": "Centered"}[action] + f" {_title(r)}.",
            retry=lambda: _tool("desktop.arrange_window", action, lambda r: "Done.", window="it", action=action),
            window=ref, action=action), "desktop")

    # ---- close ---------------------------------------------------------------------------
    m = re.match(r"^close (?:all|every one of|each of) (?:the |my )?(.+?) windows?$|^close all (?:the |my )?(.+?)s$", t)
    if m:
        app = (m.group(1) or m.group(2)).strip()
        app = "browser" if app in ("browser", "web browser", "internet") else app
        return Intent("desktop.close_windows", lambda: run_tool(
            "desktop.close_windows", f"close all your {app} windows",
            lambda r: f"Closed {r['closed']} of {r['of']} {app} windows." if r["closed"] != r["of"]
            else f"Closed all {r['closed']} {app} windows.", app=app), "desktop")
    m = re.match(r"^close (the window i'?m looking at|the window i am looking at|what i'?m looking at|"
                 r"(?:the |my )?browser|it|that|this|that window|this window)$", t)
    if m:
        ref = _win(m.group(1))

        def run_close():
            def ok(r):
                if r.get("closed"):
                    return f"Closed {_title(r)}."
                return f"I asked {_title(r)} to close, but it's still open — {r.get('note', '')}".strip()
            return _tool("desktop.close_app", f"close {ref if ref not in ('it', 'this') else 'that window'}", ok,
                         retry=lambda: _tool("desktop.close_app", "close it", ok, name="it"), name=ref)
        return Intent("desktop.close_app", run_close, "desktop")

    # ---- browser -------------------------------------------------------------------------
    from modules.desktop import browser
    m = re.match(r"^(?:search|look up|find|google)\s+(?:on\s+)?(youtube|google|amazon|wikipedia|reddit|github|twitch|"
                 r"bing|duckduckgo|ebay|google maps|maps|images|the web)\s+(?:for\s+)?(.+)$", t) or \
        re.match(r"^(?:search|look up|find|search for)\s+(.+?)\s+on\s+(youtube|google|amazon|wikipedia|reddit|github|"
                 r"twitch|bing|duckduckgo|ebay|google maps|maps)$", t) or \
        re.match(r"^(youtube|google|amazon|wikipedia|reddit|github)\s+search\s+(?:for\s+)?(.+)$", t)
    if m:
        a, b = m.group(1), m.group(2)
        site, query = (a, b) if a in browser.SEARCH_URLS else (b, a)
        q_raw = raw[raw.lower().find(query):raw.lower().find(query) + len(query)] if query in raw.lower() else query

        def run_search():
            desktop_context.note_domain("browser")
            return _tool("desktop.web_search", f"search {site} for {q_raw}",
                         lambda r: (f"Searched {site.title()} for {q_raw}." if r.get("verified")
                                    else f"I typed the {site.title()} search for {q_raw}, but the page hasn't changed yet."),
                         retry=run_search, query=q_raw, site=site)
        return Intent("browser.search", run_search, "browser")
    # "search for X" with no site: search the site the browser is on (YouTube,
    # Amazon, ...) — decided when it runs, so it works inside multi-step commands.
    m = re.match(r"^(?:search|look up|search for|look for)\s+(?:for\s+)?(.+)$", t)
    if m and not re.search(r"\b(spotify|song|playlist|album|my files?|folder|on my computer|reminders?)\b", t)             and not browser.site_url(m.group(1).strip()):
        query = m.group(1).strip()
        q_raw = raw[len(raw) - len(query):] if raw.lower().endswith(query) else query

        def run_here():
            from modules.desktop.controller import desktop
            site = "google"
            ref = desktop_context.window()
            try:
                w = desktop._info(ref) if ref else desktop.target_window()
            except Exception:
                w = None
            if w is not None and desktop.is_browser(w):
                title = w.title.lower()
                site = next((sname for sname in browser.SEARCH_URLS
                             if sname not in ("web", "the web", "images", "maps") and sname in title), "google")
            desktop_context.note_domain("browser")
            return _tool("desktop.web_search", f"search {site} for {q_raw}",
                         lambda r: f"Searched {site.title()} for {q_raw}." if r.get("verified")
                         else f"I entered the search for {q_raw}, but the page hasn't changed yet.",
                         retry=run_here, query=q_raw, site=site)
        return Intent("browser.search", run_here, "browser")
    m = re.match(r"^(?:go to|navigate to|visit|browse to|pull up|bring up|open(?: up)?|search|load)\s+(?:the\s+)?"
                 r"(?:website\s+)?(.+?)(?:\s+(?:website|site|page|dot com))?(?:\s+in (?:my|the) browser)?$", t)
    if m:
        site = m.group(1).strip()
        url = browser.site_url(site)
        # "open X" stays "open the app X" unless X is a website and not an installed app.
        if url and (not t.startswith(("open", "search", "bring up", "pull up")) or _not_an_app(site)):
            def run_site():
                desktop_context.note_domain("browser")
                return _tool("desktop.open_url", f"open {site}",
                             lambda r: (f"Opened {r['site']}." if r.get("verified") or not r.get("reused")
                                        else f"I entered {r['site']}, but the page hasn't loaded yet."),
                             retry=run_site, url=url)
            return Intent("browser.open_url", run_site, "browser")
    # "Open my browser with <X> on it" / "open my browser to <X>" — treat as
    # open-browser + navigate/search, so the app resolver doesn't try to open
    # an app called "browser with bloodhounds on it".
    m = re.match(r"^(?:open|launch|bring up|pull up)\s+(?:a\s+)?(?:my\s+|the\s+|a\s+)?"
                 r"(?:web\s+)?browser(?:\s+window)?\s+(?:with|to|and (?:go to|search|open))\s+(.+?)"
                 r"(?:\s+(?:on it|please))?$", t)
    if m:
        target = m.group(1).strip()

        def run_browser_with():
            desktop_context.note_domain("browser")
            from modules.desktop.browser import site_url
            url = site_url(target)
            if url:
                return _tool("desktop.open_url", f"open {target}",
                             lambda r: f"Opened {r.get('site', target)}.",
                             retry=run_browser_with, url=url)
            # Fall back to a search on the target text.
            import urllib.parse
            return _tool("desktop.open_url", f"search for {target}",
                         lambda r: f"Searched for {target}.",
                         retry=run_browser_with,
                         url="https://duckduckgo.com/?q=" + urllib.parse.quote_plus(target))
        return Intent("browser.open_url", run_browser_with, "browser")

    m = re.match(r"^(?:open|launch|start|bring up|pull up|switch to|go to)\s+(?:up\s+)?(?:a\s+)?(new\s+)?(?:my\s+|the\s+|a\s+)?"
                 r"(?:web\s+)?browser(?:\s+window)?$", t)
    if m:
        new = bool(m.group(1)) or "new window" in t

        def run_browser():
            desktop_context.note_domain("browser")

            def ok(r):
                if r.get("reused"):
                    return f"Switched to your browser ({_title(r)})."
                return f"Opened {r.get('app', 'your browser')}."
            return _tool("desktop.open_app", "open your browser", ok, retry=run_browser, name="browser",
                         new_window=new)
        return Intent("desktop.open_browser", run_browser, "browser")

    # "open my Monkeytype browser" / "switch to the YouTube browser window":
    # the browser window whose title matches the words before "browser".
    m = re.match(r"^(?:open|launch|bring up|pull up|switch to|go to|go back to)\s+(?:up\s+)?(?:my\s+|the\s+)?(.+?)\s+"
                 r"(?:web\s+)?browser(?:\s+(?:window|tab))?$", t)
    if m and m.group(1).strip() not in ("my", "the", "a", "new", "a new", "web", "default", "other", "your"):
        hint = m.group(1).strip()

        def run_browser_hint():
            desktop_context.note_domain("browser")
            return _tool("desktop.open_app", f"switch to your {hint} browser window",
                         lambda r: f"Switched to {_title(r)}." if r.get("reused") else f"Opened {r.get('app', 'your browser')}.",
                         retry=run_browser_hint, name="browser", hint=hint)
        return Intent("desktop.open_browser", run_browser_hint, "browser")
    return None


def _summarize_screen() -> Reply:
    """'Summarize this page' — the window's visible text, condensed by the LLM."""
    res = call("screen.read")
    if not res.success:
        return Reply(res.error, ok=False)
    r = res.result
    lines = [x for x in (r.get("text") or []) if len(x) > 2]
    name = _title({"title": r.get("window") or ""})
    if not lines:
        return Reply(f"I can't read any text in {name}, so I can't summarize it.", ok=False)
    from modules.agent.llm import complete
    prompt = (f"The user is looking at \"{r.get('window', '')}\". Its visible text, top to bottom:\n"
              + "\n".join(f"- {x}" for x in lines[:40])
              + "\n\nSummarize what this page is about in two or three short spoken sentences. Only use the "
                "text above. Skip menus, buttons and navigation.")
    answer = complete(prompt)
    desktop_context.note_domain("desktop")
    return Reply(answer or f"I read {name}, but I couldn't summarize it.", ok=bool(answer))


def _not_an_app(name: str) -> bool:
    try:
        from modules.desktop.apps import app_catalog
        return app_catalog.resolve(name) is None
    except Exception:
        return True


def _mon_label(index) -> str:
    try:
        from modules.desktop.controller import desktop
        m = next(x for x in desktop.monitors() if x.index == int(index))
        return desktop.monitor_label(m)
    except Exception:
        return f"monitor {index}"

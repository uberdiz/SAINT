"""
modules/agent/autonomy/verify.py

Did a step actually work? An action saying "done" isn't proof: after "open Discord" SAINT looks
for a Discord window; after "start the dev server", something must answer on its port.

A step's ``verify`` spec (kinds):

    reply          the action's own result was a success (tools already verify what they can)
    window         {"name": "Discord"}               a window of that app is open
    foreground     {"name": "Discord"}               ... and in front
    process        {"name": "node"}                  the process is running
    port           {"port": 5173}                    something listens on localhost:5173
    http           {"url": "http://localhost:5173"}  the URL answers (< 500)
    file           {"path": "C:/..."}                the path exists
    monitor        {"name": "Spotify", "monitor": 2} the app's window is on that monitor
    spotify        {"playing": true} / {"volume": 40}
    browser_url    {"contains": "localhost:5173"}    the browser's address bar shows it
    screen_text    {"contains": "Submit succeeded"}  that text is on screen (UIA, then OCR)
    none           nothing to check

Checks are polled until they pass or time out (apps take a few seconds to open, servers longer),
and stop early when the task is cancelled. Missing specs are inferred from the command.
"""

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from modules.agent.autonomy.observe import Observer, observer as default_observer

TIMEOUTS = {"window": 10.0, "foreground": 6.0, "process": 8.0, "port": 30.0, "http": 30.0, "file": 3.0,
            "monitor": 4.0, "spotify": 5.0, "browser_url": 8.0, "screen_text": 4.0}
POLL = 0.4


@dataclass
class Verdict:
    ok: bool
    evidence: str = ""
    retryable: bool = True          # worth another attempt / a recovery (False: the check itself can't run)
    checked: str = ""               # the kind of check that ran


def infer(action: str) -> Dict[str, Any]:
    """A sensible check for a command when the plan didn't give one."""
    a = (action or "").strip().lower()
    m = re.match(r"^(?:open|launch|start|run|bring up|pull up)\s+(?:the\s+|my\s+)?(.+?)(?:\s+app)?$", a)
    if m and not re.search(r"\b(?:server|tests?|project|folder|file|scene|playlist|album|song|music|"
                           r"website|site|url|tab|page|link|terminal|in\b)", a):
        return {"kind": "window", "name": m.group(1)}
    m = re.match(r"^(?:switch to|focus|bring)\s+(?:the\s+|my\s+)?(.+?)(?:\s+to the front)?$", a)
    if m:
        return {"kind": "foreground", "name": m.group(1)}
    m = re.match(r"^(?:move|put|send)\s+(?:the\s+|my\s+)?(.+?)\s+(?:to|on|onto)\s+(?:my\s+|the\s+)?"
                 r"(main|primary|first|second|third|left|right|other|\d)\s*(?:monitor|screen|display)$", a)
    if m:
        return {"kind": "monitor", "name": m.group(1), "monitor": m.group(2)}
    if re.match(r"^(?:pause|stop)\s+(?:the\s+)?(?:music|spotify|song)", a):
        return {"kind": "spotify", "playing": False}
    if re.match(r"^(?:play|resume)\b", a):
        return {"kind": "spotify", "playing": True}
    return {"kind": "reply"}


def _monitor_index(want, observer: Observer) -> Optional[int]:
    if isinstance(want, int) or (isinstance(want, str) and want.isdigit()):
        return int(want)
    mons = observer.monitors().value or []
    if not mons:
        return None
    w = str(want).lower()
    if w in ("main", "primary", "first"):
        return next((m.index for m in mons if m.primary), 1)
    if w in ("second", "other"):
        others = [m.index for m in mons if not m.primary]
        return others[0] if others else None
    if w == "third":
        return mons[2].index if len(mons) > 2 else None
    if w in ("left", "right"):
        ordered = sorted(mons, key=lambda m: m.left)
        return (ordered[0] if w == "left" else ordered[-1]).index
    return None


def check_once(spec: Dict[str, Any], reply_ok: bool, observer: Observer) -> Verdict:
    kind = (spec or {}).get("kind", "reply")
    if kind == "none":
        return Verdict(True, "nothing to check", checked=kind)
    if kind == "reply":
        return Verdict(reply_ok, "the action reported success" if reply_ok else "the action reported a failure",
                       checked=kind)
    if kind in ("window", "foreground"):
        obs = observer.window(spec["name"], fresh=True)
        if kind == "window" or not obs.ok:
            return Verdict(obs.ok, obs.detail, checked=kind)
        fg = observer.foreground(fresh=True)
        ok = fg.ok and any(w.hwnd == fg.value.hwnd for w in obs.value)
        return Verdict(ok, f"{spec['name']} is in front" if ok else f"{spec['name']} is open but not in front",
                       checked=kind)
    if kind == "process":
        obs = observer.process(spec["name"], fresh=True)
        return Verdict(obs.ok, obs.detail, checked=kind)
    if kind == "port":
        obs = observer.port(int(spec["port"]), spec.get("host", "127.0.0.1"))
        return Verdict(obs.ok, obs.detail, checked=kind)
    if kind == "http":
        obs = observer.http(spec["url"])
        return Verdict(obs.ok, obs.detail, checked=kind)
    if kind == "file":
        obs = observer.file(spec["path"])
        return Verdict(obs.ok, obs.detail, checked=kind)
    if kind == "monitor":
        want = _monitor_index(spec.get("monitor"), observer)
        obs = observer.window_monitor(spec["name"])
        if want is None:
            return Verdict(obs.ok, obs.detail, retryable=False, checked=kind)
        ok = obs.ok and obs.value == want
        return Verdict(ok, obs.detail if obs.ok else obs.detail, checked=kind)
    if kind == "spotify":
        obs = observer.spotify(fresh=True)
        if not obs.ok:
            return Verdict(False, obs.detail, retryable=False, checked=kind)
        st = obs.value
        ok = True
        if "playing" in spec:
            ok = ok and bool(st.get("is_playing")) == bool(spec["playing"])
        if "volume" in spec and st.get("volume") is not None:
            ok = ok and abs(int(st["volume"]) - int(spec["volume"])) <= 2
        if "track" in spec:
            ok = ok and str(spec["track"]).lower() in str(st.get("track", "")).lower()
        return Verdict(ok, obs.detail, checked=kind)
    if kind == "browser_url":
        obs = observer.browser(spec.get("hint", ""))
        if not obs.ok:
            return Verdict(False, obs.detail, checked=kind)
        url = (obs.value or {}).get("url") or ""
        title = (obs.value or {}).get("title") or ""
        want = str(spec.get("contains", "")).lower()
        ok = want in url.lower() or (not url and want in title.lower())
        return Verdict(ok, f"the browser shows {url or title[:60]}", checked=kind)
    if kind == "screen_text":
        obs = observer.screen_text()
        want = str(spec.get("contains", "")).lower()
        ok = obs.ok and want in str(obs.value).lower()
        return Verdict(ok, f"{'saw' if ok else 'did not see'} “{spec.get('contains')}” ({obs.source})", checked=kind)
    return Verdict(reply_ok, f"unknown check {kind!r}: used the action's own result", retryable=False,
                   checked="reply")


def verify(spec: Optional[Dict[str, Any]], reply_ok: bool, observer: Optional[Observer] = None,
           cancelled: Callable[[], bool] = lambda: False, timeout: Optional[float] = None) -> Verdict:
    """Poll the check until it passes, times out or the task is cancelled. A failed action whose
    check is 'reply' fails immediately (there's nothing to wait for)."""
    observer = observer or default_observer
    spec = spec or {"kind": "reply"}
    kind = spec.get("kind", "reply")
    limit = timeout if timeout is not None else float(spec.get("timeout", TIMEOUTS.get(kind, 0.0)))
    deadline = time.time() + max(0.0, limit)
    while True:
        try:
            v = check_once(spec, reply_ok, observer)
        except Exception as e:                   # a check that can't run never crashes the task
            return Verdict(reply_ok, f"couldn't check ({e or type(e).__name__}); used the action's own result",
                           retryable=False, checked="reply")
        # Waiting only makes sense after the action said it worked (apps take a moment to appear);
        # a reported failure is checked once — maybe the result is already there anyway.
        if v.ok or not reply_ok or kind in ("reply", "none") or not v.retryable or time.time() >= deadline \
                or cancelled():
            return v
        time.sleep(POLL)

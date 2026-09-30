"""
modules/agent/winctl_intents.py

Voice commands for Windows itself:

    "lock my PC"                         "put the computer to sleep" / "restart" / "shut down"
    "cancel the shutdown"                "mute my mic" / "unmute my microphone"
    "set the system volume to 40"        "set Discord to 30 percent" / "mute the game" / "turn Discord down"
    "switch audio to my headphones"      "brightness 60" / "dim the screen"
    "do not disturb"                     "take a screenshot" (saved to Pictures\\Screenshots)

Restart / shutdown / sleep always ask first; restart and shutdown also wait
60 seconds so "cancel the shutdown" still works.
"""

import re
from typing import Optional

from core.config import config
from modules.agent.router import Intent, Reply, _clean, call, run_tool

_PC = r"(?:(?:my|the|this)\s+)?(?:pc|computer|laptop|machine|system)"
_NOT_APPS = {"it", "this", "that", "the volume", "volume", "the music", "music", "the sound", "sound", "audio",
             "the audio", "the video", "video", "youtube", "the tab", "tab", "everything", "all", "the song",
             "song", "spotify", "the computer", "computer", "the pc", "pc", "my mic", "the mic", "mic",
             "microphone", "my microphone", "the microphone"}


def _is_app(name: str) -> bool:
    """'discord', 'the game', 'steam' — not 'the volume', 'brightness', 'a timer'."""
    n = (name or "").strip()
    if re.match(r"^(?:it|this|that|these|those|them|everything)\b", n):
        return False              # "turn it up to 80%" is the music, not an app called "it up"
    return bool(n) and n not in _NOT_APPS and len(n.split()) <= 3 and not re.search(
        r"\b(brightness|timer|alarm|reminder|theme|mode|temperature|volume|speed|quality|playback|screen|"
        r"halo|overlay|notices|mini ?player|notifications?)\b", n)


def _run(tool: str, describe: str, ok, **kwargs) -> Intent:
    return Intent(tool, lambda: run_tool(tool, describe, ok, **kwargs), "system")


def parse_winctl(text: str) -> Optional[Intent]:
    t = _clean(text).lower().strip(" .!?")
    if not t:
        return None

    # ---- power ----------------------------------------------------------------------------
    if re.match(rf"^lock(?:\s+(?:{_PC}|the screen|my screen|windows|it|up))?$", t):
        return _run("system.lock", "lock the PC", lambda r: "Locked.")
    if re.match(rf"^(?:cancel|abort|stop|don'?t)\s+(?:the\s+)?(?:shut ?down|restart|reboot)(?:\s+{_PC})?$", t):
        return _run("system.cancel_shutdown", "cancel the shutdown",
                    lambda r: "Cancelled it." if r.get("cancelled") else "There wasn't a shutdown or restart pending.")
    m = re.match(rf"^(?:put\s+{_PC}\s+to\s+sleep|sleep\s+{_PC}|(?:make\s+)?{_PC}\s+(?:go\s+)?to\s+sleep|"
                 rf"(?P<hib>hibernate)(?:\s+{_PC})?)$", t)
    if m:
        action = "hibernate" if m.group("hib") else "sleep"
        return _run("system.power", f"put the PC to {'hibernate' if action == 'hibernate' else 'sleep'}",
                    lambda r: "Good night.", action=action)
    m = re.match(rf"^(?:restart|reboot)(?:\s+{_PC})?$|^(?:shut\s*down|power\s+off|turn\s+off)\s*(?:{_PC})?$|"
                 rf"^(?:sign|log)\s+(?:me\s+)?(?:out|off)(?:\s+of\s+windows)?$", t)
    if m and (re.search(_PC, t) or t in ("restart", "reboot", "shut down", "shutdown", "power off")
              or t.startswith(("sign", "log"))):
        if t.startswith(("restart", "reboot")):
            action, when = "restart", "restart the PC in 60 seconds"
        elif t.startswith(("sign", "log")):
            action, when = "sign_out", "sign you out of Windows now"
        else:
            action, when = "shutdown", "shut down the PC in 60 seconds"
        return _run("system.power", when,
                    lambda r: "Okay — say “cancel the shutdown” in the next minute if you change your mind."
                    if r.get("delay") else "Signing out.", action=action)

    # ---- microphone ---------------------------------------------------------------------------
    t = re.sub(r"\s+(?:on|in|with|through)\s+(?:the\s+)?voicemeeter$", "", t)
    t = re.sub(r"^(mute|unmute)\s+(?:the\s+)?voicemeeter\s+(mic|microphone)$", r"\1 my \2", t)
    # Bare "mute" / "unmute" means the mic (audio.bare_mute = "mic"), as with a mic-control app.
    if re.fullmatch(r"(?:un)?mute(?: me| myself)?", t) and config.get("audio.bare_mute", "mic") == "mic":
        t = ("unmute" if t.startswith("un") else "mute") + " my mic"
    m = re.match(r"^(mute|unmute)\s+(?:my\s+|the\s+)?(?:mic|microphone)$|"
                 r"^turn\s+(on|off)\s+(?:my\s+|the\s+)?(?:mic|microphone)$|"
                 r"^turn\s+(?:my\s+|the\s+)?(?:mic|microphone)\s+(on|off)$", t)
    if m:
        word = next(g for g in m.groups() if g)
        state = "on" if word in ("mute", "off") else "off"          # state = muted?
        return _run("audio.mic_mute", f"{'mute' if state == 'on' else 'unmute'} your mic",
                    lambda r: "Mic muted." if r["muted"] else "Mic on.", state=state)

    # ---- whole-PC volume ------------------------------------------------------------------------
    m = re.match(r"^(?:set|put|turn)\s+(?:the\s+)?(?:system|pc|computer|windows|master|overall)\s+(?:volume|sound)\s+"
                 r"(?:to|at)\s+(\d{1,3})(?:\s*(?:%|percent))?$|^(?:system|pc|computer)\s+volume\s+(\d{1,3})$", t)
    if m:
        pct = int(m.group(1) or m.group(2))
        return _run("audio.system_volume", f"set the volume to {pct}%", lambda r: f"Volume {r['percent']}%.",
                    percent=pct)

    # ---- one app's volume (volume mixer) ------------------------------------------------------
    m = re.match(r"^(?:set|put|turn)\s+(?:the\s+)?(?P<app>[\w .'-]+?)(?:'s)?\s+(?:volume\s+|sound\s+)?(?:to|at)\s+"
                 r"(?P<pct>\d{1,3})(?:\s*(?:%|percent))?$", t)
    if m and _is_app(m.group("app")):
        app, pct = m.group("app").strip(), int(m.group("pct"))
        return _run("audio.app_volume", f"set {app} to {pct}%", lambda r: f"{app.capitalize()} at {r['percent']}%.",
                    app=app, percent=pct)
    m = re.match(r"^(mute|unmute)\s+(?P<app>[\w .'-]+)$", t)
    if m and _is_app(m.group("app")) and not re.search(r"\b(tab|video|youtube)\b", t):
        app = m.group("app").strip()
        mute = "on" if m.group(1) == "mute" else "off"
        return _run("audio.app_volume", f"{m.group(1)} {app}", lambda r: f"{app.capitalize()} "
                    f"{'muted' if r.get('muted') else 'unmuted'}.", app=app, mute=mute)
    m = re.match(r"^(?:turn|make)\s+(?P<app>[\w .'-]+?)\s+(?P<dir>up|down|louder|quieter|softer)(?:\s+a (?:bit|little|lot))?$",
                 t)
    if m and _is_app(m.group("app")):
        app = m.group("app").strip()
        step = 20 if m.group("dir") in ("up", "louder") else -20
        if "a lot" in t:
            step *= 2
        elif re.search(r"a (bit|little)", t):
            step //= 2
        return _run("audio.app_volume", f"turn {app} {'up' if step > 0 else 'down'}",
                    lambda r: f"{app.capitalize()} at {r['percent']}%.", app=app, step=step)

    # ---- output device ------------------------------------------------------------------------------
    m = re.match(r"^(?:switch|change|move|send|set)\s+(?:the\s+|my\s+)?(?:audio|sound|output|playback)(?:\s+output)?\s+"
                 r"(?:to|over to|through)\s+(?P<dev>.+)$|"
                 r"^(?:use|switch to|play (?:sound|audio) (?:through|on|from|out of))\s+(?:my\s+|the\s+)?"
                 r"(?P<dev2>headphones?|headset|speakers?|monitor|tv|earbuds|buds|[\w .'-]+?\s+(?:headphones|headset|"
                 r"speakers))(?:\s+for\s+(?:audio|sound))?$", t)
    if m:
        dev = (m.group("dev") or m.group("dev2")).strip()
        return _run("audio.output_device", f"switch audio to {dev}", lambda r: f"Sound is now on {r['device']}.",
                    name=dev)

    # ---- brightness ---------------------------------------------------------------------------------
    m = re.match(r"^(?:set\s+)?(?:the\s+|my\s+)?(?:screen\s+)?brightness\s+(?:to\s+)?(\d{1,3})(?:\s*(?:%|percent))?$|"
                 r"^(?:set|put)\s+(?:the\s+|my\s+)?(?:screen\s+)?brightness\s+(?:to|at)\s+(\d{1,3})", t)
    if m:
        pct = int(m.group(1) or m.group(2))
        return _run("display.brightness", f"set brightness to {pct}%", lambda r: f"Brightness {r['percent']}%.",
                    percent=pct)
    m = re.match(r"^(?:make\s+(?:the|my)\s+screen\s+(brighter|dimmer|darker)|(dim|brighten)\s+(?:the|my)\s+screen|"
                 r"(?:turn\s+)?(?:the\s+)?brightness\s+(up|down))$", t)
    if m:
        word = next(g for g in m.groups() if g)
        step = 20 if word in ("brighter", "brighten", "up") else -20
        return _run("display.brightness", "change the brightness", lambda r: f"Brightness {r['percent']}%.",
                    step=step)

    # ---- focus / screenshot ----------------------------------------------------------------------------
    if re.match(r"^(?:turn on\s+|enable\s+|switch on\s+)?(?:do not disturb|dnd|focus assist)(?:\s+mode)?(?:\s+on)?$", t):
        return _run("system.do_not_disturb", "open Do Not Disturb",
                    lambda r: "Windows doesn't let apps switch Do Not Disturb, so I opened its settings — it's the "
                              "first switch.")
    if re.match(r"^(?:take|grab|capture|save)\s+(?:a\s+)?(?:screenshot|screen ?shot|screen capture)"
                r"(?:\s+of\s+(?:my|the|both|all)\s+screens?)?$|^screenshot(?:\s+(?:this|that|my screen|the screen))?$", t):
        return _run("system.screenshot", "take a screenshot",
                    lambda r: "Saved a screenshot to Pictures, Screenshots.")
    m = re.match(r"^(?:take|grab|capture|save)\s+(?:a\s+)?(?:screenshot|screen ?shot|screen capture|picture)\s+of\s+"
                 r"(?:the\s+|my\s+)?(?P<what>.+?)(?:\s+window)?$|^screenshot\s+(?:the\s+|my\s+)?(?P<what2>.+?)$", t)
    if m:
        what = (m.group("what") or m.group("what2")).strip()
        if what in ("this", "this window", "that", "it", "the window"):
            what = "this"
        return _run("system.screenshot", f"take a screenshot of {what}",
                    lambda r: f"Saved a screenshot of {r.get('what') or what} to Pictures, Screenshots.", target=what)
    return None

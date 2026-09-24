"""
modules/desktop/youtube.py

YouTube control in the user's own browser, the way a person does it: the
player's keyboard shortcuts (F full screen, K play/pause, J/L ten seconds,
Shift+> faster, C captions, T theater, I miniplayer, 0-9 seek, ...) and, for
settings that have no shortcut (quality, autoplay, loop, sleep timer, stable
volume, ambient mode, captions language), the player's own menus through
Windows UI Automation.

``parse(text)`` turns a spoken request into (action, value); ``run(action,
value)`` performs it in the YouTube window and returns what happened.
"""

import logging
import re
import time
from typing import Optional, Tuple

from modules.automation.tools import ToolError

log = logging.getLogger("saint.desktop.youtube")

# action -> (keys, reply). Keys are pressed in the YouTube page.
SHORTCUTS = {
    "play_pause": ("k", "Done."),
    "play": ("k", "Playing."),
    "pause": ("k", "Paused."),
    "fullscreen": ("f", "Full screen."),
    "exit_fullscreen": ("f", "Left full screen."),
    "theater": ("t", "Toggled theater mode."),
    "miniplayer": ("i", "Toggled the miniplayer."),
    "mute": ("m", "Muted."),
    "unmute": ("m", "Unmuted."),
    "captions": ("c", "Toggled captions."),
    "captions_on": ("c", "Captions on."),
    "captions_off": ("c", "Captions off."),
    "caption_bigger": ("shift+=", "Bigger captions."),
    "caption_smaller": ("-", "Smaller captions."),
    "caption_opacity": ("o", "Changed the caption text opacity."),
    "caption_background": ("w", "Changed the caption background."),
    "rewind": ("j", "Back 10 seconds."),
    "forward": ("l", "Forward 10 seconds."),
    "rewind_5": ("left", "Back 5 seconds."),
    "forward_5": ("right", "Forward 5 seconds."),
    "volume_up": ("up", "Louder."),
    "volume_down": ("down", "Quieter."),
    "speed_up": ("shift+.", "Faster."),
    "speed_down": ("shift+,", "Slower."),
    "next_video": ("shift+n", "Next video."),
    "previous_video": ("shift+p", "Previous video."),
    "next_frame": (".", "Next frame."),
    "previous_frame": (",", "Previous frame."),
    "next_chapter": ("ctrl+right", "Next chapter."),
    "previous_chapter": ("ctrl+left", "Previous chapter."),
    "restart": ("0", "Back to the start."),
    "end": ("end", "Jumped to the end."),
    "search": ("/", "The search box is ready."),
    "close": ("esc", "Closed."),
    "shortcuts": ("shift+/", "Here are YouTube's keyboard shortcuts."),
}

# Settings reached through the player's gear menu / buttons (no shortcut).
MENU_ACTIONS = ("quality", "autoplay_on", "autoplay_off", "loop_on", "loop_off", "sleep_timer",
                "stable_volume", "ambient_mode", "annotations", "captions_language", "like", "dislike",
                "subscribe", "skip_ad", "speed_menu")

ACTIONS = tuple(SHORTCUTS) + ("seek_percent", "seek_seconds", "speed") + MENU_ACTIONS

SPEEDS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]

_NUM = {"one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty five": 45,
        "fifty": 50, "sixty": 60, "ninety": 90, "half a": 0.5, "a couple": 2, "a few": 3}


def _num(s: str) -> Optional[float]:
    s = (s or "").strip().lower()
    try:
        return float(s)
    except ValueError:
        return _NUM.get(s)


# ---------------------------------------------------------------------- #
# Understanding a request
# ---------------------------------------------------------------------- #
_VID = r"(?:the |this |that )?(?:video|youtube(?: video)?|clip)"


def parse_speed(text: str) -> Optional[float]:
    t = text.lower()
    if re.search(r"\b(normal|regular|default|standard|1x|one x)\s+speed\b|\bspeed (?:back )?to normal\b", t):
        return 1.0
    if re.search(r"\bdouble speed\b|\btwice as fast\b|\b2x\b|\btwo x\b", t):
        return 2.0
    if re.search(r"\bhalf speed\b|\bhalf as fast\b", t):
        return 0.5
    m = re.search(r"(\d(?:\.\d+)?)\s*(?:x|times)\b", t) or re.search(r"speed (?:to |at )?(\d(?:\.\d+)?)\b", t)
    if m:
        v = float(m.group(1))
        return min(SPEEDS, key=lambda s: abs(s - v))
    m = re.search(r"\b(one|two) point (two five|five|seven five|twenty five|seventy five)\b", t)
    if m:
        whole = 1 if m.group(1) == "one" else 2
        frac = {"two five": .25, "twenty five": .25, "five": .5, "seven five": .75, "seventy five": .75}[m.group(2)]
        return min(SPEEDS, key=lambda s: abs(s - (whole + frac)))
    return None


def parse(text: str, youtube_context: bool = False) -> Optional[Tuple[str, object]]:
    """A spoken YouTube request -> (action, value), or None.

    ``youtube_context``: the user is watching YouTube, so bare commands
    ("faster", "mute", "skip ahead 30 seconds") mean the video. Without it only
    unambiguous YouTube requests match ("theater mode", "turn on captions",
    "set the video quality to 1080p")."""
    t = re.sub(r"[.!?]+$", "", (text or "").strip().lower())
    t = re.sub(r"\s+(?:on|in) (?:the |this )?(?:youtube|video|player)$", "", t)
    explicit = bool(re.search(r"\b(youtube|video|clip|player)\b", t))
    ctx = youtube_context or explicit

    # -- menu settings (unambiguous) ------------------------------------------------------
    m = re.search(r"\b(?:quality|resolution)\b.*?\b(\d{3,4}p(?:\s?60)?|4k|8k|auto(?:matic)?|highest|best|max(?:imum)?|"
                  r"lowest|worst|min(?:imum)?|high|low|hd|full hd)\b", t) or \
        re.search(r"^(?:set |put |change |switch )?(?:it |the video |youtube )?(?:to |in |at )?"
                  r"(\d{3,4}p(?:\s?60)?|4k|8k)(?: quality)?$", t) or \
        re.search(r"^(?:watch (?:it |this )?in |play (?:it |this )?in )(\d{3,4}p|4k|hd|full hd)$", t)
    if m:
        return "quality", m.group(1)
    m = re.search(r"\b(higher|better|lower|worse|highest|best|lowest|worst)\b.*\b(?:quality|resolution)\b", t)
    if m:
        return "quality", "highest" if m.group(1) in ("higher", "better", "highest", "best") else "lowest"
    if re.search(r"\bauto ?play\b", t):
        return ("autoplay_off" if re.search(r"\b(off|disable|stop|no more|turn off|don'?t)\b", t) else
                "autoplay_on"), None
    if (re.search(r"\bloop(?:ing)?\b", t) or ctx and re.search(r"\brepeat (?:this|the) video\b|\bon repeat\b", t)) \
            and not re.search(r"\b(song|track|playlist|album|spotify|music)\b", t):
        return ("loop_off" if re.search(r"\b(stop|off|don'?t|no longer|unloop|disable)\b", t) else "loop_on"), None
    m = re.search(r"\bsleep timer\b(?:.*?\b(\d+|off|end of (?:the )?video)\b\s*(minutes?|mins?|hours?)?)?", t)
    if m:
        val = m.group(1) or ""
        if val.isdigit() and (m.group(2) or "").startswith("hour"):
            val = str(int(val) * 60)
        return "sleep_timer", val
    if re.search(r"\bstable volume\b", t):
        return "stable_volume", None
    if re.search(r"\bambient mode\b", t):
        return "ambient_mode", None
    if re.search(r"\bannotations?\b", t):
        return "annotations", None
    m = re.search(r"\b(?:captions?|subtitles?|cc)\b.*\b(?:in|to) (english|spanish|french|german|italian|portuguese|"
                  r"japanese|korean|chinese|russian|arabic|hindi|dutch|polish|turkish|auto-?translate)\b", t)
    if m:
        return "captions_language", m.group(1)
    if re.search(r"^(?:skip|close|get rid of) (?:the |this )?ads?$|^skip ad$", t):
        return "skip_ad", None
    if re.search(r"^(?:like|thumbs up)(?: (?:this|the|that) (?:video|one))?$", t) and ctx:
        return "like", None
    if re.search(r"^(?:dislike|thumbs down)(?: (?:this|the|that) (?:video|one))?$", t):
        return "dislike", None
    if re.search(r"^subscribe(?: to (?:this|the|that) (?:channel|creator|guy|person|youtuber))?$", t):
        return "subscribe", None

    # -- player modes (unambiguous) -------------------------------------------------------
    if re.search(r"\b(?:theater|theatre|cinema|wide) mode\b", t):
        return "theater", None
    if re.search(r"\bmini ?player\b", t):
        return "miniplayer", None
    if re.match(r"^(?:(?:turn|switch|put|show|hide|enable|disable|toggle|remove|get rid of)\s+)?(?:on\s+|off\s+)?"
                r"(?:the\s+)?(?:closed\s+)?(?:captions?|subtitles?|cc)(?:\s+(?:on|off))?(?:\s+please)?$", t):
        if re.search(r"\b(off|disable|hide|remove|stop|no)\b", t):
            return "captions_off", None
        if re.search(r"\b(on|enable|show|turn on|put on|add)\b", t):
            return "captions_on", None
        return "captions", None
    if re.search(r"\b(?:captions?|subtitles?)\b.*\b(bigger|larger)\b|\b(bigger|larger) (?:captions?|subtitles?)\b", t):
        return "caption_bigger", None
    if re.search(r"\b(?:captions?|subtitles?)\b.*\bsmaller\b|\bsmaller (?:captions?|subtitles?)\b", t):
        return "caption_smaller", None
    if re.search(r"\bshortcuts\b", t) and ctx and re.search(r"\b(youtube|show|what|list)\b", t):
        return "shortcuts", None
    if re.search(r"\b(next|previous|last) chapter\b|\bskip (?:this |the )?chapter\b", t):
        return ("previous_chapter" if re.search(r"\b(previous|last)\b", t) else "next_chapter"), None
    if re.search(r"\b(next|previous) frame\b|\bframe (forward|back)\b", t):
        return ("previous_frame" if re.search(r"\b(previous|back)\b", t) else "next_frame"), None

    if not ctx:
        return None

    # -- playback (the video) ---------------------------------------------------------------
    if re.search(rf"^(?:go |put (?:it|this|the video) |make (?:it|this|the video) |enter )?full ?screen(?: mode)?$|"
                 rf"^(?:full ?screen|maximi[sz]e) {_VID}$", t):
        return "fullscreen", None
    if re.search(r"^(?:exit|leave|get out of|close) full ?screen", t):
        return "exit_fullscreen", None
    speed = parse_speed(t)
    if speed is not None and re.search(r"\b(speed|times|fast|playback)\b|\dx\b|\bx\b", t):
        return "speed", speed
    if re.search(r"^(?:speed (?:it |this |the video )?up|faster|play faster|go faster|increase (?:the )?"
                 r"(?:playback )?speed|speed up (?:the )?(?:video|playback))$", t):
        return "speed_up", None
    if re.search(r"^(?:slow (?:it |this |the video )?down|slower|play slower|go slower|decrease (?:the )?"
                 r"(?:playback )?speed|lower (?:the )?(?:playback )?speed|slow down (?:the )?(?:video|playback))$", t):
        return "speed_down", None
    if re.search(r"\b(?:playback )?speed\b", t) and re.search(r"\b(menu|options|settings|change)\b", t):
        return "speed_menu", None
    m = re.search(r"\b(?:skip|jump|go|fast forward|forward|move|seek)\s+(?:ahead|forward|on)?\s*(?:by\s+)?"
                  r"(\d+|[a-z]+(?: [a-z]+)?)\s+(seconds?|secs?|minutes?|mins?)\b", t)
    if m and not re.search(r"\bback\b", t) and _num(m.group(1)) is not None:
        secs = _num(m.group(1)) * (60 if m.group(2).startswith("min") else 1)
        return "seek_seconds", secs
    m = re.search(r"\b(?:go|skip|jump|rewind|move)\s+(?:back|backwards?)\s*(?:by\s+)?(\d+|[a-z]+(?: [a-z]+)?)\s+"
                  r"(seconds?|secs?|minutes?|mins?)\b|^rewind (?:by )?(\d+|[a-z]+(?: [a-z]+)?) (seconds?|minutes?)\b", t)
    if m:
        amount, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        if _num(amount) is not None:
            return "seek_seconds", -_num(amount) * (60 if unit.startswith("min") else 1)
    if re.search(r"^(?:rewind|go back a bit|back a bit|back 10(?: seconds)?|go back 10(?: seconds)?)$", t):
        return "rewind", None
    if re.search(r"^(?:skip ahead|fast forward|forward a bit|skip forward|jump ahead|go forward a bit)$", t):
        return "forward", None
    m = re.search(r"\b(?:skip|jump|go|seek|move)\s+(?:to\s+)?(?:the\s+)?(\d{1,2}|ninety|[a-z]+)\s*(?:%|percent)\b", t)
    if m:
        pct = _num(m.group(1))
        if pct is not None:
            return "seek_percent", pct
    if re.search(r"\b(?:halfway|half way|the middle)\b", t) and re.search(r"\b(skip|jump|go|seek)\b", t):
        return "seek_percent", 50
    if re.search(r"^(?:restart|start over|replay|go (?:back )?to the (?:beginning|start)|from the (?:beginning|top))"
                 rf"(?: {_VID})?$|^(?:restart|replay) {_VID}$", t):
        return "restart", None
    if re.search(r"^(?:go|skip|jump) to the end\b", t):
        return "end", None
    if re.search(rf"^(?:next|skip)(?: to the next)? (?:video|one)$|^(?:play|go to) the next video$|^skip {_VID}$", t):
        return "next_video", None
    if re.search(r"^(?:previous|last|go back to the (?:previous|last)) video$|^play the previous video$", t):
        return "previous_video", None
    if re.search(rf"^(?:un-?mute|turn (?:the )?sound (?:back )?on)(?: {_VID})?$", t):
        return "unmute", None
    if re.search(rf"^mute(?: {_VID}| it| the sound)?$", t):
        return "mute", None
    if re.search(rf"^(?:pause|stop)(?: {_VID}| it)?$", t):
        return "pause", None
    if re.search(rf"^(?:play|resume|unpause|continue)(?: {_VID}| it)?$", t):
        return "play", None
    if re.search(rf"^(?:turn (?:the )?(?:video|youtube) (?:volume )?up|louder)$", t):
        return "volume_up", None
    if re.search(rf"^(?:turn (?:the )?(?:video|youtube) (?:volume )?down|quieter|softer)$", t):
        return "volume_down", None
    return None


# ---------------------------------------------------------------------- #
# Doing it
# ---------------------------------------------------------------------- #
def youtube_window(required: bool = True):
    """The browser window showing YouTube: the one the user is looking at or
    SAINT is working in, else the only / chosen browser window on YouTube."""
    from modules.desktop.controller import desktop
    wins = [w for w in desktop.app_windows("browser") if "youtube" in w.title.lower()]
    tw = desktop.target_window()
    if tw is not None and any(w.hwnd == tw.hwnd for w in wins):
        return tw
    if len(wins) == 1:
        return wins[0]
    if wins:
        from modules.desktop import browser
        try:
            return browser.choose(wins=wins)
        except ToolError:
            if not required:
                return None
            raise
    if required:
        raise ToolError("I can't see a YouTube video in any browser window — the YouTube tab needs to be the "
                        "one showing.", "NOT_FOUND")
    return None


def is_watching() -> bool:
    """Is a YouTube tab in front (or the window SAINT is working in)?"""
    from core.config import config
    if not (config.get("desktop.enabled", True) and config.get("modules.desktop", True)):
        return False
    try:
        from modules.desktop.controller import desktop
        tw = desktop.target_window()
        return tw is not None and desktop.is_browser(tw) and "youtube" in tw.title.lower()
    except Exception:
        return False


def _focus_page(w):
    """Make sure keystrokes reach the YouTube page, not a text box: YouTube
    ignores shortcuts while its search box (or the address bar) has focus."""
    from modules.desktop.controller import desktop
    w = desktop._activate(w)
    try:
        from modules.desktop.uia import _auto
        auto = _auto()
        f = auto.GetFocusedControl()
        if f is not None and f.ControlTypeName in ("EditControl", "ComboBoxControl"):
            inside_page = False
            p, hops = f, 0
            while p is not None and hops < 40:
                if p.ControlTypeName == "DocumentControl":
                    inside_page = True
                    break
                p, hops = p.GetParentControl(), hops + 1
            # YouTube's search box: Tab moves on to the search button (letters do
            # nothing there). The address bar: F6 hands focus back to the page.
            desktop._tap("tab" if inside_page else "f6")
            time.sleep(0.12)
    except Exception as e:
        log.debug("youtube.focus_check_failed %s", e)
    return w


def _press(keys: str, times: int = 1, interval: float = 0.06):
    import pyautogui
    parts = keys.split("+") if keys != "+" else ["+"]
    pause, pyautogui.PAUSE = pyautogui.PAUSE, 0.0       # repeated steps (speed, seeking) stay quick
    try:
        for _ in range(max(1, times)):
            if len(parts) == 1:
                pyautogui.press(parts[0])
            else:
                pyautogui.hotkey(*parts)
            time.sleep(interval)
    finally:
        pyautogui.PAUSE = pause


def run(action: str, value=None) -> dict:
    from modules.desktop.controller import _require
    _require("allow_keyboard", "Keyboard control")
    if action not in ACTIONS:
        raise ToolError(f"I don't know the YouTube action '{action}'.", "INVALID")
    w = _focus_page(youtube_window())
    out = {"action": action, "window": w.title, "hwnd": w.hwnd}
    try:
        from modules.agent.context import desktop_context
        desktop_context.note_domain("browser")
    except Exception:
        pass
    if action in SHORTCUTS:
        keys, said = SHORTCUTS[action]
        _press(keys)
        out.update(keys=keys, said=said)
    elif action == "seek_percent":
        pct = max(0, min(90, int(float(value or 0) // 10 * 10)))
        _press(str(pct // 10))
        out.update(keys=str(pct // 10), said=f"Jumped to {pct} percent.")
    elif action == "seek_seconds":
        secs = float(value or 10)
        tens, fives = divmod(abs(int(round(secs / 5.0)) * 5), 10)
        n10 = min(60, tens)
        if n10:
            _press("l" if secs > 0 else "j", n10)
        if fives:
            _press("right" if secs > 0 else "left")
        span = f"{int(abs(secs) // 60)} minute{'s' if abs(secs) >= 120 else ''}" if abs(secs) >= 60 and abs(secs) % 60 == 0 \
            else f"{int(abs(secs))} seconds"
        out.update(said=("Forward " if secs > 0 else "Back ") + span + ".")
    elif action == "speed":
        target = min(SPEEDS, key=lambda s: abs(s - float(value or 1)))
        # From any speed (YouTube goes up to 4x), twelve steps down reach the
        # slowest; then step up to the target.
        _press("shift+,", 12, 0.05)
        _press("shift+.", SPEEDS.index(target), 0.05)
        out.update(speed=target, said=f"Playing at {_speed_word(target)}.")
    else:
        out.update(_menu(action, value, w))
    log.info("youtube.%s value=%r window=%r", action, value, w.title[:50])
    return out


def _speed_word(s: float) -> str:
    return "normal speed" if s == 1 else f"{s:g}x speed"


# ---- menus ---------------------------------------------------------------- #
def _find(top, pattern: str, types=None, wait: float = 1.5):
    """First element under ``top`` whose name matches ``pattern`` (regex)."""
    from modules.desktop.uia import _walk
    rx = re.compile(pattern, re.I)
    deadline = time.monotonic() + wait
    while True:
        for c in _walk(top, limit=4000):
            try:
                if (types is None or c.ControlTypeName in types) and rx.search(c.Name or "") \
                        and c.BoundingRectangle.width() > 0:
                    return c
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def _press_el(el, how: str = "click"):
    import pyautogui
    r = el.BoundingRectangle
    x, y = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    if how == "right":
        pyautogui.rightClick(x, y)
        return
    try:
        el.GetInvokePattern().Invoke()
    except Exception:
        pyautogui.click(x, y)
    time.sleep(0.25)


def _toggle_state(el) -> Optional[bool]:
    try:
        return bool(el.GetTogglePattern().ToggleState)
    except Exception:
        pass
    try:
        return el.GetLegacyIAccessiblePattern().State & 0x10 != 0      # STATE_SYSTEM_CHECKED
    except Exception:
        return None


def _open_settings(top):
    """Show the player's controls and open the gear menu."""
    import pyautogui
    player = _find(top, r"^YouTube Video Player", wait=0.6)
    if player is not None:
        r = player.BoundingRectangle
        pyautogui.moveTo((r.left + r.right) // 2, r.bottom - 60, duration=0.1)   # reveal the control bar
    gear = _find(top, r"^Settings$", {"ButtonControl"}, wait=1.5)
    if gear is None:
        raise ToolError("I can't find the video's settings button — is a video open in that tab?",
                        "ELEMENT_NOT_FOUND")
    _press_el(gear)
    return gear


def _menu_item(top, pattern: str, what: str):
    item = _find(top, pattern, {"MenuItemControl", "CheckBoxControl", "ButtonControl", "ListItemControl"}, wait=1.5)
    if item is None:
        _press("esc")
        raise ToolError(f"YouTube's menu doesn't offer {what} for this video.", "ELEMENT_NOT_FOUND")
    return item


def _menu(action: str, value, w) -> dict:
    from modules.desktop.uia import _auto
    top = _auto().ControlFromHandle(w.hwnd)
    if action == "quality":
        return _set_quality(top, str(value or "auto"))
    if action in ("autoplay_on", "autoplay_off"):
        want = action == "autoplay_on"
        btn = _find(top, r"^Autoplay", {"ButtonControl", "CheckBoxControl"}, wait=1.5)
        if btn is None:
            raise ToolError("I can't find the autoplay switch on this page.", "ELEMENT_NOT_FOUND")
        named = re.search(r"is (on|off)", btn.Name or "", re.I)
        on = named.group(1).lower() == "on" if named else bool(_toggle_state(btn))
        if on != want:
            _press_el(btn)
        return {"said": f"Autoplay is {'on' if want else 'off'}."}
    if action in ("loop_on", "loop_off"):
        player = _find(top, r"^YouTube Video Player", wait=1.0)
        if player is None:
            raise ToolError("I can't find the video player on this page.", "ELEMENT_NOT_FOUND")
        _press_el(player, "right")
        item = _menu_item(top, r"^Loop", "looping")
        want = action == "loop_on"
        if _toggle_state(item) != want:
            _press_el(item)
        else:
            _press("esc")
        return {"said": "Looping this video." if want else "Stopped looping."}
    if action == "like":
        btn = _find(top, r"^like this video", {"ButtonControl", "ToggleButtonControl"})
        if btn is None:
            raise ToolError("I can't find the like button.", "ELEMENT_NOT_FOUND")
        _press_el(btn)
        return {"said": "Liked it."}
    if action == "dislike":
        btn = _find(top, r"^dislike this video", {"ButtonControl"})
        if btn is None:
            raise ToolError("I can't find the dislike button.", "ELEMENT_NOT_FOUND")
        _press_el(btn)
        return {"said": "Disliked it."}
    if action == "subscribe":
        btn = _find(top, r"^subscribe to ", {"ButtonControl"})
        if btn is None:
            raise ToolError("I can't find a Subscribe button — you may already be subscribed.", "ELEMENT_NOT_FOUND")
        _press_el(btn)
        return {"said": "Subscribed."}
    if action == "skip_ad":
        btn = _find(top, r"^skip( ad)?s?$|^skip ad", {"ButtonControl"}, wait=2.0)
        if btn is None:
            raise ToolError("There's no ad I can skip right now.", "ELEMENT_NOT_FOUND")
        _press_el(btn)
        return {"said": "Skipped the ad."}

    _open_settings(top)
    if action == "speed_menu":
        _press_el(_menu_item(top, r"^Playback speed", "playback speed"))
        return {"said": "Here are the speed options."}
    if action == "sleep_timer":
        _press_el(_menu_item(top, r"^Sleep timer", "a sleep timer"))
        v = str(value or "").lower()
        pat = r"^Off$" if v == "off" else r"^End of video" if v.startswith("end") else \
            (rf"^{int(v)} minutes?$" if v.isdigit() else r"^30 minutes$")
        _press_el(_menu_item(top, pat, f"a {v} minute sleep timer" if v.isdigit() else "that sleep timer"))
        return {"said": "Sleep timer off." if v == "off" else "Sleep timer set."}
    if action in ("stable_volume", "ambient_mode", "annotations"):
        label = {"stable_volume": "Stable volume", "ambient_mode": "Ambient mode", "annotations": "Annotations"}[action]
        item = _menu_item(top, rf"^{label}", label.lower())
        before = _toggle_state(item)
        _press_el(item)
        _press("esc")
        state = "" if before is None else (" off" if before else " on")
        return {"said": f"Turned {label.lower()}{state}." if state else f"Toggled {label.lower()}."}
    if action == "captions_language":
        _press_el(_menu_item(top, r"^Subtitles/CC|^Captions", "captions"))
        lang = str(value or "english")
        if lang.startswith("auto"):
            _press_el(_menu_item(top, r"^Auto-translate", "auto-translate"))
            return {"said": "Pick the language from the list."}
        _press_el(_menu_item(top, rf"^{lang}", f"{lang} captions"))
        return {"said": f"Captions in {lang.title()}."}
    raise ToolError(f"I don't know the YouTube action '{action}'.", "INVALID")


def _set_quality(top, want: str) -> dict:
    _press_el(_menu_item(top, r"^Quality", "a quality setting"))
    w = want.lower().replace(" ", "")
    options = []
    from modules.desktop.uia import _walk
    for c in _walk(top, limit=4000):
        try:
            if c.ControlTypeName in ("MenuItemControl", "RadioButtonControl", "ListItemControl") \
                    and re.match(r"^(\d{3,4})p(60)?|^Auto", c.Name or "") and c.BoundingRectangle.width() > 0:
                options.append(c)
        except Exception:
            continue
    if not options:
        _press("esc")
        raise ToolError("YouTube didn't show any quality options for this video.", "ELEMENT_NOT_FOUND")

    def height(c):
        m = re.match(r"^(\d{3,4})p", c.Name or "")
        return int(m.group(1)) if m else 0
    fixed = sorted([c for c in options if height(c)], key=height)
    if w.startswith("auto"):
        pick = next((c for c in options if (c.Name or "").lower().startswith("auto")), None)
    elif w in ("highest", "best", "max", "maximum", "high", "4k", "8k") and fixed:
        pick = fixed[-1] if w not in ("4k", "8k") else next((c for c in fixed if height(c) >= (2160 if w == "4k" else 4320)),
                                                            fixed[-1])
    elif w in ("lowest", "worst", "min", "minimum", "low") and fixed:
        pick = fixed[0]
    else:
        n = {"hd": 720, "fullhd": 1080}.get(w) or int(re.sub(r"\D", "", w.split("p")[0]) or 720)
        exact = [c for c in fixed if height(c) == n]
        pick = (exact or [min(fixed, key=lambda c: abs(height(c) - n))] if fixed else [None])[0]
        if exact and "60" in w:
            pick = next((c for c in exact if "60" in (c.Name or "")[:7]), exact[0])
    if pick is None:
        _press("esc")
        raise ToolError(f"This video doesn't offer {want}.", "ELEMENT_NOT_FOUND")
    label = (pick.Name or "").split(" ")[0]
    _press_el(pick)
    got = label if label.lower() != "auto" else "auto"
    note = "" if w.startswith(("auto", "high", "best", "max", "low", "worst", "min")) or \
        re.sub(r"\D", "", got) == re.sub(r"\D", "", w.split("p")[0]) else f" ({want} isn't available)"
    return {"said": f"Quality set to {got}{note}.", "quality": got}

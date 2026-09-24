"""
modules/desktop/browser.py

Browser actions on the user's *existing* browser window.

* ``choose`` decides which browser window a request means, without asking
  whenever the answer is obvious:
    1. a window the request names ("my YouTube browser"),
    2. the window SAINT is in the middle of working with (multi-step requests),
    3. the window the user picked last time ("use the second one") — kept
       until that window closes,
    4. the browser the user is looking at right now,
    5. the only browser window on screen.
  Several on screen and none of the above → AmbiguousWindow (the agent asks,
  and remembers the answer). None on screen → the minimized ones are offered;
  no browser at all → NoBrowser (the agent offers to open one).
* ``open_url`` / ``new_tab`` navigate in that window. ``site_search`` turns
  "search YouTube for X" into the site's own search URL.
* Every navigation is verified: the window title must change (or a new
  browser window must appear) before SAINT says it worked.
"""

import logging
import os
import re
import time
import urllib.parse
from typing import List, Optional

from core.config import config
from modules.automation.tools import ToolError

log = logging.getLogger("saint.desktop.browser")

PREF_KEY = "desktop.preferred_browser"      # {"hwnd", "process", "title", "at"}


class NoBrowser(ToolError):
    """No browser window exists; the agent offers to open one."""

    def __init__(self):
        super().__init__("You don't have a browser open.", "NO_BROWSER")
        global last_no_browser
        last_no_browser = time.time()


last_no_browser = 0.0


# ---------------------------------------------------------------------- #
# Which browser window
# ---------------------------------------------------------------------- #
def remember(w) -> None:
    """Use this window for browser requests from now on (until it closes)."""
    if w is None:
        return
    config.set(PREF_KEY, {"hwnd": int(w.hwnd), "process": w.process, "title": (w.title or "")[:80],
                          "at": time.time()})
    log.info("browser.preferred hwnd=%s title=%r", w.hwnd, (w.title or "")[:50])


def forget_preference() -> None:
    config.set(PREF_KEY, None)


def preferred(wins: List) -> Optional[object]:
    pref = config.get(PREF_KEY) or {}
    hwnd = pref.get("hwnd") if isinstance(pref, dict) else None
    return next((w for w in wins if w.hwnd == hwnd), None) if hwnd else None


def on_screen(wins: List) -> List:
    return [w for w in wins if not w.minimized]


def choose(hint: str = "", wins: Optional[List] = None, remember_hint: bool = True):
    """The browser window a request means (see the module docstring).
    Raises AmbiguousWindow or NoBrowser when it has to ask."""
    from modules.desktop.controller import AmbiguousWindow, desktop
    wins = desktop.app_windows("browser") if wins is None else list(wins)
    if not wins:
        raise NoBrowser()
    words = [x for x in re.findall(r"[a-z0-9]+", (hint or "").lower())
             if len(x) > 2 and x not in ("browser", "window", "the", "my", "tab", "web")]
    if words:
        hits = [w for w in wins if all(x in w.title.lower() for x in words)] or \
               [w for w in wins if any(x in w.title.lower() for x in words)]
        if len(hits) == 1:
            if remember_hint:
                remember(hits[0])
            return hits[0]
    if len(wins) == 1:
        return wins[0]
    from modules.agent.context import desktop_context
    ref = desktop_context.window(max_age=float(config.get("desktop.context_window_ttl_sec", 45.0)))
    working = next((w for w in wins if w.hwnd == ref), None)
    if working is not None:
        return working
    pref = preferred(wins)
    if pref is not None:
        return pref
    fg = next((w for w in wins if w.foreground), None)
    if fg is not None:
        return fg
    visible = on_screen(wins)
    if len(visible) == 1:
        return visible[0]
    if config.get("desktop.multi_window_policy", "ask") == "recent":
        return (visible or wins)[0]          # z-order: the most recently used
    if visible:
        raise AmbiguousWindow("browser", visible, remember=True)
    raise AmbiguousWindow("browser", wins, remember=True, offscreen=True)


def _browser_window(hint: str = "", new_window: bool = False):
    """The browser window to navigate in, or None to open a new one. A site
    hint ("youtube") only steers this request — it isn't remembered."""
    if new_window:
        return None
    return choose(hint, remember_hint=False)


def open_default_browser() -> dict:
    """Start the default browser (when none is open and the user said yes)."""
    from modules.desktop.controller import desktop
    return desktop.open_app("browser", new_window=True)

SITES = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.com", "gmail": "https://mail.google.com",
    "github": "https://github.com", "reddit": "https://www.reddit.com", "twitch": "https://www.twitch.tv",
    "netflix": "https://www.netflix.com", "amazon": "https://www.amazon.com", "wikipedia": "https://www.wikipedia.org",
    "twitter": "https://x.com", "x": "https://x.com", "facebook": "https://www.facebook.com",
    "instagram": "https://www.instagram.com", "chatgpt": "https://chatgpt.com", "roblox": "https://www.roblox.com",
    "google maps": "https://maps.google.com", "spotify": "https://open.spotify.com", "bing": "https://www.bing.com",
    "duckduckgo": "https://duckduckgo.com", "ebay": "https://www.ebay.com", "tiktok": "https://www.tiktok.com",
}

SEARCH_URLS = {
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "google": "https://www.google.com/search?q={q}",
    "the web": "https://www.google.com/search?q={q}",
    "web": "https://www.google.com/search?q={q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
    "github": "https://github.com/search?q={q}",
    "twitch": "https://www.twitch.tv/search?term={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "ebay": "https://www.ebay.com/sch/i.html?_nkw={q}",
    "google maps": "https://www.google.com/maps/search/{q}",
    "maps": "https://www.google.com/maps/search/{q}",
    "images": "https://www.google.com/search?tbm=isch&q={q}",
}


def site_url(name: str) -> Optional[str]:
    n = (name or "").strip().lower().strip(".").removeprefix("the ")
    if n in SITES:
        return SITES[n]
    if re.fullmatch(r"(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|gg|tv|dev|co|app|ai|"
                    r"edu|gov|uk|ca|de|me|so|xyz)(?:/\S*)?", n):
        return n if n.startswith("http") else "https://" + n
    return None


def search_url(site: str, query: str) -> Optional[str]:
    tpl = SEARCH_URLS.get((site or "").strip().lower().removeprefix("the "))
    return tpl.format(q=urllib.parse.quote_plus(query.strip())) if tpl else None


def new_tab(hwnd: Optional[int] = None) -> dict:
    """Open a new tab in the user's browser."""
    from modules.desktop.controller import desktop, _require
    _require("allow_keyboard", "Keyboard control")
    w = desktop._activate(desktop._info(hwnd) if hwnd else _browser_window())
    desktop.press_keys("ctrl+t")
    time.sleep(0.3)
    info = desktop._info(w.hwnd)
    desktop._note(info)
    return {"window": info.title, "hwnd": info.hwnd, "started": False}


def open_url(url: str, hint: str = "", new_window: bool = False, hwnd: Optional[int] = None) -> dict:
    """Navigate the user's browser to ``url`` and verify the page changed."""
    from modules.desktop.controller import desktop, _require
    _require("allow_keyboard", "Keyboard control")
    site = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    w = desktop._info(hwnd) if hwnd else _browser_window(hint, new_window)
    if w is None:
        # No browser running (or a new window was asked for): let Windows open
        # the default browser, then wait for its window.
        before = {x.hwnd for x in desktop.app_windows("browser")}
        os.startfile(url)  # type: ignore[attr-defined]
        deadline = time.time() + 10
        while time.time() < deadline:
            time.sleep(0.3)
            new = [x for x in desktop.app_windows("browser") if x.hwnd not in before or x.foreground]
            if new:
                desktop._note(new[0])
                return {"url": url, "site": site, "window": new[0].title, "reused": False, "verified": True}
        return {"url": url, "site": site, "window": "", "reused": False, "verified": False}
    w = desktop._activate(w)
    before = w.title
    desktop.press_keys("ctrl+l")
    time.sleep(0.12)
    desktop.type_text(url, press_enter=True)
    verified = False
    deadline = time.time() + 8
    while time.time() < deadline:
        time.sleep(0.25)
        cur = desktop._info(w.hwnd).title
        if cur and cur != before:
            verified = True
            break
    info = desktop._info(w.hwnd)
    desktop._note(info)
    log.info("browser.open_url site=%s verified=%s window=%r", site, verified, info.title[:60])
    return {"url": url, "site": site, "window": info.title, "hwnd": info.hwnd, "reused": True,
            "verified": verified}


def site_search(site: str, query: str, hint: str = "") -> dict:
    url = search_url(site, query)
    if url is None:
        base = site_url(site)
        if base is None:
            raise ToolError(f"I don't know how to search {site}.", "UNSUPPORTED")
        url = search_url("google", f"site:{re.sub(r'^https?://(www.)?', '', base)} {query}")
    res = open_url(url, hint=hint or site)
    res["query"] = query
    return res

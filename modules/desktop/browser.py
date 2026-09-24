"""
modules/desktop/browser.py

Browser actions on the user's *existing* browser window.

* ``open_url`` reuses a running browser window (never opens a duplicate
  unless asked). With several browser windows it picks the one the request
  or the user's focus points at, or raises AmbiguousWindow so the agent can
  ask which one.
* ``site_search`` turns "search YouTube for X" into the site's own search
  URL — more reliable than hunting for the search box.
* Every navigation is verified: the window title must change (or a new
  browser window must appear) before SAINT says it worked.
"""

import logging
import os
import re
import time
import urllib.parse
from typing import Optional

from modules.automation.tools import ToolError

log = logging.getLogger("saint.desktop.browser")

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


def _browser_window(hint: str = "", new_window: bool = False):
    """The browser window to use: foreground/recent/matching, else None."""
    from modules.desktop.controller import desktop, AmbiguousWindow
    if new_window:
        return None
    wins = desktop.app_windows("browser")
    if not wins:
        return None
    from modules.agent.context import desktop_context
    ref = desktop_context.window()
    if ref and any(w.hwnd == ref for w in wins) and not hint:
        return next(w for w in wins if w.hwnd == ref)
    tw = desktop.target_window()
    if tw is not None and desktop.is_browser(tw):
        return tw
    chosen = desktop.pick_window(wins, hint)
    if chosen is None:
        raise AmbiguousWindow("browser", wins)
    return chosen


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

"""
modules/agent/social_intents.py

Messages on social apps, done the way a person would:

    "open what Gian sent me on Instagram"     Instagram app if it's installed,
    "open my chat with Gian on Discord"        otherwise its inbox in the browser;
    "check my Instagram DMs"                    then the conversation with that
                                                person is clicked open.

SAINT only opens and reads — it never sends a message by itself. The user
asked to open something, so it comes to the front (core/focus_guard.py).
"""

import re
import time
from typing import Optional

from modules.agent.router import Intent, Reply, _clean, _open_site, run_tool

# platform -> (app name to look for, inbox URL, label)
PLATFORMS = {
    "instagram": ("instagram", "https://www.instagram.com/direct/inbox/", "Instagram"),
    "insta": ("instagram", "https://www.instagram.com/direct/inbox/", "Instagram"),
    "ig": ("instagram", "https://www.instagram.com/direct/inbox/", "Instagram"),
    "discord": ("discord", "https://discord.com/channels/@me", "Discord"),
    "whatsapp": ("whatsapp", "https://web.whatsapp.com/", "WhatsApp"),
    "messenger": ("messenger", "https://www.messenger.com/", "Messenger"),
    "facebook": ("messenger", "https://www.messenger.com/", "Messenger"),
    "snapchat": ("snapchat", "https://web.snapchat.com/", "Snapchat"),
    "snap": ("snapchat", "https://web.snapchat.com/", "Snapchat"),
    "twitter": ("x", "https://x.com/messages", "X"),
    "x": ("x", "https://x.com/messages", "X"),
    "tiktok": ("tiktok", "https://www.tiktok.com/messages", "TikTok"),
    "reddit": ("reddit", "https://chat.reddit.com/", "Reddit chat"),
    "telegram": ("telegram", "https://web.telegram.org/", "Telegram"),
}
_P = "|".join(sorted(map(re.escape, PLATFORMS), key=len, reverse=True))
_THING = r"(?:what|whatever|the\s+(?:thing|message|messages|link|post|reel|video|pic|picture|photo|meme|tiktok|dm|text)s?)"
_SENT = re.compile(
    rf"^(?:open|show(?:\s+me)?|check|pull up|bring up|go to|look at|read)\s+(?:up\s+)?{_THING}\s+(?:that\s+)?"
    rf"(?:my\s+(?:friend|boy|homie|bro|girl)\s+)?(?P<who>[\w' .()-]+?)\s+(?:just\s+)?(?:sent|send|dm'?d|messaged|posted)"
    rf"(?:\s+me)?(?:\s+(?:on|in|over|through)\s+(?P<p>{_P}))?$", re.I)
_CHAT = re.compile(
    rf"^(?:open|show(?:\s+me)?|check|pull up|bring up|go to)\s+(?:up\s+)?(?:my\s+)?(?:chat|chats|dms?|messages|"
    rf"conversation|convo|texts?)\s+(?:with|from)\s+(?P<who>[\w' .()-]+?)\s+(?:on|in)\s+(?P<p>{_P})$", re.I)
_INBOX = re.compile(
    rf"^(?:open|show(?:\s+me)?|check|pull up|bring up|go to|read)\s+(?:up\s+)?(?:my\s+)?(?P<p>{_P})\s+"
    rf"(?:dms?|messages|inbox|chats?|texts?)$|"
    rf"^(?:open|show(?:\s+me)?|check|go to)\s+(?:up\s+)?(?:my\s+)?(?:dms?|messages|inbox)\s+(?:on|in)\s+(?P<p2>{_P})$",
    re.I)


def _clean_name(who: str) -> str:
    # "my john (gian)" -> "gian": the part in brackets is what the user meant.
    m = re.search(r"\(([^)]+)\)", who)
    who = m.group(1) if m else who
    who = re.sub(r"^(?:my|the|this|that|our)\s+", "", who.strip(" .'\""), flags=re.I)
    return who.strip()


def _open_platform(key: str) -> Reply:
    app, url, label = PLATFORMS[key]
    try:
        from modules.desktop.apps import app_catalog
        entry = app_catalog.resolve(app)
    except Exception:
        entry = None
    if entry is not None and entry.kind != "uri":
        reply = run_tool("desktop.open_app", f"open {label}", lambda r: f"Opened {label}.", name=app)
        if reply.ok:
            return reply
    return _open_site(url)


def _open_chat(key: str, who: str) -> Reply:
    label = PLATFORMS[key][2]
    opened = _open_platform(key)
    if not opened.ok:
        return opened
    if not who:
        return Reply(f"Opened your {label} messages.")
    # The inbox takes a moment to load: look for the person a few times.
    last = None
    for wait in (2.5, 2.0, 2.5):
        time.sleep(wait)
        last = run_tool("desktop.click_element", f"open the chat with {who}", lambda r: "", name=who,
                        action="click")
        if last.ok:
            return Reply(f"Opened your {label} chat with {who.title()}.")
    return Reply(f"Opened your {label} messages, but I couldn't find a chat with {who.title()} on screen — "
                 f"it may be further down.", ok=True)


def parse_social(text: str) -> Optional[Intent]:
    t = _clean(text).strip(" .!?")
    m = _SENT.match(t) or _CHAT.match(t)
    if m:
        key = (m.group("p") or "instagram").lower()
        who = _clean_name(m.group("who"))
        if not who or who.lower() in ("you", "it", "this", "that", "someone", "somebody"):
            return None
        return Intent("social.open_chat", lambda: _open_chat(key, who), "desktop")
    m = _INBOX.match(t)
    if m:
        key = (m.group("p") or m.group("p2")).lower()
        return Intent("social.open_inbox", lambda: _open_chat(key, ""), "desktop")
    return None

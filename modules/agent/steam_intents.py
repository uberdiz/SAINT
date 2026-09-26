"""
modules/agent/steam_intents.py

Voice commands for Steam:

    "open my Steam library"               "search Steam for Hades" / "search Hades on Steam"
    "launch Counter-Strike 2" / "play Terraria on Steam" / "start CS2"
    "what games do I have" / "how big is Apex Legends"
    "uninstall Celeste"                   (asks first; Steam asks again)

After "open my Steam library", a bare "search X" means Steam: if you have X
installed SAINT says so, otherwise it searches the Steam store.
"""

import re
from typing import Optional

from modules.agent.router import Intent, Reply, _clean, call, run_tool


def _gb(n: int) -> str:
    return f"{n / 1e9:.0f} GB" if n >= 1e10 else f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _match(name: str):
    try:
        from modules.steam.library import steam_library, steam_path
        if not steam_path():
            return None
        return steam_library.match(name)
    except Exception:
        return None


def _steam_context() -> bool:
    from modules.agent.context import desktop_context
    return desktop_context.domain() == "steam"


def _music_context() -> bool:
    from modules.agent.context import desktop_context
    return desktop_context.music_is_context()


def parse_steam(text: str) -> Optional[Intent]:
    raw = _clean(text)
    t = raw.lower().strip(" .!?")
    if not t:
        return None

    # ---- the library ----------------------------------------------------------------------
    if re.match(r"^(?:open|show(?: me)?|go to|bring up|pull up|switch to)\s+(?:up\s+)?(?:my\s+|the\s+)?"
                r"(?:steam\s+(?:library|games)|games? library in steam|library (?:in|on) steam)$", t):
        return Intent("steam.open_library", lambda: run_tool(
            "steam.open_library", "open your Steam library",
            lambda r: "Starting Steam — I'll open your library as soon as it's up." if r.get("starting")
            else "Opened your Steam library."), "steam")

    # ---- the store -----------------------------------------------------------------------------
    m = re.match(r"^(?:search|look up|find|look for)\s+(?:the\s+)?steam(?:\s+store)?\s+for\s+(.+)$", t) or \
        re.match(r"^(?:search(?: for)?|look up|find|look for)\s+(.+?)\s+(?:on|in)\s+(?:the\s+)?steam(?:\s+store)?$", t)
    if m:
        query = raw[m.start(1):m.end(1)] if len(raw) == len(t) else m.group(1)
        return Intent("steam.store_search", lambda: _search_store(query), "steam")
    if _steam_context():
        m = re.match(r"^(?:search(?: for)?|look up|look for|find|type)\s+(.+)$", t)
        if m:
            name = m.group(1).strip(" ?")
            return Intent("steam.search", lambda: _search_library_then_store(name), "steam")

    # ---- what's installed ------------------------------------------------------------------------
    if re.match(r"^(?:what|which)\s+(?:steam\s+)?games\s+(?:do i have|have i got|are installed)(?:\s+(?:on steam|"
                r"installed|on this pc|on my pc))?$|^list\s+(?:all\s+)?my\s+(?:steam\s+)?games$|"
                r"^(?:what(?:'s| is)|show me what(?:'s| is))\s+installed\s+(?:on|in)\s+steam$|"
                r"^(?:what are|which are)\s+my\s+(?:biggest|largest)\s+games$", t):
        by_size = bool(re.search(r"biggest|largest", t))
        return Intent("steam.list_games", lambda: _list_games(by_size), "steam")
    m = re.match(r"^(?:how big is|how much (?:space|room|storage) (?:does|is)|what size is)\s+(.+?)"
                 r"(?:\s+(?:taking(?: up)?|take(?: up)?|using|use))?(?:\s+on (?:my|the) (?:pc|drive|disk))?$", t)
    if m and _match(m.group(1)):
        g = _match(m.group(1))
        return Intent("steam.game_info", lambda: Reply(
            f"{g.name} takes {_gb(g.size)} in {g.library}."), "steam")

    # ---- uninstall -------------------------------------------------------------------------------
    m = re.match(r"^(?:uninstall|remove|delete)\s+(?:the\s+game\s+)?(.+?)(?:\s+from\s+(?:steam|my pc))?$", t)
    if m and (t.startswith("uninstall") or "steam" in t or "game" in t) and _match(m.group(1)):
        g = _match(m.group(1))
        return Intent("steam.uninstall", lambda: run_tool(
            "steam.uninstall", f"uninstall {g.name} ({_gb(g.size)})",
            lambda r: f"Steam is asking to confirm uninstalling {r['uninstalling']} — that frees {r['size_text']}.",
            name=g.name), "steam")

    # ---- launch ------------------------------------------------------------------------------------
    m = re.match(r"^(?:launch|start|run|open|boot up|fire up|load up|load)\s+(?:up\s+)?(?:the\s+game\s+|my\s+)?(.+?)"
                 r"(?:\s+(?:on|in|from|through)\s+steam)?$", t)
    if m and m.group(1) not in ("steam", "my steam", "the steam app"):
        g = _match(m.group(1))
        if g is not None:
            return Intent("steam.launch", lambda: _launch(g), "steam")
    m = re.match(r"^(?:let'?s\s+)?play\s+(?:the\s+game\s+|some\s+)?(.+?)(?P<steam>\s+(?:on|in|from)\s+steam|\s+game)?$", t)
    if m:
        g = _match(m.group(1))
        if g is not None and (m.group("steam") or "game" in t or not _music_context()):
            # "play Terraria" is the game; "play Celeste" while music is on stays Spotify.
            from modules.steam.library import _simplify
            exact = _simplify(m.group(1)) == _simplify(g.name)
            if m.group("steam") or "game" in t or exact:
                return Intent("steam.launch", lambda: _launch(g), "steam")
    return None


def _launch(g) -> Reply:
    from modules.agent.context import desktop_context
    desktop_context.note_domain("steam")
    res = call("steam.launch", name=g.name)
    if res.success:
        return Reply(f"Starting {g.name}.")
    return Reply(res.error or f"Steam couldn't start {g.name}.", ok=False)


def _list_games(by_size: bool) -> Reply:
    res = call("steam.list_games", sort="size" if by_size else "recent")
    if not res.success:
        return Reply(res.error, ok=False)
    r = res.result
    if not r["count"]:
        return Reply("I don't see any games installed through Steam.")
    names = [g["name"] + (f" ({_gb(g['size'])})" if by_size else "") for g in r["games"][:8]]
    more = f", and {r['count'] - 8} more" if r["count"] > 8 else ""
    text = (f"You have {r['count']} Steam games using {_gb(r['total_size'])}. "
            + ("Biggest first: " if by_size else "Most recently played: ") + ", ".join(names) + more + ".")
    if r["unregistered"]:
        n = sum(1 for g in r["games"] if not g["registered"])
        text += (f" {n} of them are in {', '.join(r['unregistered'])}, which Steam doesn't have listed — "
                 f"add it under Steam > Settings > Storage to play them.")
    return Reply(text)


def _search_store(query: str) -> Reply:
    from modules.agent.context import desktop_context
    desktop_context.note_domain("steam")
    return run_tool("steam.store_search", f"search Steam for {query}",
                    lambda r: f"Searched the Steam store for {query}.", query=query)


def _search_library_then_store(name: str) -> Reply:
    g = _match(name)
    if g is not None:
        return Reply(f"You already have {g.name} installed ({_gb(g.size)}). Say “launch {g.name}” to play.")
    r = _search_store(name)
    if r.ok:
        return Reply(f"You don't have {name} installed, so I searched the Steam store for it.")
    return r

"""
modules/agent/link_intents.py

Voice and typed commands for SAINT Link (modules/link):

    "pair my phone" / "add a device"              open a pairing window for another device of yours
    "add a friend" / "let Gian connect"           open one for a collaborator's SAINT
    "what devices are connected"                  list them
    "send this prompt to Gian's PC on Claude: ..." type a prompt into an AI app on his PC
    "send gian a message saying I'm running late" a message his SAINT reads out
    "play <song> on Gian's PC"                    music on his Spotify
    "send that to Gian" / "send the screenshot to my laptop"   a file
    "ask my PC to lock itself"                    any command, run on another device of yours
    "sync my devices"
    "accept what Gian shared"
    "disconnect Gian"

Everything here is a request to *another* SAINT, which checks its own
permissions (and asks its owner when a collaborator wants something that needs
a yes). A name that isn't a paired device is never guessed at: the request is
declined, or, for phrasings that could mean something else, left to the rest of
SAINT.
"""

import re
from typing import Optional

from modules.agent.router import Intent, Reply, _clean

_APPS = r"claude|chatgpt|gemini|copilot|perplexity|grok"
_WHO = r"(?P<who>[\w' .-]{1,40}?)"
_POSS = r"(?:'s|’s|s')?"
_MACHINE = r"(?:\s+(?P<mach>pc|computer|laptop|desktop|machine|saint|phone|iphone))?"

_PROMPT = re.compile(
    rf"^(?:send|type|put|give|write)\s+(?:this|the following|that|a|the)?\s*(?:prompt|message|question|text)\s+"
    rf"(?:over\s+)?(?:to|into)\s+{_WHO}{_POSS}{_MACHINE}\s+(?:on|in|into|with|to|through)\s+(?P<app>{_APPS})\s*[:,\-–—]?\s*(?P<p>.+)$", re.I)
_MESSAGE = [
    re.compile(rf"^(?:send|text)\s+{_WHO}\s+a\s+message(?:\s+(?:saying|that says|that|like|:))?\s*[:,]?\s*(?P<t>.+)$", re.I),
    re.compile(rf"^message\s+{_WHO}\s*[:,]\s*(?P<t>.+)$", re.I),
    re.compile(rf"^tell\s+{_WHO}\s+(?:that\s+)?(?P<t>.+)$", re.I),
]
_PLAY = re.compile(rf"^play\s+(?P<q>.+?)\s+on\s+{_WHO}{_POSS}\s+(?:spotify|pc|computer|laptop|desktop|saint)$", re.I)
_OPEN = re.compile(rf"^open\s+(?P<u>https?://\S+)\s+on\s+{_WHO}{_POSS}{_MACHINE}$", re.I)
_FILE = re.compile(
    r"^(?:send|share|give|airdrop)\s+(?P<what>that|it|this|the (?:last |latest |new )?(?:screenshot|file|download|picture|"
    r"image|folder|archive|document)|my (?:last |latest )?(?:screenshot|download))(?:\s+file)?\s+to\s+"
    rf"{_WHO}{_POSS}{_MACHINE}$", re.I)
_ASK = [
    re.compile(rf"^(?:ask|tell)\s+(?:my\s+)?{_WHO}{_POSS}{_MACHINE}\s+to\s+(?P<c>.+)$", re.I),
    re.compile(rf"^(?:on|from)\s+(?:my\s+)?{_WHO}{_POSS}{_MACHINE}\s*[,:]\s*(?P<c>.+)$", re.I),
]
_ASK_SAINT = re.compile(rf"^ask\s+{_WHO}{_POSS}\s+saint\s+(?P<q>.+)$", re.I)
_PAIR_OWN = re.compile(
    r"^(?:pair|connect|link|add|set up)\s+(?:a\s+|another\s+|my\s+|the\s+)?(?:new\s+)?(?:device|phone|iphone|ipad|laptop|pc|computer)"
    r"(?:\s+to\s+(?:saint|my account))?$|^(?:pair|connect|link)\s+(?:my\s+)?devices$", re.I)
_PAIR_FRIEND = re.compile(
    r"^(?:add|invite|let)\s+(?P<who>[\w' .-]{0,30}?)\s*(?:as\s+a\s+)?(?:collaborator|friend|partner)$|"
    r"^(?:add|invite)\s+(?:a\s+)?(?:collaborator|friend|partner)$|^let\s+(?P<who2>[\w' .-]{1,30}?)\s+connect(?:\s+to\s+(?:my\s+)?saint)?$", re.I)
_LIST = re.compile(r"^(?:what|which)\s+(?:devices|collaborators|saints)\b.*\b(?:connected|paired|linked|have)\b.*$|"
                   r"^(?:list|show)(?:\s+me)?\s+(?:my\s+)?(?:paired |connected |linked )?(?:devices|collaborators)$|"
                   r"^who(?:'s| is) connected$|^(?:my\s+)?devices$", re.I)
_SYNC = re.compile(r"^(?:sync|synchroni[sz]e)\s+(?:my\s+)?(?:devices|everything|now|my phone|my pc|memory|memories)$|^sync$", re.I)
_ACCEPT = re.compile(r"^(?:accept|keep|take)\s+(?:what|everything|the things)\s+(?P<who>[\w' .-]+?)\s+(?:shared|sent)(?:\s+with me)?$|"
                     r"^(?:accept|keep)\s+(?:the\s+)?shared\s+(?:things|items)$", re.I)
_UNPAIR = re.compile(rf"^(?:disconnect|unpair|remove|forget|revoke)\s+(?:my\s+)?{_WHO}{_POSS}{_MACHINE}$", re.I)
_TOGGLE = re.compile(r"^(?P<verb>turn on|turn off|enable|disable|start|stop|switch on|switch off)\s+(?:saint\s+)?link$", re.I)
_SHARE = re.compile(rf"^share\s+(?:my\s+)?(?P<kind>scene|alias|skill|shortcut)\s+(?P<name>.+?)\s+with\s+{_WHO}$", re.I)


def _link():
    from modules.link.service import get_link
    return get_link()


def _who(m) -> str:
    """The name in a match. "my laptop" parses as who="my" + the machine word "laptop": when "my" alone
    isn't anyone, the machine word was part of the name."""
    who = (m.group("who") or "").strip()
    mach = (m.groupdict().get("mach") or "").strip()
    if mach and _peer(who) is None and _peer(f"{who} {mach}") is not None:
        return f"{who} {mach}"
    return who


def _peer(who: str):
    who = (who or "").strip(" .,'’")
    return _link().find_peer(who) if who else None


def _unknown(who: str) -> Reply:
    names = [p["name"] for p in _link().devices()]
    hint = f" I know {', '.join(names)}." if names else " Say “pair my phone” or “add a friend” to add one."
    return Reply(f"I don't have a device called {who.strip()}.{hint}", ok=False)


def _error_reply(e) -> Reply:
    code = getattr(e, "code", "")
    if code == "denied":
        return Reply(f"{e}", ok=False)
    if code in ("offline", "closed", "unreachable", "no_address", "timeout"):
        return Reply(f"I couldn't reach them: {e}", ok=False)
    return Reply(str(e) or "That didn't work.", ok=False)


def parse_link(text: str) -> Optional[Intent]:
    t = _clean(text).strip()
    if not t or len(t) > 2500:
        return None

    # ---- a prompt typed into an AI app on another PC ----------------------------------
    m = _PROMPT.match(t)
    if m:
        who, app, prompt = _who(m), m.group("app").lower(), m.group("p").strip()

        def run_prompt():
            peer = _peer(who)
            if peer is None:
                return _unknown(who)
            try:
                res = _link().run_remote(peer, "send_prompt", {"target": app, "prompt": prompt})
            except Exception as e:
                return _error_reply(e)
            return Reply(res.get("text") or f"Sent to {app.title()} on {peer.name}.")
        return Intent("link.send_prompt", run_prompt, "link")

    # ---- music / links on someone's PC --------------------------------------------------
    m = _PLAY.match(t)
    if m and _peer(_who(m)) is not None:
        who, q = _who(m), m.group("q").strip()

        def run_play():
            peer = _peer(who)
            try:
                return Reply(_link().run_remote(peer, "play_music", {"query": q}).get("text") or "Playing it there.")
            except Exception as e:
                return _error_reply(e)
        return Intent("link.play_music", run_play, "link")
    m = _OPEN.match(t)
    if m and _peer(_who(m)) is not None:
        who, url = _who(m), m.group("u")

        def run_open():
            try:
                return Reply(_link().run_remote(_peer(who), "open_url", {"url": url}).get("text") or "Opened.")
            except Exception as e:
                return _error_reply(e)
        return Intent("link.open_url", run_open, "link")

    # ---- a message ------------------------------------------------------------------------
    for rx in _MESSAGE:
        m = rx.match(t)
        if m and _who(m).strip().lower() not in ("me", "you", "saint", "us") and _peer(_who(m)) is not None:
            who, body = _who(m), m.group("t").strip()

            def run_message(who=who, body=body):
                try:
                    _link().run_remote(_peer(who), "message", {"text": body})
                except Exception as e:
                    return _error_reply(e)
                return Reply(f"Told {_peer(who).name}.")
            return Intent("link.message", run_message, "link")

    # ---- a file -----------------------------------------------------------------------------
    m = _FILE.match(t)
    if m and _peer(_who(m)) is not None:
        who, what = _who(m), m.group("what")

        def run_file():
            from modules.agent.recent import recent
            peer = _peer(who)
            thing = recent.find(what, want={"screenshot", "file", "archive", "download"}) or \
                recent.latest({"screenshot", "file", "archive", "download"})
            if thing is None or not thing.path:
                return Reply("I don't have a file to send. Take a screenshot or pick a file first, then say "
                             "“send that to " + peer.name + "”.", ok=False)
            try:
                _link().send_file(peer, thing.path)
            except Exception as e:
                return _error_reply(e)
            return Reply(f"Sent {thing.label} to {peer.name}.")
        return Intent("link.send_file", run_file, "link")

    # ---- run a command on another device of mine / ask someone's SAINT ---------------------
    m = _ASK_SAINT.match(t)
    if m and _peer(_who(m)) is not None:
        who, q = _who(m), m.group("q").strip()

        def run_ask_saint():
            try:
                res = _link().run_remote(_peer(who), "ask", {"question": q})
            except Exception as e:
                return _error_reply(e)
            return Reply(f"{_peer(who).name}'s SAINT says: {res.get('text', '')}")
        return Intent("link.ask", run_ask_saint, "link")
    for rx in _ASK:
        m = rx.match(t)
        if m and _peer(_who(m)) is not None and _peer(_who(m)).role == "own":
            who, cmd = _who(m), m.group("c").strip()

            def run_remote_cmd(who=who, cmd=cmd):
                peer = _peer(who)
                try:
                    res = _link().ask_peer(peer, cmd)
                except Exception as e:
                    return _error_reply(e)
                return Reply(res.get("text") or f"Done on {peer.name}.", expects_reply=bool(res.get("expects_reply")))
            return Intent("link.remote_command", run_remote_cmd, "link")

    # ---- pairing and housekeeping -----------------------------------------------------------
    m = _JOIN_CODE.match(t)
    if m:
        from modules.link.identity import looks_like_code
        raw_code = m.group("code")
        code = re.sub(r"[\s-]", "", raw_code)
        # "pair my laptop" is 8 letters too: a code is said with "code", or has a dash, a digit,
        # or is spelled out letter by letter.
        spelled = sum(1 for w in raw_code.split() if len(w) == 1) >= 4
        if looks_like_code(code) and (m.group("kw") or "-" in raw_code or re.search(r"\d", raw_code) or spelled):
            return Intent("link.join", lambda: _join_code(code), "link")
    if _PAIR_OWN.match(t):
        return Intent("link.pair", lambda: _open_pairing("own"), "link")
    m = _PAIR_FRIEND.match(t)
    if m:
        return Intent("link.pair_friend", lambda: _open_pairing("collaborator"), "link")
    if _LIST.match(t):
        return Intent("link.list", _list_devices, "link")
    if _SYNC.match(t):
        return Intent("link.sync", _sync, "link")
    m = _ACCEPT.match(t)
    if m:
        who = _who(m)
        return Intent("link.accept", lambda: _accept(who), "link")
    m = _SHARE.match(t)
    if m and _peer(_who(m)) is not None:
        kind, name, who = m.group("kind").lower(), m.group("name").strip(), _who(m)
        return Intent("link.share", lambda: _share(kind, name, who), "link")
    m = _TOGGLE.match(t)
    if m:
        on = m.group("verb").lower() in ("turn on", "enable", "start", "switch on")
        return Intent("link.toggle", lambda: _toggle(on), "link")
    m = _UNPAIR.match(t)
    if m and _peer(_who(m)) is not None:
        who = _who(m)
        return Intent("link.unpair", lambda: _unpair(who), "link")
    return None


_JOIN_CODE = re.compile(r"^(?:pair|join|connect|link)(?:\s+(?:with|to|using))?(?P<kw>\s+(?:the\s+)?(?:pairing\s+)?code)?"
                        r"\s+(?P<code>[a-z2-7](?:[\s-]?[a-z2-7018]){7})$", re.I)


def _join_code(code: str) -> Reply:
    try:
        peer = _link().pair(code)
    except Exception as e:
        return _error_reply(e)
    return Reply(f"Paired with {peer.name}.")


# ---------------------------------------------------------------------- #
def _open_pairing(role: str) -> Reply:
    try:
        info = _link().offer(role)
    except Exception as e:
        return _error_reply(e)
    try:
        from modules.automation.tools import get_tool_registry
        get_tool_registry().execute("ui.navigate", page="Devices")
    except Exception:
        pass
    who = "your device" if role == "own" else "your friend"
    spelled = " ".join(info["code"].replace("-", ""))
    return Reply(f"Pairing is open for five minutes. The code is {spelled}. On {who}, open SAINT and scan the "
                 f"QR code on the Devices page — or on another PC just type that code into Join.")


def _list_devices() -> Reply:
    devices = _link().devices()
    if not devices:
        return Reply("No devices are paired yet. Say “pair my phone” to add one.")
    on = [d["name"] for d in devices if d["connected"]]
    off = [d["name"] for d in devices if not d["connected"]]
    parts = []
    if on:
        parts.append(f"Connected: {', '.join(on)}")
    if off:
        parts.append(f"Not connected: {', '.join(off)}")
    return Reply(". ".join(parts) + ".")


def _sync() -> Reply:
    link = _link()
    if not link.running:
        return Reply("SAINT Link is off. Say “turn on SAINT Link” first.", ok=False)
    n = link.sync_now()
    return Reply("Synced." if n else "None of your devices are connected right now.", ok=bool(n))


def _accept(who: Optional[str]) -> Reply:
    peer = _peer(who) if who else None
    n = _link().accept_shared(peer.id if peer else None)
    return Reply(f"Kept {n} shared thing{'s' if n != 1 else ''}." if n else "There's nothing waiting.")


def _share(kind: str, name: str, who: str) -> Reply:
    peer = _peer(who)
    kind = "skill" if kind == "shortcut" else kind
    try:
        if kind == "scene":
            from modules.automation.scenes import _norm, scenes
            scene = next((s for s in scenes.all() if _norm(s.name) == _norm(name)), None)
            if scene is None:
                return Reply(f"I don't have a scene called {name}.", ok=False)
            _link().share(peer, "scene", scene.id, {"name": scene.name, "steps": scene.steps, "phrase": scene.phrase})
        elif kind == "alias":
            from modules.agent.aliases import _norm as anorm, aliases
            target = aliases.all().get(anorm(name))
            if target is None:
                return Reply(f"I don't have an alias called {name}.", ok=False)
            _link().share(peer, "alias", anorm(name), {"target": target})
        else:
            from modules.learning.skills import skills
            skill = skills.find(name)
            if skill is None:
                return Reply(f"I haven't learned anything called {name}.", ok=False)
            _link().share(peer, "skill", skill.id, {"phrase": skill.phrase, "steps": skill.steps,
                                                    "how": "shared", "said": skill.said})
    except Exception as e:
        return _error_reply(e)
    return Reply(f"Shared {name} with {peer.name}. They'll be asked to keep it.")


def _toggle(on: bool) -> Reply:
    link = _link()
    ok = link.set_enabled(on)
    if on:
        return Reply(f"SAINT Link is on, listening on port {link.node.port}." if ok
                     else "I couldn't start SAINT Link; check the port in Settings.", ok=ok)
    return Reply("SAINT Link is off. Nothing can connect to this PC.")


def _unpair(who: str) -> Reply:
    peer = _peer(who)
    _link().unpair(peer.id)
    return Reply(f"Disconnected and removed {peer.name}. They can't reconnect unless you pair again.")

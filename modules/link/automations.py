"""
modules/link/automations.py

Things a paired device can ask this PC to do, each behind its own permission.

    send_prompt   type a prompt into an AI app — "send this prompt to Gian's PC on Claude"
    message       show and read out a short message
    open_url      open a link in the browser
    run_scene     run one of the scenes you chose to share
    play_music    play something on this PC's Spotify
    ask           ask this SAINT a question (answered by the language model alone:
                  no memories, no tools, no PC access)

A collaborator gets each of these as "ask" by default, which means SAINT asks the
owner out loud (or on the Devices page) every time, with the exact text it is
about to type. Your own devices get them as "allow". Every automation checks its
input before anything happens, and none of them can run an arbitrary command:
the list is closed, and adding one means adding it here.
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, List

from core.config import config
from modules.link.wire import LinkError

log = logging.getLogger("saint.link")

MAX_PROMPT = 2000
_URL = re.compile(r"^https?://[^\s]+$", re.I)

DEFAULT_PROMPT_TARGETS = {
    "claude": {"app": "Claude", "url": "https://claude.ai/new", "wait": 3.5},
    "chatgpt": {"app": "ChatGPT", "url": "https://chatgpt.com/", "wait": 3.5},
    "gemini": {"url": "https://gemini.google.com/app", "wait": 4.0},
    "copilot": {"url": "https://copilot.microsoft.com/", "wait": 4.0},
    "perplexity": {"url": "https://www.perplexity.ai/", "wait": 4.0},
    "grok": {"url": "https://grok.com/", "wait": 4.0},
}


def prompt_targets() -> dict:
    targets = dict(DEFAULT_PROMPT_TARGETS)
    targets.update(config.get("link.prompt_targets", {}) or {})
    return targets


@dataclass
class RemoteAutomation:
    name: str
    perm: str
    title: str
    describe: Callable[[dict], str]
    validate: Callable[[dict], dict]
    run: Callable[[dict, object], dict]


def _tool(tool_name: str, /, **kwargs):
    """Run a registered tool. The tool's own arguments (``name=...`` for open_app) are keywords."""
    from modules.automation.tools import get_tool_registry
    res = get_tool_registry().execute(tool_name, **kwargs)
    if not res.success:
        raise LinkError(res.error or f"{tool_name} failed", res.error_code or "failed")
    return res.result


def _text(args: dict, key: str, limit: int) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LinkError(f"'{key}' is missing", "bad_args")
    value = value.strip()
    if len(value) > limit:
        raise LinkError(f"'{key}' is too long ({len(value)} of {limit} characters)", "bad_args")
    if "\x00" in value:
        raise LinkError(f"'{key}' has characters I can't use", "bad_args")
    return value


# ---------------------------------------------------------------------- #
# send_prompt
# ---------------------------------------------------------------------- #
def _prompt_validate(args: dict) -> dict:
    target = _text(args, "target", 30).lower()
    if target not in prompt_targets():
        raise LinkError(f"I don't know how to reach “{target}” on this PC. "
                        f"I can do: {', '.join(sorted(prompt_targets()))}.", "bad_args")
    return {"target": target, "prompt": _text(args, "prompt", int(config.get("link.max_prompt_chars", MAX_PROMPT)))}


def _prompt_describe(args: dict) -> str:
    p = args["prompt"]
    return f"type a prompt into {args['target'].title()}: “{p[:160]}{'…' if len(p) > 160 else ''}”"


def _prompt_run(args: dict, ctx) -> dict:
    spec = prompt_targets()[args["target"]]
    opened = False
    if spec.get("app"):
        try:
            _tool("desktop.open_app", name=spec["app"])
            opened = True
        except LinkError:
            opened = False
    if not opened and spec.get("url"):
        _tool("desktop.open_url", url=spec["url"])
        opened = True
    if not opened:
        raise LinkError(f"I couldn't open {args['target'].title()} on this PC.", "unavailable")
    time.sleep(float(spec.get("wait", 3.5)))
    limit = int(config.get("desktop.max_type_length", 500))
    lines = args["prompt"].replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        for start in range(0, len(line), limit):
            _tool("desktop.type_text", text=line[start:start + limit])
        if i < len(lines) - 1:
            _tool("desktop.press_keys", keys="shift+enter")     # a new line, not "send"
    time.sleep(0.3)
    _tool("desktop.press_keys", keys="enter")
    return {"text": f"Sent to {args['target'].title()}."}


# ---------------------------------------------------------------------- #
# message / open_url / run_scene / play_music / ask
# ---------------------------------------------------------------------- #
def _message_run(args: dict, ctx) -> dict:
    ctx.node.emit("link.message", {"peer_id": ctx.peer.id, "peer": ctx.peer.name, "text": args["text"]})
    return {"text": "Delivered."}


def _url_validate(args: dict) -> dict:
    url = _text(args, "url", 2000)
    if not _URL.match(url):
        raise LinkError("Only http and https links can be opened.", "bad_args")
    return {"url": url}


def _url_run(args: dict, ctx) -> dict:
    _tool("desktop.open_url", url=args["url"])
    return {"text": "Opened."}


def _scene_validate(args: dict) -> dict:
    return {"scene": _text(args, "scene", 60)}


def _scene_run(args: dict, ctx) -> dict:
    from modules.automation.scenes import _norm, scenes
    shared = {_norm(x) for x in (config.get("link.shared_scenes", []) or [])}
    name = _norm(args["scene"])
    scene = next((s for s in scenes.all() if _norm(s.name) == name or s.id == args["scene"]), None)
    if scene is None or (_norm(scene.name) not in shared and scene.id not in shared):
        raise LinkError("That scene isn't shared with you.", "not_shared")
    if scenes.plan(scene).interactive:
        # Its questions ("which account?") are asked and answered at the PC; never run it blind.
        raise LinkError(f"{scene.name} asks questions while it runs, so start it at the PC.", "interactive")
    scenes.run_in_background(scene)
    return {"text": f"Running {scene.name}."}


def _music_run(args: dict, ctx) -> dict:
    res = _tool("spotify.play_query", query=args["query"])
    what = res.get("name") or args["query"]
    return {"text": f"Playing {what}." if not res.get("artist") else f"Playing {what} by {res['artist']}."}


def _ask_run(args: dict, ctx) -> dict:
    from modules.link.service import plain_answer
    return {"text": plain_answer(args["question"], f"The user's friend {ctx.peer.name} is asking you something.")}


REGISTRY: Dict[str, RemoteAutomation] = {a.name: a for a in [
    RemoteAutomation("send_prompt", "auto.send_prompt", "Type a prompt into an AI app",
                     _prompt_describe, _prompt_validate, _prompt_run),
    RemoteAutomation("message", "auto.message", "Send a message",
                     lambda a: f"say: “{a['text'][:160]}”", lambda a: {"text": _text(a, "text", 500)}, _message_run),
    RemoteAutomation("open_url", "auto.open_url", "Open a link",
                     lambda a: f"open {a['url'][:120]}", _url_validate, _url_run),
    RemoteAutomation("run_scene", "auto.run_scene", "Run a shared scene",
                     lambda a: f"run the scene “{a['scene']}”", _scene_validate, _scene_run),
    RemoteAutomation("play_music", "auto.play_music", "Play music",
                     lambda a: f"play “{a['query'][:80]}” on Spotify", lambda a: {"query": _text(a, "query", 120)},
                     _music_run),
    RemoteAutomation("ask", "auto.ask", "Ask SAINT a question",
                     lambda a: f"answer a question: “{a['question'][:120]}”",
                     lambda a: {"question": _text(a, "question", 600)}, _ask_run),
]}


def catalog(peer=None) -> List[dict]:
    """What can be asked, and (for ``peer``) whether it would run, ask, or be refused."""
    out = []
    for a in REGISTRY.values():
        d = {"name": a.name, "title": a.title, "permission": a.perm}
        if peer is not None:
            d["policy"] = peer.permission(a.perm)
        out.append(d)
    return out


def run(ctx, name: str, args: dict) -> dict:
    """The ``automation.run`` request."""
    auto = REGISTRY.get(name)
    if auto is None:
        raise LinkError(f"I don't know an automation called “{name}”.", "unknown_automation")
    args = auto.validate(args if isinstance(args, dict) else {})
    ctx.require(auto.perm, auto.describe(args))
    log.info("link.automation.run peer=%s name=%s", ctx.peer.name, name)
    try:
        result = auto.run(args, ctx)
    except LinkError:
        raise
    except Exception as e:
        log.exception("link.automation.failed %s", name)
        raise LinkError(f"That didn't work on this PC: {e}", "failed")
    return dict(result or {}, ok=True)

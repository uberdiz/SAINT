"""
modules/agent/llm.py

LLM function calling for requests the deterministic router didn't recognise
("could you put on something chill", "move Discord over to my other screen
and make it full size").

Runs Ollama's /api/chat with a curated list of explicit, validated tools
(``Tool.llm_exposed``). Streaming text is forwarded token-by-token; tool calls
are executed through the registry (schema validation + permission policy),
their real results are fed back, and the model writes the final answer from
those results. Tools that need confirmation are NOT executed — SAINT asks the
user first. Shell/file tools are never exposed.
"""

import json
import logging
import re
from typing import Callable, Dict, List, Optional, Tuple

import requests

from core.config import config
from core.events import event_bus, EventType
from modules.agent.confirm import confirmations, PendingAction
from modules.agent.output import ReplyGuard, clean_reply, extract_tool_calls, pseudo_answer, honest
from modules.automation.tools import get_tool_registry, llm_name

log = logging.getLogger("saint.agent.llm")

# Only offer tools when the request plausibly needs one: small local models
# otherwise call tools for ordinary chat.
_ACTION_HINT = re.compile(
    r"\b(play|put on|queue|skip|pause|resume|volume|louder|quieter|softer|mute|turn (?:it )?(?:up|down)|"
    r"music|song|playlist|spotify|listen|"
    r"remind|reminder|timer|alarm|schedule|every (day|morning|week)|tomorrow|"
    r"remember|forget|note|jot|write down|nudge|my (favou?rite|name)|do i (like|prefer)|"
    r"open|launch|close|quit|switch|window|monitor|screen|type|press|click|minimi|maximi|snap|"
    r"app|chrome|discord|browser|desktop|what'?s on|youtube|video|captions|subtitles|playback|speed)\b", re.I)

_TOOL_SYSTEM = (
    "You can act on the user's PC through the provided tools. Call a tool ONLY through the "
    "structured tool-call mechanism — never write JSON, function names, XML, or code in your "
    "reply. After a tool runs, reply with ONE short sentence — a plain confirmation like "
    "'Skipped it.' or 'Spotify is open.', or the direct answer. Never repeat the user's "
    "request. Never narrate what you're doing. Never mention tool names, function names, "
    "parameters, or internal reasoning. Never claim an action happened unless a tool result "
    "confirms it; if a tool fails, say so plainly in one short sentence."
)

_capability_cache: Dict[str, bool] = {}


def might_need_tool(text: str) -> bool:
    return bool(_ACTION_HINT.search(text or ""))


def model_supports_tools(model: str, base_url: str) -> bool:
    if model in _capability_cache:
        return _capability_cache[model]
    try:
        r = requests.post(base_url.replace("localhost", "127.0.0.1").rstrip("/") + "/api/show",
                          json={"model": model}, timeout=5)
        caps = r.json().get("capabilities", []) if r.status_code == 200 else []
    except Exception:
        caps = []
    ok = "tools" in caps
    _capability_cache[model] = ok
    log.info("llm.capabilities model=%s tools=%s", model, ok)
    return ok


def run_with_tools(messages: List[Dict], model: str, base_url: str, temperature: float, timeout: float,
                   on_token: Callable[[str], None], cancel_flag) -> Tuple[str, Dict]:
    """Chat with tool calling. Returns (final_text, info). Raises ProviderError subclasses."""
    from modules.ai.providers import ProviderError, ModelNotFoundError, ConnectionError as ProvConnErr

    registry = get_tool_registry()
    tools = registry.llm_tools()
    schemas = [t.to_llm_schema() for t in tools]
    msgs = [dict(m) for m in messages]
    if msgs and msgs[0]["role"] == "system":
        msgs[0]["content"] = msgs[0]["content"] + "\n\n" + _TOOL_SYSTEM
    else:
        msgs.insert(0, {"role": "system", "content": _TOOL_SYSTEM})

    user_text = next((m.get("content", "") for m in reversed(msgs) if m.get("role") == "user"), "")
    url = base_url.replace("localhost", "127.0.0.1").rstrip("/") + "/api/chat"
    max_steps = int(config.get("ai.max_tool_steps", 4))
    spoken: List[str] = []
    info = {"tool_calls": [], "expects_reply": False}

    max_tokens = int(config.get("ai.max_tokens", 0) or 0)
    for step in range(max_steps + 1):
        opts = {"temperature": temperature}
        if max_tokens > 0:
            opts["num_predict"] = max_tokens
        body = {"model": model, "messages": msgs, "stream": True, "keep_alive": -1, "options": opts}
        if step < max_steps and schemas:
            body["tools"] = schemas
        try:
            resp = requests.post(url, json=body, stream=True, timeout=timeout)
        except requests.RequestException as e:
            raise ProvConnErr(f"Network error contacting Ollama: {e}") from e
        if resp.status_code == 404:
            raise ModelNotFoundError(f"Model '{model}' not found. Run 'ollama pull {model}' first.")
        if resp.status_code != 200:
            raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        content, calls = [], []

        def emit(tok):
            spoken.append(tok)
            on_token(tok)

        guard = ReplyGuard(emit)
        with resp:
            for line in resp.iter_lines():
                if cancel_flag is not None and cancel_flag.is_set():
                    return "".join(spoken), info
                if not line:
                    continue
                try:
                    chunk = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                msg = chunk.get("message") or {}
                tok = msg.get("content") or ""
                if tok:
                    content.append(tok)
                    guard.feed(tok)
                calls.extend(msg.get("tool_calls") or [])
                if chunk.get("done"):
                    break
        held = guard.finish()

        if not calls and held.strip():
            # The model wrote its tool call (or a wrapped answer) as text.
            for name, args in extract_tool_calls(held):
                tool = registry.by_llm_name(name)
                if tool is not None and tool.llm_exposed:
                    calls.append({"function": {"name": llm_name(tool.name), "arguments": args}})
                elif not calls:
                    ans = pseudo_answer(name, args)
                    if ans:
                        emit(honest(clean_reply(ans), _any_ok(info)))
                        return "".join(spoken), info
            if calls:
                log.info("llm.textual_tool_calls %s", [c["function"]["name"] for c in calls])
            else:
                leftover = honest(clean_reply(held, user_text), _any_ok(info))
                if leftover:
                    if leftover != held.strip():
                        log.info("llm.reply_rewritten held=%r", held[:80])
                    emit(leftover)

        if not calls:
            final = "".join(spoken).strip()
            if not final:
                final = "Sorry, I couldn't work that out."
                emit(final)
            return "".join(spoken), info

        msgs.append({"role": "assistant", "content": "".join(content), "tool_calls": calls})
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool = registry.by_llm_name(name)
            if tool is None or not tool.llm_exposed:
                result = {"success": False, "error": f"No such tool: {name}"}
            else:
                event_bus.emit_event(EventType.AGENT_INTENT, {"intent": f"llm:{tool.name}", "source": "llm"})
                log.info("llm.tool_call tool=%s args=%s", tool.name, _log_args(args))
                res = registry.execute(tool.name, **args)
                log.info("llm.tool_result tool=%s ok=%s error=%s", tool.name, res.success, (res.error or "")[:120])
                info["tool_calls"].append({"tool": tool.name, "success": res.success, "error": res.error})
                if res.error_code == "CONFIRM_REQUIRED":
                    tname, targs = tool.name, dict(res.args or args)

                    def run_confirmed(tname=tname, targs=targs):
                        r2 = registry.execute(tname, _confirmed=True, **targs)
                        return "Done." if r2.success else (r2.error or "That didn't work.")
                    confirmations.ask(PendingAction(f"run {tname} {targs}", run_confirmed, tool=tname))
                    question = f" That needs your OK — should I go ahead with {tool.description.lower()}?"
                    on_token(question)
                    spoken.append(question)
                    info["expects_reply"] = True
                    return "".join(spoken), info
                if res.error_code in ("AMBIGUOUS_WINDOW", "NO_BROWSER"):
                    # Ask which window (or whether to open a browser), then run
                    # the same call again with the answer.
                    from modules.agent.desktop_intents import _ask_which, _offer_browser
                    from modules.agent.router import Reply
                    tname, targs = tool.name, dict(res.args or args)

                    def again(tname=tname, targs=targs):
                        r2 = registry.execute(tname, **targs)
                        return Reply("Done." if r2.success else (r2.error or "That didn't work."), ok=r2.success)
                    asked = _ask_which(again) or _offer_browser(again)
                    if asked is not None:
                        on_token(asked.text)
                        spoken.append(asked.text)
                        info["expects_reply"] = True
                        return "".join(spoken), info
                result = res.to_dict()
                result["result"] = _compact(result.get("result"))
            msgs.append({"role": "tool", "content": json.dumps(result, default=str)[:4000], "tool_name": name})
        if spoken and not "".join(spoken).endswith((" ", "\n")):
            on_token(" ")
            spoken.append(" ")
    return "".join(spoken), info


def _any_ok(info: Dict) -> bool:
    return any(c.get("success") for c in info.get("tool_calls", []))


def _log_args(args: Dict) -> str:
    """Tool arguments for the log, with long/free text shortened."""
    out = {}
    for k, v in (args or {}).items():
        out[k] = (v[:40] + "…") if isinstance(v, str) and len(v) > 40 else v
    return json.dumps(out, default=str)[:200]


def _compact(value, depth=0):
    """Trim big tool results (e.g. window lists) before sending them to the model."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        return {k: _compact(v, depth + 1) for k, v in list(value.items())[:25]
                if k not in ("item", "image", "rect")}
    if isinstance(value, list):
        return [_compact(v, depth + 1) for v in value[:15]]
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "..."
    return value


def complete(prompt: str, system: str = "", timeout: float = 45.0) -> str:
    """One-shot, non-streaming answer from the configured local model (used
    for small agent sub-tasks such as explaining an error on screen).
    Returns '' on failure; the output is cleaned of any internal markup."""
    from modules.ai.module import AIModule
    base = config.get("ai.base_url", "http://localhost:11434")
    try:
        model, _ = AIModule._resolve_model(None, "ollama", base, config.get("ai.model", ""))
        opts = {"temperature": 0.3}
        cap = int(config.get("ai.max_tokens", 0) or 0)
        if cap > 0:
            opts["num_predict"] = cap
        r = requests.post(base.replace("localhost", "127.0.0.1").rstrip("/") + "/api/chat", timeout=timeout,
                          json={"model": model, "stream": False, "keep_alive": -1, "options": opts,
                                "messages": ([{"role": "system", "content": system}] if system else []) +
                                            [{"role": "user", "content": prompt}]})
        if r.status_code != 200:
            log.warning("llm.complete HTTP %s", r.status_code)
            return ""
        return clean_reply((r.json().get("message") or {}).get("content", ""), prompt)
    except Exception as e:
        log.warning("llm.complete_failed %s", e)
        return ""

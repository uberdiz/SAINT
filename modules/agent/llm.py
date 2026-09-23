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
from modules.automation.tools import get_tool_registry, llm_name

log = logging.getLogger("saint.agent.llm")

# Only offer tools when the request plausibly needs one: small local models
# otherwise call tools for ordinary chat.
_ACTION_HINT = re.compile(
    r"\b(play|put on|queue|skip|pause|resume|volume|music|song|playlist|spotify|listen|"
    r"remind|reminder|timer|alarm|schedule|every (day|morning|week)|tomorrow|"
    r"remember|forget|note|jot|write down|nudge|my (favou?rite|name)|do i (like|prefer)|"
    r"open|launch|close|quit|switch|window|monitor|screen|type|press|click|minimi|maximi|snap|"
    r"app|chrome|discord|browser|desktop|what'?s on)\b", re.I)

_TOOL_SYSTEM = (
    "You can act on the user's PC through the provided tools. Call a tool only when the "
    "user asks for an action or for information a tool provides. Never claim you did "
    "something unless a tool result says it succeeded; if a tool fails, say so plainly. "
    "Keep answers short — they are spoken aloud."
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

    url = base_url.replace("localhost", "127.0.0.1").rstrip("/") + "/api/chat"
    max_steps = int(config.get("ai.max_tool_steps", 4))
    spoken: List[str] = []
    info = {"tool_calls": [], "expects_reply": False}

    for step in range(max_steps + 1):
        body = {"model": model, "messages": msgs, "stream": True, "keep_alive": -1,
                "options": {"temperature": temperature}}
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
                    spoken.append(tok)
                    on_token(tok)
                calls.extend(msg.get("tool_calls") or [])
                if chunk.get("done"):
                    break

        if not calls:
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
                res = registry.execute(tool.name, **args)
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
                result = res.to_dict()
                result["result"] = _compact(result.get("result"))
            msgs.append({"role": "tool", "content": json.dumps(result, default=str)[:4000], "tool_name": name})
        if spoken and not "".join(spoken).endswith((" ", "\n")):
            on_token(" ")
            spoken.append(" ")
    return "".join(spoken), info


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

"""
modules/mcp/manager.py

Which MCP servers SAINT runs, and their tools as ordinary SAINT tools.

Servers come from Settings → MCP (``mcp.servers``) and from data/mcp.json, in
the same shape most MCP servers document for desktop apps, so a config can be
pasted as is:

    {"mcpServers": {
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/me/Documents"]},
        "github":     {"url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "Bearer ..."}},
        "notes":      {"command": "python", "args": ["notes_server.py"], "env": {"NOTES_DIR": "D:/notes"},
                       "trust": "allow", "disabled": false}}}

Every tool a server lists is registered as ``mcp.<server>.<tool>`` with its
input schema, so it goes through the same path as SAINT's own tools: argument
validation, the permission policy and confirmations. Tools a server marks
read-only run straight away; everything else is "high" risk and SAINT asks
first, unless that server is set to ``"trust": "allow"`` (``"confirm"`` makes
every tool ask).

Only tools that fit the request are shown to the language model (a server
named in the request, or tools whose name and description match it), so a
small local model isn't flooded with schemas.
"""

import json
import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional

from core.config import config
from core.paths import data_path
from modules.mcp.client import HttpTransport, MCPClient, MCPError, StdioTransport, result_text

log = logging.getLogger("saint.mcp")

_STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "my", "me", "please", "can", "you",
         "could", "would", "what", "is", "are", "it", "this", "that", "from", "use", "using", "via", "saint", "hey",
         "get", "make", "do", "show", "tell", "about", "all", "some", "any", "new", "into", "at", "by", "be", "i"}


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return s[:40] or "server"


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3 and w not in _STOP}


def _params(schema: dict) -> Dict[str, dict]:
    """An MCP input schema as SAINT's parameter specs."""
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    out = {}
    for pname, p in props.items():
        if not isinstance(p, dict):
            p = {}
        t = p.get("type", "string")
        if isinstance(t, list):
            t = next((x for x in t if x != "null"), "string")
        if t not in ("string", "integer", "number", "boolean", "array", "object"):
            t = "string"
        spec = {"type": t, "description": str(p.get("description", ""))[:200], "required": pname in required}
        if isinstance(p.get("enum"), list) and p["enum"]:
            spec["enum"] = p["enum"]
        if t == "array":
            items = p.get("items") or {}
            it = items.get("type") if isinstance(items, dict) else "string"
            spec["items"] = it if it in ("string", "integer", "number", "boolean", "object") else "string"
        out[pname] = spec
    return out


class _Server:
    def __init__(self, name: str, spec: dict):
        self.name, self.spec = name, spec
        self.key = slug(name)
        self.client: Optional[MCPClient] = None
        self.tools: List[dict] = []
        self.error = ""
        self.connected_at = 0.0
        self.lock = threading.Lock()


class MCPManager:
    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._servers: Dict[str, _Server] = {}
        self._lock = threading.RLock()
        self._started = False

    # ------------------------------------------------------------------ #
    @property
    def path(self) -> str:
        return self._path or str(data_path("mcp.json"))

    def configured(self) -> Dict[str, dict]:
        """Every server SAINT knows about: Settings first, then data/mcp.json."""
        out: Dict[str, dict] = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
            out.update({k: v for k, v in servers.items() if isinstance(v, dict)})
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("mcp.config_unreadable %s: %s", self.path, e)
        conf = config.get("mcp.servers", {}) or {}
        if isinstance(conf, dict):
            out.update({k: v for k, v in conf.items() if isinstance(v, dict)})
        return out

    @staticmethod
    def _transport(name: str, spec: dict):
        if spec.get("url"):
            return HttpTransport(str(spec["url"]), spec.get("headers") or {}, name=name)
        if spec.get("command"):
            return StdioTransport(str(spec["command"]), [str(a) for a in spec.get("args") or []],
                                  spec.get("env") or {}, spec.get("cwd"), name=name)
        raise MCPError("needs a \"command\" (a program to run) or a \"url\"")

    # ------------------------------------------------------------------ #
    def start(self, wait: bool = False):
        """Connect every enabled server (in the background unless ``wait``)."""
        if not config.get("mcp.enabled", True):
            return
        self._started = True
        servers = {k: v for k, v in self.configured().items() if not v.get("disabled")}
        threads = []
        for name, spec in servers.items():
            t = threading.Thread(target=self._connect, args=(name, spec), daemon=True, name=f"mcp-{slug(name)}")
            t.start()
            threads.append(t)
        if wait:
            for t in threads:
                t.join(float(config.get("mcp.connect_timeout_sec", 60)) + 5)

    def _connect(self, name: str, spec: dict) -> _Server:
        srv = _Server(name, spec)
        with self._lock:
            old = self._servers.get(srv.key)
            self._servers[srv.key] = srv
        if old is not None:
            self._drop(old)
        with srv.lock:
            try:
                client = MCPClient(self._transport(name, spec), name=name)
                client.initialize(timeout=float(config.get("mcp.connect_timeout_sec", 60)))
                srv.client = client
                srv.tools = client.list_tools()
                srv.connected_at = time.time()
                self._register(srv)
                log.info("mcp.connected %s tools=%d (%s)", name, len(srv.tools),
                         (client.server_info or {}).get("name", "?"))
            except Exception as e:
                srv.error = str(e)
                log.warning("mcp.connect_failed %s: %s", name, e)
                if srv.client is not None:
                    srv.client.close()
                    srv.client = None
        return srv

    def stop(self):
        with self._lock:
            servers, self._servers = list(self._servers.values()), {}
        for srv in servers:
            self._drop(srv)
        self._started = False

    def reload(self, wait: bool = True) -> List[dict]:
        self.stop()
        self.start(wait=wait)
        return self.status()

    def _drop(self, srv: _Server):
        from modules.automation.tools import get_tool_registry
        reg = get_tool_registry()
        for t in reg.list_tools():
            if t.name.startswith(f"mcp.{srv.key}."):
                reg.unregister(t.name)
        if srv.client is not None:
            try:
                srv.client.close()
            except Exception:
                pass
            srv.client = None

    # ------------------------------------------------------------------ #
    def _level(self, srv: _Server, tool: dict):
        from modules.automation.tools import PermissionLevel
        trust = str(srv.spec.get("trust", "auto")).lower()
        if trust == "allow":
            return PermissionLevel.MEDIUM
        if trust == "confirm":
            return PermissionLevel.HIGH
        ann = tool.get("annotations") or {}
        if ann.get("readOnlyHint"):
            return PermissionLevel.LOW
        return PermissionLevel.HIGH          # may change things: SAINT asks first

    def _register(self, srv: _Server):
        from modules.automation.tools import Tool, ToolError, get_tool_registry
        reg = get_tool_registry()
        for tool in srv.tools:
            original = tool["name"]
            full = f"mcp.{srv.key}.{slug(original)}"
            desc = (tool.get("description") or (tool.get("annotations") or {}).get("title") or original).strip()
            desc = re.sub(r"\s+", " ", desc)[:280] + f" (from the {srv.name} MCP server)"

            def run(_srv=srv, _name=original, **kwargs):
                if _srv.client is None or getattr(_srv.client.transport, "closed", False):
                    raise ToolError(f"The {_srv.name} MCP server isn't connected"
                                    f"{': ' + _srv.error if _srv.error else ''}.", "UNAVAILABLE")
                try:
                    res = _srv.client.call_tool(_name, kwargs, timeout=float(config.get("mcp.call_timeout_sec", 120)))
                except MCPError as e:
                    raise ToolError(f"{_srv.name}: {e}", "MCP_ERROR") from e
                text = result_text(res)
                if res.get("isError"):
                    raise ToolError(text or f"{_srv.name} couldn't do that.", "MCP_TOOL_ERROR")
                return {"summary": text or "Done.", "server": _srv.name, "tool": _name}

            reg.register(Tool(full, desc, {}, self._level(srv, tool), run, parameters=_params(tool.get("inputSchema")),
                              llm_exposed=True, category="mcp"))

    # ------------------------------------------------------------------ #
    def status(self) -> List[dict]:
        with self._lock:
            servers = list(self._servers.values())
        out = []
        for srv in servers:
            out.append({"name": srv.name, "connected": srv.client is not None, "tools": [t["name"] for t in srv.tools],
                        "error": srv.error, "kind": "url" if srv.spec.get("url") else "command"})
        known = {s["name"] for s in out}
        for name, spec in self.configured().items():
            if name not in known:
                out.append({"name": name, "connected": False, "tools": [], "kind": "url" if spec.get("url") else "command",
                            "error": "turned off" if spec.get("disabled") else "not started"})
        return out

    def connected(self) -> bool:
        with self._lock:
            return any(s.client is not None for s in self._servers.values())

    def relevant(self, text: str, limit: int = 10) -> List[str]:
        """Names of the MCP tools worth showing the model for ``text``: every tool of a server it names,
        otherwise the tools whose name/description share at least two words with it."""
        with self._lock:
            servers = [s for s in self._servers.values() if s.client is not None]
        if not servers:
            return []
        low = (text or "").lower()
        words = _words(text)
        named = [s for s in servers if re.search(rf"\b{re.escape(s.name.lower())}\b|\b{re.escape(s.key)}\b", low)]
        if named or re.search(r"\bmcp\b", low):
            pick = named or servers
            return [f"mcp.{s.key}.{slug(t['name'])}" for s in pick for t in s.tools][:max(limit, 20)]
        scored = []
        for s in servers:
            for t in s.tools:
                name_words = _words(t["name"].replace("_", " ").replace("-", " "))
                desc_words = _words(t.get("description", ""))
                score = 2 * len(words & name_words) + len(words & desc_words)
                if score >= 2:
                    scored.append((score, f"mcp.{s.key}.{slug(t['name'])}"))
        return [n for _sc, n in sorted(scored, reverse=True)[:limit]]

    def describe(self) -> str:
        st = self.status()
        if not st:
            return ("No MCP servers are set up. Add them in Settings → MCP (paste the server's JSON), then say "
                    "“reload MCP servers”.")
        parts = []
        for s in st:
            if s["connected"]:
                tools = ", ".join(t.replace("_", " ") for t in s["tools"][:6])
                more = f" and {len(s['tools']) - 6} more" if len(s["tools"]) > 6 else ""
                parts.append(f"{s['name']} ({len(s['tools'])} tools: {tools}{more})")
            else:
                parts.append(f"{s['name']} (not connected: {s['error']})")
        return "MCP servers: " + "; ".join(parts) + "."


mcp_manager = MCPManager()

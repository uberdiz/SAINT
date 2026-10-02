"""
modules/mcp/client.py

A small Model Context Protocol client: initialize, list tools, call a tool.

Two transports, as the spec defines them:

    stdio            SAINT starts the server program and speaks newline-delimited
                     JSON-RPC on its stdin/stdout ("command" + "args" in the config).
    Streamable HTTP  JSON-RPC POSTed to a URL; the answer is JSON or a short SSE
                     stream. The Mcp-Session-Id the server hands out is sent back.

Only what SAINT needs is implemented. Requests the server makes of SAINT
(sampling, elicitation, roots) are declined; "ping" is answered.
"""

import itertools
import json
import logging
import os
import shutil
import subprocess
import threading
from typing import Any, Dict, List, Optional

log = logging.getLogger("saint.mcp")

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "SAINT", "version": "2.3"}


class MCPError(Exception):
    def __init__(self, message: str, code: int = -1):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------- #
# Transports
# ---------------------------------------------------------------------- #
class StdioTransport:
    def __init__(self, command: str, args: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None,
                 cwd: Optional[str] = None, name: str = "mcp"):
        self.command, self.args, self.env, self.cwd, self.name = command, list(args or []), dict(env or {}), cwd, name
        self.proc: Optional[subprocess.Popen] = None
        self._pending: Dict[int, dict] = {}
        self._events: Dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self.on_request = None            # (method, params) -> result, for server -> client requests
        self.on_notification = None       # (method, params) -> None
        self.closed = False

    def start(self):
        exe = shutil.which(self.command) or self.command        # "npx" -> npx.cmd on Windows
        env = dict(os.environ)
        env.update({k: str(v) for k, v in self.env.items()})
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}      # no console window
        try:
            self.proc = subprocess.Popen([exe] + self.args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, cwd=self.cwd or None, env=env, **kw)
        except OSError as e:
            raise MCPError(f"couldn't start “{self.command}” ({e}). Is it installed and on the PATH?") from e
        threading.Thread(target=self._read, daemon=True, name=f"mcp-{self.name}-out").start()
        threading.Thread(target=self._drain_stderr, daemon=True, name=f"mcp-{self.name}-err").start()

    def _drain_stderr(self):
        try:
            for line in self.proc.stderr:
                log.debug("mcp.%s.stderr %s", self.name, line.decode("utf-8", "replace").rstrip()[:300])
        except Exception:
            pass

    def _read(self):
        try:
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.debug("mcp.%s.not_json %s", self.name, line[:200])
                    continue
                for m in msg if isinstance(msg, list) else [msg]:
                    self._dispatch(m)
        except Exception:
            log.debug("mcp.%s.read_failed", self.name, exc_info=True)
        finally:
            self.closed = True
            with self._lock:
                for ev in self._events.values():
                    ev.set()

    def _dispatch(self, msg: dict):
        if not isinstance(msg, dict):
            return
        if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
            with self._lock:
                ev = self._events.get(msg["id"])
                if ev is not None:
                    self._pending[msg["id"]] = msg
                    ev.set()
            return
        method = msg.get("method")
        if not method:
            return
        if "id" in msg:                                       # the server asks something of SAINT
            try:
                result = self.on_request(method, msg.get("params") or {}) if self.on_request else None
                reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result if result is not None else {}}
            except MCPError as e:
                reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": e.code, "message": str(e)}}
            self._write(reply)
        elif self.on_notification:
            self.on_notification(method, msg.get("params") or {})

    def _write(self, msg: dict):
        if self.proc is None or self.proc.stdin is None:
            raise MCPError("the server isn't running")
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except OSError as e:
                raise MCPError(f"the server stopped ({e})") from e

    def request(self, msg: dict, timeout: float) -> dict:
        ev = threading.Event()
        with self._lock:
            self._events[msg["id"]] = ev
        try:
            self._write(msg)
            if not ev.wait(timeout):
                raise MCPError(f"no answer within {int(timeout)} s", -32001)
            with self._lock:
                reply = self._pending.pop(msg["id"], None)
            if reply is None:
                raise MCPError("the server stopped")
            return reply
        finally:
            with self._lock:
                self._events.pop(msg["id"], None)

    def notify(self, msg: dict):
        self._write(msg)

    def close(self):
        self.closed = True
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.stdin.close()
            except Exception:
                pass
            try:
                p.terminate()
                p.wait(3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


class HttpTransport:
    """Streamable HTTP: every message is a POST; the answer is JSON or an SSE stream."""

    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None, name: str = "mcp"):
        self.url, self.headers, self.name = url, dict(headers or {}), name
        self.session_id = ""
        self.protocol = ""
        self.closed = False
        self.on_request = None
        self.on_notification = None

    def start(self):
        pass

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **self.headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            h["MCP-Protocol-Version"] = self.protocol
        return h

    def _post(self, msg: dict, timeout: float):
        import requests
        try:
            return requests.post(self.url, json=msg, headers=self._headers(), timeout=timeout,
                                 stream=True)
        except requests.RequestException as e:
            raise MCPError(f"couldn't reach {self.url} ({e})") from e

    def request(self, msg: dict, timeout: float) -> dict:
        r = self._post(msg, timeout)
        with r:
            if r.headers.get("Mcp-Session-Id"):
                self.session_id = r.headers["Mcp-Session-Id"]
            if r.status_code in (401, 403):
                raise MCPError(f"{self.url} refused SAINT ({r.status_code}) — check the server's token/headers")
            if r.status_code >= 400:
                raise MCPError(f"{self.url} answered HTTP {r.status_code}: {r.text[:200]}")
            ctype = r.headers.get("Content-Type", "")
            if "text/event-stream" in ctype:
                data = []
                for raw in r.iter_lines(decode_unicode=True):
                    if raw is None:
                        continue
                    if raw.startswith("data:"):
                        data.append(raw[5:].lstrip())
                    elif raw == "" and data:
                        try:
                            m = json.loads("\n".join(data))
                        except ValueError:
                            m = None
                        data = []
                        if isinstance(m, dict) and m.get("id") == msg["id"] and ("result" in m or "error" in m):
                            return m
                        if isinstance(m, dict) and m.get("method") and self.on_notification and "id" not in m:
                            self.on_notification(m["method"], m.get("params") or {})
                raise MCPError("the server's stream ended without an answer")
            try:
                m = r.json()
            except ValueError as e:
                raise MCPError("the server sent back something that isn't JSON") from e
            if isinstance(m, list):
                m = next((x for x in m if isinstance(x, dict) and x.get("id") == msg["id"]), {})
            return m

    def notify(self, msg: dict):
        try:
            self._post(msg, 10).close()
        except MCPError:
            pass

    def close(self):
        self.closed = True
        if self.session_id:
            try:
                import requests
                requests.delete(self.url, headers=self._headers(), timeout=3)
            except Exception:
                pass


# ---------------------------------------------------------------------- #
# The client
# ---------------------------------------------------------------------- #
class MCPClient:
    def __init__(self, transport, name: str = "mcp"):
        self.transport = transport
        self.name = name
        self._ids = itertools.count(1)
        self.server_info: Dict[str, Any] = {}
        self.capabilities: Dict[str, Any] = {}
        self.tools_changed = False
        transport.on_request = self._on_request
        transport.on_notification = self._on_notification

    @staticmethod
    def _on_request(method: str, params: dict):
        if method == "ping":
            return {}
        raise MCPError(f"SAINT doesn't support {method}", -32601)

    def _on_notification(self, method: str, params: dict):
        if method == "notifications/tools/list_changed":
            self.tools_changed = True

    def _call(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        msg = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            msg["params"] = params
        reply = self.transport.request(msg, timeout)
        if "error" in reply:
            err = reply["error"] or {}
            raise MCPError(str(err.get("message") or "the server reported an error"), int(err.get("code") or -1))
        return reply.get("result") or {}

    def initialize(self, timeout: float = 60.0) -> dict:
        self.transport.start()
        res = self._call("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                        "clientInfo": CLIENT_INFO}, timeout)
        self.server_info = res.get("serverInfo") or {}
        self.capabilities = res.get("capabilities") or {}
        if isinstance(self.transport, HttpTransport):
            self.transport.protocol = str(res.get("protocolVersion") or PROTOCOL_VERSION)
        self.transport.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return res

    def list_tools(self) -> List[dict]:
        tools, cursor = [], None
        for _ in range(20):
            res = self._call("tools/list", {"cursor": cursor} if cursor else {})
            tools += [t for t in res.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools_changed = False
        return tools

    def call_tool(self, name: str, arguments: dict, timeout: float = 120.0) -> dict:
        return self._call("tools/call", {"name": name, "arguments": arguments or {}}, timeout)

    def close(self):
        self.transport.close()


def result_text(result: dict, limit: int = 4000) -> str:
    """A tool result's content as plain text for the model (images and binary noted, not inlined)."""
    parts = []
    for c in result.get("content") or []:
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t == "text":
            parts.append(str(c.get("text", "")))
        elif t == "resource":
            r = c.get("resource") or {}
            parts.append(str(r.get("text") or f"[resource {r.get('uri', '')}]"))
        elif t == "resource_link":
            parts.append(f"[{c.get('name') or 'link'}: {c.get('uri', '')}]")
        elif t in ("image", "audio"):
            parts.append(f"[{t}]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(p for p in parts if p).strip()
    return text if len(text) <= limit else text[:limit] + " …(cut)"

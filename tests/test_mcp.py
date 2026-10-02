"""SAINT as an MCP client (modules/mcp): a real server process over stdio and a Streamable HTTP server,
their tools registered as SAINT tools behind the usual validation and permission checks."""

import json
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.config import config

SERVER = textwrap.dedent('''
    import json, sys
    TOOLS = [
        {"name": "add_note", "description": "Save a note in the notes app",
         "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
        {"name": "list_notes", "description": "List the saved notes", "annotations": {"readOnlyHint": True},
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "explode", "description": "Always fails", "inputSchema": {"type": "object", "properties": {}}},
    ]
    notes = []
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        m, p = msg["method"], msg.get("params") or {}
        if m == "initialize":
            res = {"protocolVersion": p["protocolVersion"], "capabilities": {"tools": {}},
                   "serverInfo": {"name": "notes", "version": "1"}}
        elif m == "tools/list":
            res = {"tools": TOOLS}
        elif m == "tools/call":
            name, args = p["name"], p.get("arguments") or {}
            if name == "add_note":
                notes.append(args["text"])
                res = {"content": [{"type": "text", "text": "Saved: " + args["text"]}]}
            elif name == "list_notes":
                res = {"content": [{"type": "text", "text": "; ".join(notes) or "no notes"}]}
            else:
                res = {"content": [{"type": "text", "text": "it blew up"}], "isError": True}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "nope"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}), flush=True)
''')


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    from modules.mcp.manager import MCPManager
    script = tmp_path / "notes_server.py"
    script.write_text(SERVER)
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {"Notes": {"command": sys.executable,
                                                                           "args": [str(script)]}}}))
    monkeypatch.setitem(config._data, "mcp", {"enabled": True, "servers": {}, "connect_timeout_sec": 15})
    m = MCPManager(str(tmp_path / "mcp.json"))
    import modules.mcp.manager as mod
    monkeypatch.setattr(mod, "mcp_manager", m)
    m.start(wait=True)
    yield m
    m.stop()


def test_a_stdio_server_s_tools_become_saint_tools(mcp):
    from modules.automation.tools import get_tool_registry, PermissionLevel
    st = mcp.status()
    assert st[0]["connected"] and set(st[0]["tools"]) == {"add_note", "list_notes", "explode"}
    reg = get_tool_registry()
    add, lst = reg.get("mcp.notes.add_note"), reg.get("mcp.notes.list_notes")
    assert add.permission == PermissionLevel.HIGH and lst.permission == PermissionLevel.LOW   # read-only runs at once
    assert add.to_llm_schema()["function"]["name"] == "mcp__notes__add_note"
    # anything that may change something waits for a yes, like SAINT's own risky tools
    assert reg.execute("mcp.notes.add_note", text="buy milk").error_code == "CONFIRM_REQUIRED"
    assert reg.execute("mcp.notes.add_note", _confirmed=True, text="buy milk").success
    res = reg.execute("mcp.notes.list_notes")
    assert res.success and res.result["summary"] == "buy milk"
    bad = reg.execute("mcp.notes.explode", _confirmed=True)
    assert not bad.success and "it blew up" in bad.error
    assert reg.execute("mcp.notes.add_note", _confirmed=True).error_code == "INVALID_PARAMS"


def test_only_tools_that_fit_the_request_are_shown_to_the_model(mcp):
    from modules.agent.llm import might_need_tool
    assert mcp.relevant("list everything I saved") == ["mcp.notes.list_notes"]
    assert len(mcp.relevant("use notes to save something")) == 3          # the server named: all its tools
    assert mcp.relevant("what's the weather") == []
    assert might_need_tool("list everything I saved") and not might_need_tool("what's the weather like")


def test_list_and_reload_by_voice(mcp):
    from modules.agent.router import route
    r = route("what MCP servers do you have").run()
    assert "Notes (3 tools" in r.text
    r = route("reload MCP servers").run()
    assert r.ok and "Connected 1 MCP server with 3 tools" in r.text
    from modules.automation.tools import get_tool_registry
    assert get_tool_registry().get("mcp.notes.add_note") is not None


def test_a_server_that_wont_start_is_reported_not_fatal(tmp_path, monkeypatch):
    from modules.mcp.manager import MCPManager
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {"ghost": {"command": "no-such-program-xyz"}}}))
    monkeypatch.setitem(config._data, "mcp", {"enabled": True, "servers": {}})
    m = MCPManager(str(tmp_path / "mcp.json"))
    m.start(wait=True)
    st = m.status()
    assert not st[0]["connected"] and "couldn't start" in st[0]["error"]


def test_streamable_http_server(monkeypatch, tmp_path):
    from modules.mcp.client import HttpTransport, MCPClient, result_text
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.setdefault("sessions", []).append(self.headers.get("Mcp-Session-Id"))
            if "id" not in msg:
                self.send_response(202)
                self.end_headers()
                return
            if msg["method"] == "initialize":
                res = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "web"}}
            elif msg["method"] == "tools/list":
                res = {"tools": [{"name": "echo", "inputSchema": {"type": "object",
                                                                  "properties": {"x": {"type": "string"}}}}]}
            else:
                res = {"content": [{"type": "text", "text": "echo " + msg["params"]["arguments"]["x"]}]}
            body = ("event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}) +
                    "\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Mcp-Session-Id", "abc123")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = MCPClient(HttpTransport(f"http://127.0.0.1:{srv.server_port}/mcp"))
        c.initialize(timeout=5)
        assert [t["name"] for t in c.list_tools()] == ["echo"]
        assert result_text(c.call_tool("echo", {"x": "hi"}, timeout=5)) == "echo hi"
        assert seen["sessions"][0] is None and seen["sessions"][-1] == "abc123"
        c.close()
    finally:
        srv.shutdown()

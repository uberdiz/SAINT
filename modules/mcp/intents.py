"""
modules/mcp/intents.py

Deterministic commands about MCP servers (no model needed):

    "what MCP servers do you have" / "list your MCP tools"
    "reload MCP servers" / "reconnect the MCP servers"

Asking an MCP server to *do* something ("use github to list my open pull requests")
goes to the language model with that server's tools (modules/agent/llm.py), since
picking the tool and its arguments is the reasoning part.
"""

import re
from typing import Optional

_LIST = re.compile(r"^(?:what|which)\s+(?:mcp|model context protocol)\s+(?:servers?|tools?)\b.*$|"
                   r"^(?:list|show)(?:\s+me)?\s+(?:your\s+|my\s+|the\s+)?(?:mcp|model context protocol)\s+"
                   r"(?:servers?|tools?)$|^(?:mcp|model context protocol)\s+status$", re.I)
_RELOAD = re.compile(r"^(?:reload|restart|reconnect|refresh)\s+(?:the\s+|my\s+|your\s+|all\s+)?(?:mcp|model context "
                     r"protocol)(?:\s+servers?)?$", re.I)


def parse_mcp(text: str):
    from modules.agent.router import Intent, Reply, _clean
    t = _clean(text).lower().strip(" .!?")
    if _LIST.match(t):
        from modules.mcp.manager import mcp_manager
        return Intent("mcp.list", lambda: Reply(mcp_manager.describe()), "mcp")
    if _RELOAD.match(t):
        def run():
            from modules.mcp.manager import mcp_manager
            st = mcp_manager.reload(wait=True)
            if not st:
                return Reply(mcp_manager.describe(), ok=False)
            ok = [s for s in st if s["connected"]]
            bad = [s for s in st if not s["connected"]]
            text = f"Connected {len(ok)} MCP server{'s' if len(ok) != 1 else ''}" + \
                (f" with {sum(len(s['tools']) for s in ok)} tools." if ok else ".")
            if bad:
                text += " Not connected: " + "; ".join(f"{s['name']} ({s['error']})" for s in bad) + "."
            return Reply(text, ok=not bad)
        return Intent("mcp.reload", run, "mcp")
    return None

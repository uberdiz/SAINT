"""
modules/agent/taskmgr_intents.py

Task Manager by voice (modules/desktop/taskmgr.py). Everything is answered
from the process list — Task Manager itself is only opened when asked for
("open task manager"), so a game or editor in front stays in front.
"""

import re
from typing import Optional

from modules.agent.router import Intent, Reply, _clean, run_tool

_PC = r"(?:(?:my|the|this)\s+)?(?:pc|computer|laptop|machine|system)"


def _mb(mb: float) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{int(mb)} MB"


def _processes_reply(r) -> str:
    ps = r["processes"]
    if not ps:
        return "Nothing much is running."
    if r["sort"] == "memory_mb":
        items = [f"{p['name']} ({_mb(p['memory_mb'])})" for p in ps[:5]]
        return "Using the most memory: " + ", ".join(items) + "."
    busy = [p for p in ps if p["cpu"] >= 1]
    if not busy:
        items = [f"{p['name']} ({_mb(p['memory_mb'])})" for p in sorted(ps, key=lambda x: -x["memory_mb"])[:5]]
        return "Nothing is working the CPU hard right now. The biggest programs are " + ", ".join(items) + "."
    return "Using the most CPU: " + ", ".join(f"{p['name']} ({p['cpu']:.0f}%)" for p in busy[:5]) + "."


def _health_reply(r) -> str:
    parts = [f"CPU {r['cpu_percent']}%", f"memory {r['memory_percent']}% ({r['memory_used_gb']} of "
                                         f"{r['memory_total_gb']} GB)"]
    g = r.get("gpu")
    if g:
        parts.append(f"GPU {g['percent']}% at {g['temp_c']}°C")
    text = "Your PC: " + ", ".join(parts) + "."
    if r.get("battery"):
        b = r["battery"]
        text += f" Battery {b['percent']}%{' and charging' if b['plugged'] else ''}."
    if r["warnings"]:
        text += " Heads up: " + "; ".join(r["warnings"]) + "."
    else:
        low = min(r["disks"], key=lambda d: d["free_gb"], default=None)
        text += " Everything looks healthy" + (f"; the fullest drive is {low['drive']} with {low['free_gb']} GB free"
                                               if low else "") + "."
    top = [x for x in r["top_cpu"] if x["cpu"] >= 5]
    if top:
        text += f" {top[0]['name']} is the busiest program ({top[0]['cpu']:.0f}% CPU)."
    if r["uptime_h"] >= 72:
        text += f" It's been on for {r['uptime_h'] / 24:.0f} days — a restart might help."
    return text


def parse_taskmgr(text: str) -> Optional[Intent]:
    t = _clean(text).lower().strip(" .!?")
    if not t:
        return None

    # ---- end task -------------------------------------------------------------------------
    m = re.match(r"^(?:end\s+(?:the\s+)?task(?:\s+(?:on|for))?|end\s+(?:the\s+)?process(?:es)?(?:\s+(?:of|for))?|"
                 r"kill\s+(?:the\s+)?process(?:es)?(?:\s+(?:of|for))?|force\s+(?:quit|close|kill|stop))\s+"
                 r"(?:the\s+|my\s+)?(?P<a>.+?)(?:\s+(?:app|process(?:es)?|program))?$|"
                 r"^(?:end|kill)\s+(?:the\s+|my\s+)?(?P<b>.+?)\s+(?:task|process(?:es)?)$", t)
    if m:
        name = (m.group("a") or m.group("b")).strip()
        if name and name not in ("it", "this", "that", "everything", "all"):
            return Intent("system.end_task", lambda: run_tool(
                "system.end_task", f"end {name} (like Task Manager's End task)",
                lambda r: f"Ended {r['name']}" + (f" ({r['ended']} processes)." if r["ended"] > 1 else ".")
                + (f" {r['denied']} of them needed admin rights and are still running." if r["denied"] else ""),
                name=name), "system")

    # ---- what's running -----------------------------------------------------------------------
    if re.match(r"^(?:what(?:'?s| is| are)|which)\s+(?:programs?\s+|apps?\s+|processes\s+|stuff\s+)?"
                r"(?:(?:is|are)\s+)?(?:running|open in the background|using\s+(?:the\s+most\s+|all\s+(?:my\s+|the\s+)?"
                r"|up\s+(?:my\s+|all\s+my\s+)?|my\s+)?(?:cpu|processor|memory|ram|resources))", t) or \
            re.match(r"^(?:show|list|tell)\s+(?:me\s+)?(?:what(?:'?s| is)\s+running|(?:the\s+|my\s+)?(?:running\s+)?"
                     r"(?:processes|programs|tasks))(?:\s+(?:running|open))?$", t) or \
            re.match(rf"^(?:why is|what(?:'?s| is))\s+{_PC}\s+(?:so\s+)?(?:slow|laggy|lagging)|"
                     rf"^what(?:'?s| is)\s+slowing\s+(?:down\s+)?{_PC}", t):
        sort = "memory" if re.search(r"\b(memory|ram)\b", t) else "cpu"
        return Intent("system.processes", lambda: run_tool(
            "system.processes", "see what's running", _processes_reply, sort=sort, limit=8), "system")

    # ---- device health ------------------------------------------------------------------------
    if re.match(rf"^(?:(?:check|show|give me|run)\s+(?:(?:on|me)\s+)?)?(?:the\s+|my\s+)?(?:device|pc|computer|system)\s+"
                rf"(?:health|status|performance|stats)(?:\s+(?:check|report))?$|"
                rf"^how(?:'?s| is| are)\s+{_PC}\s+(?:doing|holding up|running|performing)$|"
                rf"^(?:how much|what(?:'?s| is))\s+(?:(?:my|the)\s+)?(?:cpu|gpu|ram|memory)(?:\s+(?:usage|use|load|"
                rf"temp(?:erature)?))?(?:\s+(?:at|am i using|is being used|right now))*$|"
                rf"^(?:check|run a)\s+(?:health\s+)?(?:check(?:\s*up)?|diagnostics?)\s+(?:on\s+)?{_PC}$", t):
        return Intent("system.health", lambda: run_tool("system.health", "check your PC", _health_reply), "system")

    # ---- startup apps -------------------------------------------------------------------------
    if re.match(r"^(?:what|which)\s+(?:apps|programs)\s+(?:start|open|run|launch)\s+(?:with|when)\s+(?:windows|"
                r"(?:i|my pc|the pc|my computer)\s+(?:start|boot|turn)s?(?:\s+on|\s+up)?)|"
                r"^(?:show|list)\s+(?:me\s+)?(?:my\s+)?startup\s+(?:apps|programs)$", t):
        return Intent("system.startup_apps", lambda: run_tool(
            "system.startup_apps", "list startup apps",
            lambda r: ("These start with Windows: " + ", ".join(r["apps"][:12]) + ".") if r["apps"]
            else "Nothing extra starts with Windows."), "system")
    return None

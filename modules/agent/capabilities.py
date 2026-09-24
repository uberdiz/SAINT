"""
modules/agent/capabilities.py

Lightweight, deterministic domain scoring and a compact capability summary.

The router already parses common commands directly (modules/agent/router.py);
this module answers two other questions:

  1. "If we DO fall through to the LLM, which subsystem does this look like?"
     -> a hint in the system prompt so the model reaches for the right tool
     without a big dump of tool schemas. Not authoritative — the model may
     still ignore it if it decides the request is different.

  2. "What can SAINT actually do right now?" -> a short bullet list built from
     the live tool registry (each domain summarised, not every schema).

Keep this cheap: keyword scoring, no LLM, no I/O.
"""

from __future__ import annotations

import re
from typing import Dict, List

# domain -> (regex, weight). Regex matches on the lowered, cleaned input.
_SIGNALS: Dict[str, List] = {
    "spotify": [
        (r"\b(spotify|liked songs|smart shuffle|my mixes|song radio)\b", 3),
        (r"\b(song|track|artist|album|playlist|queue|shuffle|repeat)\b", 2),
        (r"\b(play|pause|resume|skip|previous)\b(?!\s+(?:the\s+)?video)", 1),
        (r"\b(similar to (?:this|that)|something like (?:this|that))\b", 2),
    ],
    "browser": [
        (r"\b(chrome|edge|firefox|opera|brave|browser|url|tab|website|webpage)\b", 3),
        (r"\b(youtube|google|reddit|github|twitch|wikipedia|amazon)\b", 3),
        (r"\bsearch\s+(?:google|youtube|the web|online|for)\b", 3),
        (r"\bopen\s+(?:up\s+)?(?:my\s+)?browser\b", 3),
    ],
    "desktop": [
        (r"\b(window|windows|maximi[sz]e|minimi[sz]e|resize|close|move)\b", 2),
        (r"\b(monitor|screen|display|second monitor|other screen|first monitor)\b", 2),
        (r"\b(open|launch|start|run|switch to|focus)\s+", 1),
        (r"\b(type|press|hit|click|double click|right click|scroll|drag)\b", 3),
    ],
    "vision": [
        (r"\bwhat(?:'s| is)\s+(?:on\s+)?(?:my|the)\s+(?:\w+\s+)?(?:screen|monitor|display)\b", 6),
        (r"\bwhat(?:'s| is)\s+on\s+(?:my|the)\s+\w+\s+(?:screen|monitor|display)\b", 6),
        (r"\b(look at|find (?:the|a)|locate|point to|show me)\b", 2),
        (r"\bwhat (?:am i looking at|do you see|does (?:it|the screen) say)\b", 6),
        (r"\b(read (?:this|it|the screen)|verify visually)\b", 3),
    ],
    "memory": [
        (r"\b(remember|don'?t forget|note that|keep in mind|from now on)\b", 3),
        (r"\b(forget|delete that|erase (?:that|it))\b", 3),
        (r"\bwhat do you (?:know|remember) about me\b", 4),
        (r"\bmy (?:preferences?|favou?rite|nickname)\b", 2),
    ],
    "automation": [
        (r"\b(every (?:day|morning|week|hour)|each (?:day|morning))\b", 3),
        (r"\b(remind me|set (?:a )?reminder|timer|alarm|schedule)\b", 3),
        (r"\b(tomorrow|later|in \d+ (?:minutes?|hours?)|at \d+\s*(?:am|pm)?)\b", 2),
    ],
    "web": [
        (r"\b(latest|current|today'?s?)\s+(?:news|headlines|updates?)\b", 3),
        (r"\bwhat(?:'s| is)\s+happening\b", 3),
        (r"\bwhat(?:'s| is)\s+the\s+(?:weather|forecast|temperature)\b", 3),
        (r"\bwho won\b", 2),
    ],
    "system": [
        (r"\bwhat time is it\b|\bwhat(?:'s| is)\s+the\s+time\b|\bwhat day is it\b", 3),
        (r"\bwhat can you do\b|\bhelp\b", 1),
    ],
}


def score_domains(text: str) -> Dict[str, int]:
    """Return {domain: score} for the domains that got any signal at all."""
    t = (text or "").lower()
    scores: Dict[str, int] = {}
    for domain, rules in _SIGNALS.items():
        s = 0
        for pattern, weight in rules:
            if re.search(pattern, t):
                s += weight
        if s > 0:
            scores[domain] = s
    return scores


def top_domain(text: str) -> str:
    """Highest-scoring domain, or 'general' if nothing matched."""
    scores = score_domains(text)
    return max(scores, key=scores.get) if scores else "general"


# ---------------------------------------------------------------------- #
# Capability summary
# ---------------------------------------------------------------------- #
_DOMAIN_LABELS = {
    "spotify":    "Spotify (play, pause, skip, search, playlists, recommendations, queue)",
    "desktop":    "Windows apps and windows (open, focus, close, move, resize, arrange, monitors)",
    "browser":    "Web browser (open a URL, search a site, click, type — uses your existing browser)",
    "vision":     "Screen understanding (what's on screen, read text, locate an element, describe a monitor)",
    "memory":     "Long-term memory (remember / forget / recall user facts and preferences)",
    "automation": "Reminders and scheduled commands (once, daily, weekly, conditional)",
    "system":     "Time / date and what SAINT can do",
    "web":        "Current information (news, weather, latest facts) — separate from browser control",
}


def capability_summary() -> str:
    """A short bullet list of what SAINT can actually do right now.

    Filters domains whose tool group is disabled/unregistered. Cheap and cached
    per-call by the tool registry itself.
    """
    from modules.automation.tools import get_tool_registry
    try:
        registered_domains = {t.name.split(".", 1)[0] for t in get_tool_registry().list_tools()}
    except Exception:
        registered_domains = set()
    # 'web' isn't a tool group but is served by parse_web/router — always include.
    lines = []
    for domain, label in _DOMAIN_LABELS.items():
        if domain in ("web", "system") or domain in registered_domains:
            lines.append(f"- {label}")
    return "You can help with:\n" + "\n".join(lines)

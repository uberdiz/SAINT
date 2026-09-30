"""
modules/agent/aliases.py

Personal aliases: "when I say the lab, I mean open my SAINT project in VS Code".

Stored in data/aliases.json as {"the lab": "open my saint project in vs code"}.
Before routing, an utterance that *is* an alias (optionally with "open",
"go to", "start" in front) runs the target command; an alias phrase inside a
longer request is replaced by its target when the target is a thing (a folder
or app name) rather than a command.
"""

import json
import os
import re
import threading
from typing import Dict, Optional

from core.paths import data_path

_Q = "[\"'“”]"
_DEFINE = re.compile(
    rf"^(?:from now on,?\s+)?(?:when(?:ever)?|if)\s+i\s+say\s+{_Q}?(?P<alias>.+?){_Q}?,?\s+"
    r"(?P<verb>i\s+mean|that\s+means|it\s+means|you\s+should|open|run)\s+(?P<target>.+?)[.!]?$", re.I)
_FORGET = re.compile(rf"^(?:forget|delete|remove)\s+(?:the\s+)?alias\s+{_Q}?(?P<alias>.+?){_Q}?[.!]?$", re.I)
_LIST = re.compile(r"^(?:what|which)\s+aliases\s+(?:do\s+i\s+have|have\s+i\s+(?:set|made))|^list\s+(?:my\s+)?aliases",
                   re.I)
_LEAD = re.compile(r"^(?:open|go to|start|run|launch|load|bring up)\s+", re.I)
# A target that is itself a command (vs. a thing to open).
_COMMAND_TARGET = re.compile(
    r"^(?:open|play|start|run|launch|go to|search|turn|set|put|move|close|save|restore|extract|"
    r"clean|show|pause|skip|remind|minimi[sz]e|maximi[sz]e|switch|focus|watch|tell|get)\b", re.I)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s']", " ", (text or "").lower()).split())


def _article(text: str) -> str:
    """'the lab' / 'my lab' / 'lab' all compare equal."""
    return re.sub(r"^(?:the|my)\s+", "", _norm(text))


class AliasStore:
    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return self._path or str(data_path("aliases.json"))

    def all(self) -> Dict[str, str]:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return {}

    def _save(self, data: Dict[str, str]):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def set(self, alias: str, target: str):
        data = self.all()
        data[_norm(alias)] = target.strip()
        with self._lock:
            self._save(data)

    def forget(self, alias: str) -> bool:
        data = self.all()
        key = _norm(alias)
        if key not in data:
            key = _norm(re.sub(r"^(?:the|my)\s+", "", alias, flags=re.I))
        if key not in data:
            return False
        data.pop(key)
        with self._lock:
            self._save(data)
        return True

    # ------------------------------------------------------------------ #
    def expand(self, text: str) -> str:
        """Return ``text`` with an alias resolved (unchanged if none)."""
        data = self.all()
        if not data:
            return text
        bare = _norm(re.sub(r"^(?:hey\s+)?saint[\s,]+", "", text or "", flags=re.I))
        said = {_article(bare), _article(_LEAD.sub("", bare))}
        for key, target in data.items():
            if _article(key) in said:
                return target if _COMMAND_TARGET.match(target) else f"open {target}"
        # An alias inside a longer request: "open the lab in vs code".
        for alias in sorted(data, key=len, reverse=True):
            target = data[alias]
            if _COMMAND_TARGET.match(target):
                continue
            pattern = re.compile(rf"\b(?:the\s+|my\s+)?{re.escape(alias)}\b", re.I)
            if pattern.search(text):
                return pattern.sub(lambda _m: target, text, count=1)
        return text


aliases = AliasStore()


def parse_alias_command(text: str) -> Optional[str]:
    """The reply for define / forget / list alias requests, else None."""
    t = " ".join((text or "").strip().split())
    t = re.sub(r"^(?:hey\s+)?saint[\s,]+", "", t, flags=re.I)
    if _LIST.match(t):
        data = aliases.all()
        if not data:
            return ("You haven't set any aliases yet. Say, for example, "
                    "“when I say the lab, I mean my SAINT project”.")
        return "; ".join(f"“{k}” means {v}" for k, v in list(data.items())[:8]) + "."
    m = _FORGET.match(t)
    if m:
        name = m.group("alias")
        return f"Forgot “{name}”." if aliases.forget(name) else f"I don't have an alias called “{name}”."
    m = _DEFINE.match(t)
    if m:
        alias, target = m.group("alias").strip(" ,"), m.group("target").strip(" ,")
        if len(alias.split()) > 5 or not target or _norm(alias) == _norm(target):
            return None
        verb = m.group("verb").lower()
        if verb in ("open", "run") and not _COMMAND_TARGET.match(target):
            target = f"{verb} {target}"
        aliases.set(alias, target)
        return f"Got it — “{alias}” means {target}."
    return None

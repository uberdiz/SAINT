"""
modules/agent/dictate.py

"Talk about an idea, get back a clean written prompt."

Flow:
  User: "SAINT, help me write a prompt about a chess app."
  SAINT: "Go ahead — talk it through. Say 'done' when finished."
  User rambles ...   [each utterance is captured, not executed as a command]
  User: "Done."
  SAINT: writes the cleaned prompt to a file, opens it, and reads a short summary.

The dictation session is a small state object stored on the module; the agent
consults it before regular intent routing, so ordinary Spotify/desktop
commands are frozen while a session is open. Say "cancel" / "never mind" to
throw it away.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from core.config import config
from core.paths import data_path

log = logging.getLogger("saint.dictate")


@dataclass
class DictationSession:
    topic: str = ""
    lines: List[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    last_touched: float = field(default_factory=time.time)


class DictationManager:
    """Single-user, single-session capture. Not thread-safe across processes."""

    START_RE = re.compile(
        r"^(?:help me\s+)?(?:write|draft|compose|make|create)\s+(?:me\s+)?(?:a\s+|an\s+|the\s+)?"
        r"(?:prompt|message|email|note|paragraph|description|summary|idea|essay|post|readme|"
        r"pitch|brief|spec|outline|plan)"
        r"(?:\s+(?:about|for|on|regarding)\s+(.+))?"
        r"[.!?]?$", re.IGNORECASE)

    ALT_START_RE = re.compile(
        r"^(?:let me\s+dictate(?:\s+(?:a|an|the))?"
        r"|dictate\s+(?:a\s+)?(?:prompt|note|message|paragraph|essay|idea|post)"
        r"|start\s+(?:a\s+)?(?:prompt|note|dictation)|take\s+(?:a\s+)?note"
        r"|i want to (?:brainstorm|talk through|write about|dictate)"
        r"|take\s+this\s+down)"
        r"(?:\s+(?:a|an|the)\s+\w+)?"
        r"(?:[:\-]\s*|\s+(?:about|for|on)\s+)?(.+?)?[.!?]?$", re.IGNORECASE)

    DONE_RE = re.compile(
        r"^(?:done|that'?s? (?:it|all|good)|finish(?:ed)?|stop dictating|end (?:it|dictation)|"
        r"finish the prompt|wrap (?:it )?up)[.!?]?$", re.IGNORECASE)

    CANCEL_RE = re.compile(
        r"^(?:cancel|never ?mind|forget it|throw it away|throw that away|discard|scrap it)"
        r"[.!?]?$", re.IGNORECASE)

    KIND_MAP = {
        "prompt": "prompt", "message": "message", "email": "email", "note": "note",
        "paragraph": "note", "description": "description", "summary": "summary",
        "idea": "idea", "essay": "essay", "post": "post", "readme": "readme",
        "pitch": "pitch", "brief": "brief", "spec": "spec", "outline": "outline",
        "plan": "plan",
    }

    def __init__(self):
        self._session: Optional[DictationSession] = None
        self._lock = threading.Lock()
        self._kind = "prompt"

    # ------------------------------------------------------------------ #
    @property
    def active(self) -> bool:
        return self._session is not None

    def maybe_start(self, text: str) -> Optional[str]:
        """If ``text`` is a start command, open a session and return the greeting."""
        m = self.START_RE.match(text.strip()) or self.ALT_START_RE.match(text.strip())
        if not m:
            return None
        # Kind: "prompt", "message", "note", ...
        head = re.search(r"\b(prompt|message|email|note|paragraph|description|summary|idea|"
                         r"essay|post|readme|pitch|brief|spec|outline|plan)\b",
                         text, re.IGNORECASE)
        kind = self.KIND_MAP.get((head.group(1) if head else "prompt").lower(), "prompt")
        topic = (m.group(1) or "").strip() if m.lastindex else ""
        with self._lock:
            self._session = DictationSession(topic=topic)
            self._kind = kind
        log.info("dictate.start kind=%s topic=%r", kind, topic)
        opener = "Go ahead — talk it through."
        if topic:
            opener = f"Got it, {kind} about {topic}. Go ahead."
        return opener + " Say 'done' when finished, or 'cancel' to throw it away."

    def feed(self, text: str) -> Optional[str]:
        """Add ``text`` to the active session, or return a completion reply
        when the user says 'done'/'cancel'. Returns None when nothing to say."""
        if not self.active:
            return None
        stripped = text.strip()
        if not stripped:
            return None
        if self.CANCEL_RE.match(stripped):
            with self._lock:
                self._session = None
            return "Okay, thrown out."
        if self.DONE_RE.match(stripped):
            return self.finish()
        with self._lock:
            self._session.lines.append(stripped)
            self._session.last_touched = time.time()
        return None                     # silent acknowledgement while capturing

    def finish(self) -> str:
        with self._lock:
            if self._session is None:
                return "Nothing to save."
            sess = self._session
            self._session = None
            kind = self._kind
        raw = " ".join(sess.lines).strip()
        if not raw:
            return "Nothing to save — you didn't say anything."
        cleaned = self._compress(raw, kind, sess.topic)
        path = self._save(cleaned, kind, sess.topic)
        summary = cleaned.strip().split("\n")[0][:120]
        return (f"Saved a {kind} of about {len(cleaned.split())} words to {path.name}. "
                f"It starts: {summary}")

    # ------------------------------------------------------------------ #
    def _compress(self, raw_text: str, kind: str, topic: str) -> str:
        """Turn rambling speech into a clean written form via the local LLM."""
        try:
            from modules.agent.llm import complete
        except Exception:
            return raw_text
        system = (
            f"Rewrite the following spoken monologue into a clear, well-structured {kind}"
            + (f" about {topic}" if topic else "")
            + ". Keep the speaker's meaning and vocabulary; fix disfluencies, remove filler, "
              "organise the thoughts. Output only the finished text — no preamble, no "
              "commentary, no markdown fences.")
        try:
            out = complete(raw_text, system=system, timeout=60.0)
        except Exception as e:
            log.warning("dictate.compress_failed %s", e)
            out = ""
        return (out or raw_text).strip()

    def _save(self, text: str, kind: str, topic: str) -> Path:
        d = Path(data_path("dictations", ".keep")).parent
        d.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^\w-]+", "-", (topic or kind).lower()).strip("-") or kind
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = d / f"{ts}_{slug}.md"
        path.write_text(text + "\n", encoding="utf-8")
        try:
            import os
            os.startfile(str(path))          # opens with the default editor
        except Exception:
            pass
        return path


dictation = DictationManager()

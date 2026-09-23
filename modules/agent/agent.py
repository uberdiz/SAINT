"""
modules/agent/agent.py

The agent entry point used by the AI module for every user turn:

    user text
      → pending confirmation?        (yes / no)
      → deterministic intent router  (Spotify, memory, reminders, desktop, ...)
      → otherwise: None  → the AI module asks the LLM (with memory context and,
                           when useful, tool calling)

``handle()`` returns an AgentResult when the request was handled here. The
reply text is built from real tool results.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from core.config import config
from core.events import event_bus, EventType
from modules.agent.confirm import confirmations
from modules.agent.router import route, Reply

log = logging.getLogger("saint.agent")


@dataclass
class AgentResult:
    text: str
    intent: str
    ok: bool = True
    expects_reply: bool = False


class Agent:
    def __init__(self):
        self._lock = threading.Lock()

    def handle(self, text: str) -> Optional[AgentResult]:
        if not config.get("agent.enabled", True):
            return None
        t0 = time.perf_counter()

        answer = confirmations.resolve(text)
        if answer is not None:
            log.info("agent.intent confirmation_reply")
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "confirmation", "text": text[:80]})
            return AgentResult(answer, "confirmation")

        intent = route(text)
        if intent is None:
            log.info("agent.intent none → LLM text=%r", text[:80])
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "llm", "text": text[:80]})
            return None

        log.info("agent.intent %s text=%r", intent.name, text[:80])
        event_bus.emit_event(EventType.AGENT_INTENT, {"intent": intent.name, "domain": intent.domain,
                                                      "text": text[:80]})
        with self._lock:
            try:
                reply: Reply = intent.run()
            except Exception:
                log.exception("agent.intent_failed %s", intent.name)
                reply = Reply("Something went wrong while doing that, so I stopped.", ok=False)
        event_bus.emit_event(EventType.LATENCY_INTENT, {
            "ms": round((time.perf_counter() - t0) * 1000, 1), "intent": intent.name})
        return AgentResult(reply.text, intent.name, ok=reply.ok, expects_reply=reply.expects_reply)

    def run_command(self, text: str) -> str:
        """Used by scheduled automations: run a command, return the result text."""
        res = self.handle(text)
        if res is None:
            return f"I didn't recognise the scheduled command '{text}'."
        return res.text


agent = Agent()

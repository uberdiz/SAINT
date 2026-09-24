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

        # Dictation mode: while active, every utterance is *content*, not a
        # command. Say "done" to finish, "cancel" to throw it away.
        from modules.agent.dictate import dictation
        if dictation.active:
            reply = dictation.feed(text)
            log.info("agent.intent dictation reply=%r active=%s", (reply or "")[:60], dictation.active)
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "dictation.capture",
                                                          "active": dictation.active})
            return AgentResult(reply or "Got it.", "dictation.capture")
        greeting = dictation.maybe_start(text)
        if greeting is not None:
            log.info("agent.intent dictation.start")
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "dictation.start"})
            return AgentResult(greeting, "dictation.start", expects_reply=True)

        answer = confirmations.resolve(text)
        if answer is not None:
            log.info("agent.intent confirmation_reply")
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "confirmation", "text": text[:80]})
            return AgentResult(answer, "confirmation")

        from modules.agent.confirm import choices
        with self._lock:
            chosen = choices.resolve(text)
        if chosen is not None:
            log.info("agent.intent choice_reply")
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "choice", "text": text[:80]})
            return AgentResult(chosen, "choice", expects_reply=choices.pending is not None)

        from modules.automation.scenes import scenes
        scene = scenes.match(text)
        if scene is not None:
            # Runs on its own thread: steps re-enter handle(), which holds _lock.
            scenes.run_in_background(scene, self.run_command)
            log.info("agent.intent scene.run name=%r", scene.name)
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "scene.run", "text": text[:80]})
            return AgentResult(f"Running {scene.name}.", "scene.run")

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
        if intent.domain in ("spotify", "browser", "desktop"):
            from modules.agent.context import desktop_context
            if not (intent.domain == "desktop" and desktop_context.domain() == "browser"):
                desktop_context.note_domain(intent.domain)
        log.info("agent.result %s ok=%s reply=%r", intent.name, reply.ok, reply.text[:120])
        event_bus.emit_event(EventType.LATENCY_INTENT, {
            "ms": round((time.perf_counter() - t0) * 1000, 1), "intent": intent.name})
        return AgentResult(reply.text, intent.name, ok=reply.ok, expects_reply=reply.expects_reply)

    # Intents never started from speech SAINT wasn't addressed with: song lyrics
    # like "my name is ..." or "type ..." must not be saved or typed.
    _UNADDRESSED_BLOCKED = ("memory.remember", "memory.forget", "memory.forget_all", "desktop.type_text",
                            "automation.schedule_command")

    def accepts_followup(self, text: str) -> bool:
        """Would ``text`` do something if it were a command? Used by the voice
        module to tell a follow-up command ("click it", "skip that", "yes")
        from chatter or lyrics while music plays. No side effects."""
        text = (text or "").strip()
        if not text:
            return False
        try:
            from modules.agent.dictate import dictation
            from modules.agent.confirm import choices
            if dictation.active or confirmations.pending is not None or choices.pending is not None:
                return True
            from modules.automation.scenes import scenes
            if scenes.match(text) is not None:
                return True
            intent = route(text)
        except Exception:
            log.exception("agent.accepts_followup_failed")
            return False
        return intent is not None and intent.name not in self._UNADDRESSED_BLOCKED

    def run_command(self, text: str) -> str:
        """Used by scheduled automations: run a command, return the result text."""
        res = self.handle(text)
        if res is None:
            return f"I didn't recognise the scheduled command '{text}'."
        return res.text


agent = Agent()

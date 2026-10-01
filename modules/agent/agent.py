"""
modules/agent/agent.py

The agent entry point used by the AI module for every user turn:

    user text
      → pending confirmation?        (yes / no)
      → "no, I meant X"              run X, and learn it for the last request
      → learned skills               requests SAINT was taught (modules/learning)
      → deterministic intent router  (Spotify, memory, reminders, desktop, ...)
      → didn't work / not known      the planner tries harder with commands SAINT
                                     knows; if that works it's learned, if not
                                     SAINT offers to watch the user do it once
      → otherwise: None  → the AI module asks the LLM (with memory context and,
                           when useful, tool calling)

``handle()`` returns an AgentResult when the request was handled here. The
reply text is built from real tool results.
"""

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

from core.config import config
from core.events import event_bus, EventType
from modules.agent.confirm import confirmations
from modules.agent.router import route, Reply

log = logging.getLogger("saint.agent")

# Failures worth trying another way ("I couldn't find a window for all my
# windows") — not ones another attempt can't fix ("Spotify isn't connected").
_TRY_HARDER = re.compile(r"couldn'?t find|can'?t see|isn'?t a key|isn'?t visible|no such|didn'?t recogni|"
                         r"not sure what you mean|don'?t know (?:how|what)|nothing (?:called|found)|"
                         r"couldn'?t tell|isn'?t open|isn'?t running|couldn'?t work out", re.I)


@dataclass
class AgentResult:
    text: str
    intent: str
    ok: bool = True
    expects_reply: bool = False


class Agent:
    def __init__(self):
        self._lock = threading.Lock()
        self._last_failed = ""           # for "watch me" without saying what
        # Scene / scheduled steps run exactly as written: a step that fails is
        # reported, never "worked out" into something else (2026-09-30: a scene
        # step that failed was planned into extracting a zip and a Steam search).
        self._literal = threading.local()

    def handle(self, text: str) -> Optional[AgentResult]:
        if not config.get("agent.enabled", True):
            return None
        t0 = time.perf_counter()
        from core.focus_guard import focus_guard
        literal = getattr(self._literal, "on", False)
        if not literal:
            focus_guard.begin(text)            # scene steps keep the scene's own "yes, show things"

        # While SAINT watches the user show it something, "done" ends the lesson.
        from modules.learning import demonstration
        from modules.learning import intents as learning
        if demonstration.recorder.active and learning.is_done(text):
            log.info("agent.intent learning.done")
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "learning.done", "text": text[:80]})
            text = demonstration.finish()
            # A long lesson is read back and needs a "yes" (demonstration.learn_from).
            return AgentResult(text, "learning.done", expects_reply=confirmations.pending is not None)
        if learning.is_done(text) and demonstration.just_finished():
            # It had already stopped (15 quiet seconds) and saved what it saw.
            return AgentResult("I'd already stopped watching. " + demonstration.just_finished(), "learning.done")

        # "No, that's wrong" / "I didn't ask for that" right after SAINT acted:
        # a recipe SAINT worked out itself is unlearned on the spot, and the
        # mistake is journaled so the planner doesn't repeat it. The utterance
        # still goes on ("no, I meant X" runs X; "stop" stops).
        from modules.learning.feedback import feedback, is_complaint
        unlearned = None if getattr(self._literal, "on", False) else feedback.check_complaint(text)
        if unlearned and is_complaint(text) and len(text.split()) <= 6 and not demonstration.recorder.active:
            from modules.agent.meta import match_meta as _mm
            if _mm(text) is None:
                log.info("agent.intent learning.unlearned")
                event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "learning.unlearned", "text": text[:80]})
                return AgentResult(unlearned + " What did you want instead?", "learning.unlearned")

        # Commands about SAINT itself (stop, "what are you doing?", silent
        # mode) never wait for the lock a running plan holds.
        from modules.agent.meta import match_meta, run_meta
        meta = match_meta(text)
        if meta is not None:
            reply = run_meta(meta)
            log.info("agent.intent meta.%s", meta.kind)
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": f"meta.{meta.kind}", "text": text[:80]})
            return AgentResult(reply, f"meta.{meta.kind}")

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

        from modules.learning.corrections import corrections
        from modules.agent.confirm import choices
        if confirmations.pending is not None or choices.pending is not None:
            # "No, close the finals" / "no, I meant the folder you just made" answers
            # the question *and* says what to do instead.
            fixed = corrections.detect(text)
            if fixed:
                confirmations.clear("corrected")
                choices.clear()
                return self._corrected(text, fixed)
        answer = confirmations.resolve(text)
        if answer is not None:
            log.info("agent.intent confirmation_reply")
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "confirmation", "text": text[:80]})
            return AgentResult(answer, "confirmation")

        with self._lock:
            chosen = choices.resolve(text)
        if chosen is not None:
            log.info("agent.intent choice_reply")
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "choice", "text": text[:80]})
            return AgentResult(chosen, "choice", expects_reply=choices.pending is not None)

        from modules.agent.aliases import aliases, parse_alias_command
        alias_reply = parse_alias_command(text)
        if alias_reply is not None:
            log.info("agent.intent alias")
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "alias", "text": text[:80]})
            return AgentResult(alias_reply, "alias")
        expanded = aliases.expand(text)
        if expanded != text:
            log.info("agent.alias.expand %r -> %r", text[:60], expanded[:60])
            text = expanded

        lk = learning.parse(text, last_failed=self._last_failed)
        if lk is not None:
            log.info("agent.intent learning.%s", lk[0])
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": f"learning.{lk[0]}", "text": text[:80]})
            return AgentResult(learning.run(*lk), f"learning.{lk[0]}")

        fixed = corrections.detect(text)
        if fixed:
            return self._corrected(text, fixed)

        from modules.automation.scenes import scenes
        scene = scenes.match(text)
        if scene is not None:
            # Runs on its own thread: steps re-enter handle(), which holds _lock.
            scenes.run_in_background(scene, self.run_command)
            log.info("agent.intent scene.run name=%r", scene.name)
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "scene.run", "text": text[:80]})
            return AgentResult(f"Running {scene.name}.", "scene.run")

        from modules.learning.skills import skills, run_steps
        skill = skills.match(text)
        if skill is not None:
            log.info("agent.intent skill %r -> %r", skill.phrase, skill.steps)
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "learning.skill", "domain": "learned",
                                                          "text": text[:80], "steps": skill.steps})
            with self._lock:
                try:
                    reply = run_steps(skill.steps)
                except Exception:
                    log.exception("agent.skill_failed %r", skill.phrase)
                    reply = None
            skills.note_result(skill, bool(reply and reply.ok))
            if reply is not None and (reply.ok or not _TRY_HARDER.search(reply.text)):
                return self._finish(text, "learning.skill", reply, t0, steps=skill.steps)
            # The learned way stopped working: work it out again below.

        intent = route(text)
        from core.game_mode import game_mode
        if intent is None and not game_mode.feature("ai_chat"):
            return self._finish(text, "gaming.ai_off", Reply(
                "AI chat is off in Gaming Mode. Commands like music, volume and timers still work.", ok=False), t0)
        if intent is not None and intent.domain == "spotify" and not game_mode.feature("spotify"):
            return self._finish(text, "gaming.spotify_off", Reply(
                "Spotify control is off in Gaming Mode. Turn it on in Settings > Gaming Mode.", ok=False), t0)
        if intent is None:
            from modules.learning import planner
            if planner.worth_planning(text):
                return self._try_harder(text, "", t0)
            log.info("agent.intent none → LLM text=%r", text[:80])
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "llm", "text": text[:80]})
            corrections.note(text, "llm", None)
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
        if intent.domain in ("spotify", "browser", "desktop", "steam"):
            from modules.agent.context import desktop_context
            if not (intent.domain == "desktop" and desktop_context.domain() == "browser"):
                desktop_context.note_domain(intent.domain)
        log.info("agent.result %s ok=%s reply=%r", intent.name, reply.ok, reply.text[:120])
        if not reply.ok and not reply.expects_reply and _TRY_HARDER.search(reply.text) \
                and not intent.name.startswith("composite"):
            return self._try_harder(text, reply.text, t0, first=intent.name)
        return self._finish(text, intent.name, reply, t0)

    # ------------------------------------------------------------------ #
    # Learning
    # ------------------------------------------------------------------ #
    def _finish(self, text: str, name: str, reply, t0: float, steps=None) -> AgentResult:
        from modules.learning.corrections import corrections
        from modules.learning.feedback import feedback
        corrections.note(text, name, reply.ok)
        if not getattr(self._literal, "on", False):
            feedback.note_result(text, name, reply.ok, reply.text, steps=steps, expects_reply=reply.expects_reply)
            if reply.ok:
                from modules.learning.habits import habits
                habits.maybe_scan()                    # notice routines (rate-limited, background)
            taught, at = feedback.just_taught
            if taught and time.time() - at < 2:
                feedback.just_taught = ("", 0.0)
                reply = type(reply)(f"{reply.text.rstrip()} Got it — next time you say “{taught.strip(' .!?')}”, "
                                    f"that's what I'll do.", ok=True)
        self._last_failed = "" if reply.ok else text
        event_bus.emit_event(EventType.LATENCY_INTENT, {
            "ms": round((time.perf_counter() - t0) * 1000, 1), "intent": name})
        return AgentResult(reply.text, name, ok=reply.ok, expects_reply=reply.expects_reply)

    def _try_harder(self, text: str, failure: str, t0: float, first: str = "") -> AgentResult:
        """The request wasn't recognised, or what SAINT tried didn't work: plan
        it with commands SAINT knows. If that fails too, offer to watch."""
        from modules.agent.router import Reply
        from modules.learning import demonstration, planner
        from modules.learning.corrections import corrections
        from modules.learning.feedback import feedback
        if getattr(self._literal, "on", False):
            log.info("agent.literal_step_failed text=%r failure=%r", text[:80], failure[:80])
            return self._finish(text, first or "learning.unknown",
                                Reply(failure or f"I didn't understand the step “{text.strip()}”.", ok=False), t0)
        log.info("agent.try_harder text=%r failure=%r", text[:80], failure[:80])
        event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "learning.plan", "text": text[:80],
                                                      "failure": failure[:120]})
        with self._lock:
            try:
                out = planner.attempt(text, failure, domain=first.split(".")[0] if first else "")
            except Exception:
                log.exception("agent.planner_failed")
                out = None
        if out is not None and (out.reply.ok or out.reply.expects_reply):
            r = out.reply
            if out.learned:
                r = Reply(r.text.rstrip() + " I'll remember how to do that.", ok=True)
            return self._finish(text, "learning.planned", r, t0, steps=out.steps)
        if out is None and not failure and self._llm_can_act(text):
            # No plan from known commands: let the model try its tools directly;
            # if that does nothing either, the AI module offers to watch and learn.
            log.info("agent.try_harder no plan → LLM tools")
            event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "llm", "text": text[:80]})
            corrections.note(text, "llm", None)
            return None
        # Couldn't work it out: offer to learn it from the user. Only a "yes"
        # starts watching, so SAINT never records whatever happens next by itself.
        lead = failure or (out.reply.text if out is not None else "")
        lead = (lead.rstrip(". ") + ". ") if lead else ""
        # Either way the user can just *say* what it should do next: the next
        # command that works is learned for this wording (feedback.expect_teaching).
        if config.get("learning.watch_after_failure", True) and demonstration.offer(text):
            msg = lead + ("I don't know how to do that yet. Tell me what it should do, or say yes and show me, "
                          "then say “done”.")
        else:
            msg = lead + "I don't know how to do that yet. Tell me what it should do and I'll remember it."
        res = self._finish(text, first or "learning.unknown", Reply(msg.strip(), ok=False, expects_reply=True), t0)
        feedback.expect_teaching(text, first or "learning.unknown")
        return res

    @staticmethod
    def _llm_can_act(text: str) -> bool:
        if config.get("ai.provider", "ollama") != "ollama" or not config.get("ai.tool_calling", True):
            return False
        from modules.agent.llm import might_need_tool
        return might_need_tool(text)

    def _corrected(self, text: str, fixed: str) -> Optional[AgentResult]:
        """'No, I meant X': do X, and if it works, learn it for the request it corrects."""
        from modules.learning.corrections import corrections
        from modules.learning.skills import skills
        prev = corrections.last
        corrections.clear()
        log.info("agent.intent correction %r -> %r", prev.text[:60] if prev else "", fixed[:60])
        event_bus.emit_event(EventType.AGENT_INTENT, {"intent": "learning.correction", "text": text[:80],
                                                      "command": fixed[:80]})
        res = self.handle(fixed)
        if res is None:
            return None
        if prev is not None and res.ok and not res.expects_reply and \
                skills.learn(prev.text, [fixed], "corrected") is not None:
            said = prev.text.strip(" .!?")
            res = AgentResult(f"{res.text.rstrip()} Got it — next time you say “{said}”, that's what I'll do.",
                              res.intent, ok=True)
        return res

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
            from modules import lang as _lang
            text = _lang.analyze(text, update_state=False).routed_text
        except Exception:
            log.debug("agent.accepts_followup lang failed", exc_info=True)
        try:
            from modules.agent.dictate import dictation
            from modules.agent.confirm import choices
            if dictation.active or confirmations.can_answer(text) or choices.pending is not None:
                return True
            from modules.agent.meta import match_meta
            if match_meta(text) is not None:
                return True
            from modules.agent.aliases import aliases
            text = aliases.expand(text)
            from modules.automation.scenes import scenes
            if scenes.match(text) is not None:
                return True
            from modules.learning import demonstration, intents as learning
            from modules.learning.corrections import corrections
            from modules.learning.skills import skills
            if skills.match(text) is not None or corrections.detect(text) or learning.parse(text) is not None:
                return True
            if (demonstration.recorder.active or demonstration.just_finished()) and learning.is_done(text):
                return True
            intent = route(text)
        except Exception:
            log.exception("agent.accepts_followup_failed")
            return False
        return intent is not None and intent.name not in self._UNADDRESSED_BLOCKED

    def run_command(self, text: str) -> str:
        """Used by scenes and scheduled automations: run a command exactly as
        written and return the result text. Nothing is improvised or learned."""
        self._literal.on = True
        from core.focus_guard import focus_guard
        focus_guard.begin(text, explicit=True)     # the user asked for the scene / automation by name
        try:
            res = self.handle(text)
        finally:
            self._literal.on = False
        if res is None:
            return f"I didn't recognise the scheduled command '{text}'."
        return res.text


agent = Agent()

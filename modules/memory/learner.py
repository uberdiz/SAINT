"""
modules/memory/learner.py

Learns about the user from ordinary conversation, after each turn and off
the voice pipeline:

1. Plain statements ("I'm a nurse", "I hate horror movies") are picked up by
   modules/memory/passive.py and stored as *learned* memories.
2. With memory.learn_with_ai on, personal-sounding turns are also given to the
   local model, which returns durable facts as JSON (never guesses, never
   moods or requests). They're stored with lower confidence.

Learned memories are reinforced when they come up again and fade if they
never do (MemoryService.consolidate). Nothing here ever answers the user or
changes the conversation — it only writes to the local memory database.
"""

import json
import logging
import queue
import re
import threading
import time
from typing import Dict, List

from core.config import config
from core.events import event_bus, EventType

log = logging.getLogger("saint.memory.learner")

_PERSONAL = re.compile(r"\b(i|i'm|im|i've|i'd|my|me|mine|myself)\b", re.I)
_COMMANDY = re.compile(r"^(?:hey\s+)?(?:saint[,\s]+)?(?:open|close|play|pause|skip|search|go to|turn|set|remind|"
                       r"what(?:'s| is) the (?:time|weather)|type|press|click|scroll|move|show|stop)\b", re.I)

_PROMPT = (
    "You extract long-term facts about the USER from one thing they said to their voice assistant.\n"
    "Return JSON: {\"facts\": [{\"statement\": str, \"key\": str, \"value\": str, \"category\": str, "
    "\"confidence\": number}]}\n"
    "Rules:\n"
    "- Only facts the user clearly states about themselves or people/pets in their life that stay true for "
    "weeks or longer: identity, job, school, home, family, pets, likes, dislikes, hobbies, tools they use, "
    "projects, goals, routines.\n"
    "- Never include moods, one-off plans, requests, questions, things about the assistant, or guesses.\n"
    "- statement: first person, short (\"I work night shifts\"). key: 2-4 word topic (\"work schedule\"). "
    "value: the answer (\"night shifts\").\n"
    "- category: one of identity, person, preference, interest, dislike, project, goal, routine, fact.\n"
    "- confidence 0-1: how clearly they said it.\n"
    "- Most messages contain no such fact: return {\"facts\": []}.\n\n"
    "The user said: "
)


def worth_learning(text: str) -> bool:
    t = (text or "").strip()
    return (len(t.split()) >= 4 and bool(_PERSONAL.search(t)) and not t.endswith("?")
            and not _COMMANDY.match(t))


def parse_facts(raw: str) -> List[Dict]:
    """The model's JSON → clean fact dicts (tolerates code fences / junk)."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for f in (data.get("facts") or [])[:4]:
        if not isinstance(f, dict):
            continue
        st = str(f.get("statement") or "").strip()
        if not st or len(st) > 140 or st.endswith("?"):
            continue
        try:
            conf = float(f.get("confidence", 0.6))
        except (TypeError, ValueError):
            conf = 0.6
        if conf < 0.5:
            continue
        out.append({"content": st[0].upper() + st[1:], "key": str(f.get("key") or "")[:40],
                    "value": str(f.get("value") or "")[:80], "category": str(f.get("category") or "fact"),
                    "confidence": min(0.8, conf)})
    return out


class MemoryLearner:
    def __init__(self):
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=20)
        self._thread = None
        self._started = False
        self._last_consolidate = 0.0

    def start(self):
        if self._started:
            return
        self._started = True
        event_bus.subscribe(self._on_event)
        self._thread = threading.Thread(target=self._work, name="memory-learner", daemon=True)
        self._thread.start()

    def _on_event(self, ev):
        if ev.type != EventType.CONVERSATION_TURN_END:
            return
        if not (config.get("memory.enabled", True) and config.get("modules.memory", True)):
            return
        text = (ev.payload or {}).get("user_text", "")
        if text and not (ev.payload or {}).get("was_action"):
            try:
                self._q.put_nowait(text)
            except queue.Full:
                pass

    # ------------------------------------------------------------------ #
    def _work(self):
        time.sleep(5)
        self._consolidate()
        while True:
            text = self._q.get()
            try:
                self.learn(text)
            except Exception:
                log.exception("memory.learn_failed")
            if time.time() - self._last_consolidate > 6 * 3600:
                self._consolidate()

    def _consolidate(self):
        self._last_consolidate = time.time()
        try:
            from modules.memory.service import memory_service
            memory_service.consolidate()
        except Exception:
            log.exception("memory.consolidate_failed")

    def learn(self, text: str) -> List[Dict]:
        """Store what ``text`` reveals about the user. Returns what was stored."""
        from modules.memory.passive import extract
        from modules.memory.service import LEARNED, memory_service
        stored = []
        facts = extract(text) if config.get("memory.learn_passive", True) else []
        for f in facts:
            memory_service.remember(content=f["content"], key=f["key"], value=f["value"], category=f["category"],
                                    source="conversation", confidence=0.8, how=LEARNED)
            stored.append(f)
        if not facts and config.get("memory.learn_with_ai", True) and worth_learning(text):
            self._wait_until_quiet()
            for f in parse_facts(self._ask_model(text)):
                memory_service.remember(content=f["content"], key=f["key"], value=f["value"],
                                        category=f["category"], source="conversation",
                                        confidence=f["confidence"], how=LEARNED)
                stored.append(f)
        if stored:
            log.info("memory.learned %d fact(s) from %r", len(stored), text[:60])
        return stored

    @staticmethod
    def _wait_until_quiet(limit: float = 20.0):
        """Don't compete with the answer the user is waiting for."""
        from core.assistant_state import assistant_state
        deadline = time.time() + limit
        while time.time() < deadline:
            state = str(assistant_state.snapshot().get("state", ""))
            if state not in ("processing", "executing", "observing"):
                return
            time.sleep(0.5)

    @staticmethod
    def _ask_model(text: str) -> str:
        if config.get("ai.provider", "ollama") != "ollama":
            return ""
        import requests
        from modules.ai.module import AIModule
        base = config.get("ai.base_url", "http://localhost:11434")
        try:
            model, _ = AIModule._resolve_model(None, "ollama", base, config.get("ai.model", ""))
            r = requests.post(base.replace("localhost", "127.0.0.1").rstrip("/") + "/api/chat", timeout=40,
                              json={"model": model, "stream": False, "format": "json", "keep_alive": -1,
                                    "options": {"temperature": 0.1, "num_predict": 220},
                                    "messages": [{"role": "user", "content": _PROMPT + json.dumps(text)}]})
            if r.status_code != 200:
                return ""
            return (r.json().get("message") or {}).get("content", "")
        except Exception as e:
            log.debug("memory.learn_model_failed %s", e)
            return ""


learner = MemoryLearner()

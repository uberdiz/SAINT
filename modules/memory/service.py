"""
modules/memory/service.py

Structured long-term memory for SAINT.

Memory categories (each persisted separately):
  * short-term conversation   modules/ai/context.py (in RAM, current session)
  * long-term user memory     THIS service  -> data/memory/saint_memory.db
                               (facts, preferences, personal details)
  * Spotify memory             modules/spotify/memory.py -> spotify_memory.db
  * automations                modules/automation/scheduler.py -> automations.db

Facts are stored as key/value pairs when possible ("favorite programming
language" = "Python") so that re-stating a fact updates it instead of creating
a duplicate, and recall answers from exactly what was stored. SAINT only
"remembers" what is in this database — there is no LLM-invented memory.
"""

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.events import event_bus, EventType
from modules.memory.database import MemoryType, MemoryEntry, get_memory_db

log = logging.getLogger("saint.memory")

_CATEGORY_TYPES = {
    "fact": MemoryType.LONG_TERM,
    "personal": MemoryType.LONG_TERM,
    "preference": MemoryType.PREFERENCE,
    "project": MemoryType.PROJECT,
}
_SEARCHABLE = [MemoryType.LONG_TERM, MemoryType.PREFERENCE, MemoryType.PROJECT]

_STOP = {"the", "a", "an", "my", "your", "is", "are", "was", "what", "whats", "do", "does", "i", "me",
         "i'd", "i'm", "im", "id", "can", "could", "would", "should", "please", "some", "any",
         "of", "to", "that", "this", "and", "or", "for", "you", "know", "remember", "about", "tell",
         "which", "who", "where", "when", "how", "did", "say", "said", "it", "in", "on", "at", "be"}

# Words that mean the same thing when matching questions to stored keys.
_SYNONYMS = {
    "like": {"favorite", "favourite", "like", "love", "prefer", "likes"},
    "favorite": {"favorite", "favourite", "like", "love", "prefer"},
    "favourite": {"favorite", "favourite", "like", "love", "prefer"},
    "love": {"favorite", "like", "love"},
    "prefer": {"prefer", "favorite", "like"},
    "live": {"live", "home", "city", "location"},
    "birthday": {"birthday", "born", "birth"},
    "born": {"birthday", "born", "birth"},
    "work": {"work", "job", "employer", "company"},
    "job": {"work", "job", "occupation"},
    "name": {"name", "called"},
    "lang": {"language"},
}


_GENERIC = {"favorite", "favourite", "like", "love", "prefer", "likes", "enjoy", "use", "most", "best"}


def normalize_key(key: str) -> str:
    key = key.lower().strip()
    key = re.sub(r"^(my|your|the)\s+", "", key)
    key = key.replace("favourite", "favorite")
    key = re.sub(r"[^\w\s'-]", " ", key)
    return re.sub(r"\s+", " ", key).strip()


def _tokens(text: str) -> List[str]:
    toks = re.findall(r"[a-z0-9']+", (text or "").lower().replace("favourite", "favorite"))
    return [t.rstrip("s") if len(t) > 4 else t for t in toks if t not in _STOP]


def _expand(tokens: List[str]) -> set:
    out = set(tokens)
    for t in tokens:
        out |= _SYNONYMS.get(t, set())
    return out


def second_person(text: str) -> str:
    """Rewrite a stored first-person fact for speaking back to the user."""
    rules = [(r"\bI am\b", "you are"), (r"\bI'm\b", "you're"), (r"\bI was\b", "you were"),
             (r"\bI\b", "you"), (r"\bmy\b", "your"), (r"\bme\b", "you"), (r"\bmine\b", "yours"),
             (r"\bmyself\b", "yourself")]
    for pat, rep in rules:
        text = re.sub(pat, rep, text, flags=re.IGNORECASE)
    return text


@dataclass
class Recall:
    entry: MemoryEntry
    score: float

    @property
    def key(self) -> str:
        return self.entry.metadata.get("key", "")

    @property
    def value(self) -> str:
        return self.entry.metadata.get("value", "")

    def spoken(self) -> str:
        if self.key and self.value:
            return f"your {self.key} is {self.value}"
        return second_person(self.entry.content)


class MemoryService:
    def __init__(self):
        self._lock = threading.RLock()

    @property
    def db(self):
        return get_memory_db()

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #
    def remember(self, content: str = "", key: str = "", value: str = "",
                 category: str = "fact", source: str = "user") -> Dict[str, Any]:
        """Store (or update) a memory. Returns {"id", "updated", "previous", ...}."""
        category = category if category in _CATEGORY_TYPES else "fact"
        mtype = _CATEGORY_TYPES[category]
        key = normalize_key(key) if key else ""
        value = (value or "").strip().rstrip(".")
        content = (content or "").strip()
        if not content:
            if not (key and value):
                raise ValueError("Nothing to remember.")
            content = f"My {key} is {value}"
        with self._lock:
            existing = self.get_by_key(key) if key else self._find_duplicate(content)
            if existing is not None:
                previous = existing.metadata.get("value") or existing.content
                meta = dict(existing.metadata)
                history = list(meta.get("history", []))[-4:]
                if previous and previous != (value or content):
                    history.append(previous)
                meta.update({"key": key or meta.get("key", ""), "value": value or meta.get("value", ""),
                             "category": category, "history": history})
                self.db.update(existing.id, content=content, metadata=meta)
                event_bus.emit_event(EventType.MEMORY_UPDATED, {"id": existing.id, "key": key,
                                                                "category": category})
                log.info("memory.updated id=%s key=%r", existing.id, key)
                return {"id": existing.id, "updated": True, "previous": previous,
                        "key": key, "value": value, "content": content}
            entry_id = self.db.store(mtype, content,
                                     metadata={"key": key, "value": value, "category": category},
                                     source=source, tags=[category] + ([key] if key else []))
        event_bus.emit_event(EventType.MEMORY_STORED, {"id": entry_id, "key": key, "category": category})
        log.info("memory.stored id=%s category=%s key=%r", entry_id, category, key)
        return {"id": entry_id, "updated": False, "key": key, "value": value, "content": content}

    def update(self, entry_id: int, content: Optional[str] = None, value: Optional[str] = None) -> bool:
        entry = self.db.retrieve(int(entry_id))
        if entry is None:
            return False
        meta = dict(entry.metadata)
        if value is not None:
            meta["value"] = value
            if content is None and meta.get("key"):
                content = f"My {meta['key']} is {value}"
        ok = self.db.update(entry.id, content=content, metadata=meta)
        if ok:
            event_bus.emit_event(EventType.MEMORY_UPDATED, {"id": entry.id})
        return ok

    def forget(self, entry_id: int) -> bool:
        ok = self.db.delete(int(entry_id))
        if ok:
            event_bus.emit_event(EventType.MEMORY_DELETED, {"id": int(entry_id)})
            log.info("memory.deleted id=%s", entry_id)
        return ok

    def forget_matching(self, query: str) -> List[Dict[str, Any]]:
        """Delete the single best match for ``query`` (returns what was deleted)."""
        hits = self.recall(query, limit=3)
        if not hits:
            return []
        best = hits[0]
        if len(hits) > 1 and hits[1].score >= best.score * 0.95 and best.score < 1.5:
            # Ambiguous — don't guess which one to delete.
            raise ValueError("ambiguous: " + "; ".join(second_person(h.entry.content) for h in hits[:3]))
        self.forget(best.entry.id)
        return [best.entry.to_dict()]

    def forget_all(self) -> int:
        count = 0
        for t in _SEARCHABLE:
            count += self.db.forget_by_type(t)
        event_bus.emit_event(EventType.MEMORY_DELETED, {"all": True, "count": count})
        return count

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #
    def all(self, category: Optional[str] = None) -> List[MemoryEntry]:
        types = [_CATEGORY_TYPES[category]] if category in _CATEGORY_TYPES else _SEARCHABLE
        entries = self.db.all_of_types(types)
        if category in ("fact", "personal"):
            entries = [e for e in entries if e.metadata.get("category", "fact") == category]
        return entries

    def get_by_key(self, key: str) -> Optional[MemoryEntry]:
        key = normalize_key(key)
        if not key:
            return None
        for e in self.all():
            if normalize_key(e.metadata.get("key", "")) == key:
                return e
        return None

    def _find_duplicate(self, content: str) -> Optional[MemoryEntry]:
        norm = re.sub(r"\W+", " ", content.lower()).strip()
        for e in self.all():
            if re.sub(r"\W+", " ", e.content.lower()).strip() == norm:
                return e
        return None

    def recall(self, query: str, limit: int = 5, min_score: float = 0.5, strict: bool = True) -> List[Recall]:
        q = _tokens(query)
        if not q:
            return []
        qx = _expand(q)
        # "favorite"/"like" only say *that* it's a preference; the topic words
        # ("programming language", "color") must match for a hit.
        topic = [t for t in q if t not in _GENERIC]
        results = []
        for e in self.all():
            key_toks = _tokens(e.metadata.get("key", ""))
            body = _tokens(e.content + " " + e.metadata.get("value", ""))
            if topic:
                covered = sum(1 for t in topic if t in key_toks or t in body or (_SYNONYMS.get(t, set()) & set(key_toks + body)))
                if strict and (covered == 0 or covered / len(topic) < 0.5):
                    continue
                if not strict and not any(t in key_toks for t in topic):
                    continue
            score = 0.0
            for t in qx:
                if t in key_toks:
                    score += 1.0
                elif t in body:
                    score += 0.5
            # Reward covering the whole key ("programming language").
            if key_toks and all(t in qx for t in key_toks if t not in {"favorite"}):
                score += 0.75
            if score >= min_score:
                results.append(Recall(e, score / max(1.0, len(q) ** 0.5)))
        results.sort(key=lambda r: (r.score, r.entry.updated_at), reverse=True)
        if results:
            event_bus.emit_event(EventType.MEMORY_RECALLED, {"query": query[:80], "hits": len(results)})
        return results[:limit]

    def context_for(self, prompt: str, limit: int = 6) -> List[str]:
        """Relevant memories to give the LLM for this prompt."""
        hits = self.recall(prompt, limit=limit, min_score=0.75, strict=False)
        return [h.entry.content for h in hits]


memory_service = MemoryService()


# ---------------------------------------------------------------------- #
# Deterministic extraction of explicit statements
# ---------------------------------------------------------------------- #
_NOT_FACT_VALUES = re.compile(r"^(what|who|where|when|how|why|which)\b", re.I)


def extract_fact(text: str) -> Optional[Dict[str, str]]:
    """Parse an explicit personal statement into {key, value, category, content}.

    Only clear first-person statements are extracted ("my favorite X is Y",
    "call me Sam", "I live in Boston"). Anything ambiguous returns None.
    """
    t = text.strip().rstrip(".!")
    t = re.sub(r"^(please\s+)?(remember|note|keep in mind|don't forget)\s+(that\s+)?", "", t, flags=re.I)
    t = re.sub(r"^(actually|oh and|also|and|by the way|btw)[,\s]+", "", t, flags=re.I).strip()
    if not t or t.endswith("?"):
        return None

    m = re.match(r"^my\s+(favou?rite\s+[\w\s'-]{2,40}?)\s+(?:is|are)\s+(?:now\s+)?(.+)$", t, re.I)
    if m and not _NOT_FACT_VALUES.match(m.group(2)):
        key, value = normalize_key(m.group(1)), m.group(2).strip()
        return {"key": key, "value": value, "category": "preference", "content": f"My {key} is {value}"}

    m = re.match(r"^(?:call me|my name is|i'?m called)\s+([\w' -]{1,40})$", t, re.I)
    if m:
        return {"key": "name", "value": m.group(1).strip().title(), "category": "personal",
                "content": f"My name is {m.group(1).strip().title()}"}

    m = re.match(r"^i\s+(?:live|am living)\s+in\s+(.+)$", t, re.I)
    if m:
        return {"key": "home location", "value": m.group(1).strip(), "category": "personal",
                "content": f"I live in {m.group(1).strip()}"}

    m = re.match(r"^i\s+work\s+(?:at|for)\s+(.+)$", t, re.I)
    if m:
        return {"key": "employer", "value": m.group(1).strip(), "category": "personal",
                "content": f"I work at {m.group(1).strip()}"}

    m = re.match(r"^my\s+birthday\s+is\s+(?:on\s+)?(.+)$", t, re.I)
    if m:
        return {"key": "birthday", "value": m.group(1).strip(), "category": "personal",
                "content": f"My birthday is {m.group(1).strip()}"}

    m = re.match(r"^i\s+(?:really\s+)?(?:prefer)\s+(.+)$", t, re.I)
    if m:
        return {"key": f"preference {m.group(1).strip()[:30]}", "value": m.group(1).strip(),
                "category": "preference", "content": f"I prefer {m.group(1).strip()}"}

    m = re.match(r"^my\s+([\w\s'-]{2,40}?)\s+(?:is|are)\s+(.+)$", t, re.I)
    if m and not _NOT_FACT_VALUES.match(m.group(2)) and len(m.group(2).split()) <= 12:
        key, value = normalize_key(m.group(1)), m.group(2).strip()
        return {"key": key, "value": value, "category": "personal", "content": f"My {key} is {value}",
                "generic": True}
    return None

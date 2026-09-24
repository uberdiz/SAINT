"""
modules/memory/profile.py

A profile of the user, assembled only from what's stored on this PC:

* long-term memories (told + learned) grouped into identity, people, likes,
  dislikes, projects & goals, routines and other facts;
* listening habits from the Spotify memory (top artists and genres);
* usage habits from the local history (when they talk to SAINT, what they
  ask for most, the sites and apps they use).

``summary()`` asks the local model for a short "About you" paragraph written
strictly from that profile; it's cached in data/memory/profile.json and only
regenerated when memories change.
"""

import json
import logging
import re
import time
from collections import Counter
from typing import Dict, List

from core.paths import data_path

log = logging.getLogger("saint.memory.profile")

IDENTITY_KEYS = ("name", "nickname", "pronouns", "age", "birthday", "occupation", "employer", "job", "studies",
                 "school", "major", "home location", "hometown", "city", "location")


def _item(e) -> Dict:
    from modules.memory.service import TOLD
    meta = e.metadata
    return {"id": e.id, "text": e.content, "key": meta.get("key", ""), "value": meta.get("value", ""),
            "category": meta.get("category", "fact"), "how": meta.get("how", TOLD),
            "confidence": round(e.confidence, 2), "mentions": int(meta.get("mentions", 1)),
            "updated": e.updated_at}


def _label(it: Dict) -> str:
    """Short chip text: "Formula 1", "Mia (sister)", "Valorant"."""
    key, value, text = it["key"], it["value"], it["text"]
    if it["category"] == "person" and value:
        who = re.sub(r"'s name$", "", key).replace("pet ", "")
        return f"{value} ({who})" if who and who.lower() != value.lower() else value
    if value and len(value) <= 40:
        return value[0].upper() + value[1:]
    return text


def build() -> Dict:
    from modules.memory.service import memory_service
    items = [_item(e) for e in memory_service.all()]
    items.sort(key=lambda i: (-i["confidence"], -i["mentions"], -i["updated"]))
    by_key = {i["key"]: i for i in items if i["key"]}

    identity = {}
    for k in IDENTITY_KEYS:
        if k in by_key:
            identity[k] = by_key[k]["value"] or by_key[k]["text"]

    def group(*cats):
        return [dict(i, label=_label(i)) for i in items
                if i["category"] in cats and i["key"] not in IDENTITY_KEYS]
    likes = group("interest") + [dict(i, label=f"{i['key'].replace('favorite ', '').capitalize()}: {i['value']}")
                                 for i in items if i["category"] == "preference" and i["value"]]
    profile = {
        "name": identity.get("name") or identity.get("nickname") or "",
        "identity": identity,
        "people": group("person"),
        "likes": likes,
        "dislikes": group("dislike"),
        "projects": group("project", "goal"),
        "routines": group("routine"),
        "facts": [dict(i, label=i["text"]) for i in items if i["category"] in ("fact", "personal", "identity")
                  and i["key"] not in IDENTITY_KEYS],
        "counts": {"total": len(items), "told": sum(1 for i in items if i["how"] != "learned"),
                   "learned": sum(1 for i in items if i["how"] == "learned")},
        "music": _music(),
        "habits": habits(),
    }
    return profile


def _music() -> Dict:
    try:
        from core.module_manager import module_manager
        sp = module_manager.get("spotify")
        if sp is None:
            return {}
        s = sp.tools.memory.snapshot()
        return {"artists": [a["artist"] for a in s.get("top_artists", [])[:6]],
                "genres": [g["genre"] for g in s.get("top_genres", [])[:5]]}
    except Exception as e:
        log.debug("profile.music_unavailable %s", e)
        return {}


_DOMAINS = (("music", r"^(?:spotify|music)\."), ("browser & YouTube", r"^(?:browser|desktop\.(?:open_url|web_search|new_tab))"),
            ("apps & windows", r"^desktop\."), ("reminders", r"^automation\."), ("memory", r"^memory\."),
            ("your screen", r"^screen\."))


_BRANDS = {"youtube": "YouTube", "github": "GitHub", "chatgpt": "ChatGPT", "tiktok": "TikTok"}


def habits(entries: List[Dict] = None) -> Dict:
    """Patterns in the local history: when, what, and where."""
    if entries is None:
        try:
            from core.history import history
            entries = history.read(3000)
        except Exception:
            entries = []
    entries = [e for e in entries if e.get("user")]
    if not entries:
        return {}
    hours = Counter(time.localtime(e["ts"]).tm_hour for e in entries if e.get("ts"))
    days = Counter(time.strftime("%A", time.localtime(e["ts"])) for e in entries if e.get("ts"))
    doms = Counter()
    for e in entries:
        for t in e.get("tools") or []:
            name = t.get("tool", "")
            dom = next((d for d, rx in _DOMAINS if re.match(rx, name)), None)
            if dom:
                doms[dom] += 1
                break
        else:
            doms["conversation"] += 1
    sites, apps = Counter(), Counter()
    for e in entries:
        u = (e.get("user") or "").lower()
        for m in re.finditer(r"\b(youtube|google|reddit|github|twitch|netflix|amazon|wikipedia|chatgpt|monkeytype|"
                             r"gmail|roblox|tiktok|instagram|twitter)\b", u):
            sites[_BRANDS.get(m.group(1), m.group(1).capitalize())] += 1
        m = re.match(r"^(?:hey saint,? )?(?:open|launch|start|switch to)\s+(?:up\s+)?(?:my\s+|the\s+)?([a-z][\w .]{1,24}?)$", u)
        if m and m.group(1) not in ("browser", "a new tab", "new tab", "it", "that"):
            apps[m.group(1).title()] += 1
    total = len(entries)
    peak = [h for h, _ in hours.most_common(3)]
    return {
        "requests": total,
        "voice_share": round(sum(1 for e in entries if e.get("source") in ("voice", "hotword")) / total, 2),
        "peak_hours": sorted(peak),
        "time_of_day": _time_of_day(hours),
        "busiest_day": days.most_common(1)[0][0] if days else "",
        "top_uses": [(d, round(n / total, 2)) for d, n in doms.most_common(4)],
        "top_sites": [s for s, _ in sites.most_common(5)],
        "top_apps": [a for a, _ in apps.most_common(5)],
        "since": min(e["ts"] for e in entries if e.get("ts")),
    }


def _time_of_day(hours: Counter) -> str:
    if not hours:
        return ""
    buckets = Counter()
    for h, n in hours.items():
        buckets["mornings" if 5 <= h < 12 else "afternoons" if 12 <= h < 17 else
                "evenings" if 17 <= h < 22 else "late nights"] += n
    return buckets.most_common(1)[0][0]


# ---------------------------------------------------------------------- #
# "About you" summary
# ---------------------------------------------------------------------- #
def _cache_path():
    return data_path("memory", "profile.json")


def _fingerprint(p: Dict) -> str:
    parts = [json.dumps(p["identity"], sort_keys=True)]
    for k in ("people", "likes", "dislikes", "projects", "routines", "facts"):
        parts += sorted(i["text"] for i in p[k])
    parts += p.get("music", {}).get("artists", [])[:3]
    import hashlib
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def cached_summary() -> Dict:
    try:
        with open(_cache_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def facts_text(p: Dict) -> str:
    lines = [f"{k}: {v}" for k, v in p["identity"].items()]
    for k, title in (("people", "People"), ("likes", "Likes"), ("dislikes", "Dislikes"),
                     ("projects", "Projects and goals"), ("routines", "Routines"), ("facts", "Other")):
        if p[k]:
            lines.append(f"{title}: " + "; ".join(i["text"] for i in p[k][:10]))
    m = p.get("music") or {}
    if m.get("artists"):
        lines.append("Listens to: " + ", ".join(m["artists"][:5]))
    h = p.get("habits") or {}
    if h.get("time_of_day"):
        lines.append(f"Uses the assistant mostly in the {h['time_of_day']}")
    return "\n".join(lines)


def summary(profile: Dict = None, force: bool = False) -> str:
    """A 2-3 sentence portrait written only from the stored profile."""
    p = profile or build()
    fp = _fingerprint(p)
    cache = cached_summary()
    if not force and cache.get("fingerprint") == fp and cache.get("text"):
        return cache["text"]
    text = ""
    facts = facts_text(p)
    from core.config import config
    if p["counts"]["total"] >= 2 and facts and config.get("ai.provider", "ollama") == "ollama":
        from modules.agent.llm import complete
        text = complete("Write a warm, 2-3 sentence profile of this person in the second person (\"You're ...\"), "
                        "using ONLY these facts. Don't add anything that isn't listed.\n\n" + facts,
                        system="You write short, accurate user profiles. Plain text only.", timeout=60)
    if not text:
        text = fallback_summary(p)
    try:
        with open(_cache_path(), "w", encoding="utf-8") as f:
            json.dump({"fingerprint": fp, "text": text, "at": time.time()}, f)
    except OSError as e:
        log.debug("profile.cache_write_failed %s", e)
    return text


def fallback_summary(p: Dict) -> str:
    """No model available: a plain sentence built from the facts."""
    idt = p["identity"]
    bits = []
    if idt.get("occupation"):
        bits.append(f"a {idt['occupation']}" if not re.match(r"^(a|an|the)\b", idt["occupation"]) else idt["occupation"])
    where = idt.get("home location") or idt.get("city") or idt.get("hometown")
    first = "You're " + " ".join(bits) if bits else ""
    if where:
        first = (first + f" in {where}") if first else f"You live in {where}"
    likes = [i["label"] for i in p["likes"] if i["category"] == "interest"][:3]
    parts = [first] if first else []
    if likes:
        parts.append("you're into " + ", ".join(likes))
    if not parts:
        return "Tell SAINT about yourself — or just chat — and your profile fills in here."
    s = "; ".join(parts)
    return s[0].upper() + s[1:] + "."

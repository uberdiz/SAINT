"""Tiny web-search helper for current-information questions.

Providers:
    duckduckgo   Instant Answer API (no key, best for definitions/summaries)
    tavily       requires web.api_key
    serpapi      requires web.api_key

Returns a short natural answer or raises; the router turns raises into a
"couldn't reach the web" reply. If a provider yields nothing useful, returns
"" so the router says so honestly.
"""

from __future__ import annotations

import json
import logging
from typing import List

import requests

from core.config import config

log = logging.getLogger("saint.web")

_UA = "SAINT/1.0 (voice assistant)"


def answer_current(query: str) -> str:
    provider = (config.get("web.provider", "none") or "none").lower()
    if provider == "none":
        return ""
    hits = _search(provider, query)
    return _summarise(query, hits)


def _search(provider: str, query: str) -> List[dict]:
    if provider == "duckduckgo":
        return _duckduckgo(query)
    if provider == "tavily":
        return _tavily(query)
    if provider == "serpapi":
        return _serpapi(query)
    return []


def _duckduckgo(query: str) -> List[dict]:
    try:
        r = requests.get("https://api.duckduckgo.com/",
                         params={"q": query, "format": "json", "no_html": "1", "no_redirect": "1"},
                         headers={"User-Agent": _UA}, timeout=8)
        data = r.json() if r.status_code == 200 else {}
    except (requests.RequestException, json.JSONDecodeError) as e:
        log.warning("ddg.failed %s", e)
        return []
    hits: List[dict] = []
    if data.get("AbstractText"):
        hits.append({"title": data.get("Heading", ""), "text": data["AbstractText"],
                     "url": data.get("AbstractURL", "")})
    for t in (data.get("RelatedTopics") or [])[:config.get("web.max_results", 4)]:
        if isinstance(t, dict) and t.get("Text"):
            hits.append({"title": t.get("Text", "")[:80], "text": t["Text"], "url": t.get("FirstURL", "")})
    return hits


def _tavily(query: str) -> List[dict]:
    key = config.get("web.api_key", "") or ""
    if not key:
        return []
    try:
        r = requests.post("https://api.tavily.com/search", timeout=10,
                          json={"api_key": key, "query": query, "search_depth": "basic",
                                "max_results": int(config.get("web.max_results", 4))})
        data = r.json() if r.status_code == 200 else {}
    except (requests.RequestException, json.JSONDecodeError) as e:
        log.warning("tavily.failed %s", e)
        return []
    hits = []
    if data.get("answer"):
        hits.append({"title": "answer", "text": data["answer"], "url": ""})
    for res in (data.get("results") or []):
        hits.append({"title": res.get("title", ""), "text": res.get("content", ""),
                     "url": res.get("url", "")})
    return hits


def _serpapi(query: str) -> List[dict]:
    key = config.get("web.api_key", "") or ""
    if not key:
        return []
    try:
        r = requests.get("https://serpapi.com/search.json", timeout=10,
                        params={"engine": "google", "q": query, "api_key": key})
        data = r.json() if r.status_code == 200 else {}
    except (requests.RequestException, json.JSONDecodeError) as e:
        log.warning("serpapi.failed %s", e)
        return []
    hits = []
    ab = data.get("answer_box") or {}
    if ab.get("answer") or ab.get("snippet"):
        hits.append({"title": ab.get("title", ""), "text": ab.get("answer") or ab["snippet"],
                     "url": ab.get("link", "")})
    for res in (data.get("organic_results") or [])[:int(config.get("web.max_results", 4))]:
        hits.append({"title": res.get("title", ""), "text": res.get("snippet", ""),
                     "url": res.get("link", "")})
    return hits


def _summarise(query: str, hits: List[dict]) -> str:
    if not hits:
        return ""
    if (config.get("web.answer_style", "concise") or "concise").lower() == "detailed":
        parts = []
        for h in hits[:3]:
            snippet = (h.get("text") or "").strip()
            if snippet:
                parts.append(snippet[:280])
        return " ".join(parts)
    # concise: hand the top snippet to the LLM to compress into ONE sentence.
    top = "\n".join(f"- {h.get('text','').strip()[:280]}" for h in hits[:3] if h.get("text"))
    if not top:
        return ""
    try:
        from modules.agent.llm import complete
        answer = complete(f"Web results for '{query}':\n{top}\n\nAnswer the question in ONE short sentence.",
                          system="You are a concise voice assistant. Reply with one short sentence, no filler.",
                          timeout=25.0)
        return (answer or hits[0].get("text", "")).strip()
    except Exception as e:
        log.warning("web.summarise_failed %s", e)
        return hits[0].get("text", "").strip()

"""New web-info routing and FLUX vision availability checks."""

from core.config import config
from modules.agent.router import route
from modules.vision.flux import FluxAnalyzer


def test_current_info_questions_route_to_web():
    for q in ("What's the latest news about NVIDIA?",
              "What's happening in the world today?",
              "What's the weather tomorrow?",
              "Who won the game?"):
        it = route(q)
        assert it is not None and it.name == "web.current", (q, it and it.name)


def test_explicit_browser_search_still_uses_browser():
    it = route("Search Google for NVIDIA news")
    assert it is not None and it.name == "browser.search"


def test_open_youtube_is_browser_not_web_info():
    it = route("Open YouTube")
    assert it is not None and it.name == "browser.open_url"


def test_web_current_reports_missing_provider():
    old = config.get("web.provider", "none")
    config.set("web.provider", "none", persist=False)
    try:
        reply = route("What's the latest news about NVIDIA?").run()
        assert not reply.ok and "web" in reply.text.lower()
    finally:
        config.set("web.provider", old, persist=False)


def test_flux_reports_missing_weights_honestly():
    fa = FluxAnalyzer()
    ok, reason = fa.available()
    # In a fresh checkout weights aren't present — SAINT must say so, not fake success.
    if not ok:
        assert "FLUX" in reason or "flux" in reason.lower()

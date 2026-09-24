"""Domain scoring + the previously-broken _site_url() bug."""

from modules.agent.capabilities import top_domain, score_domains, capability_summary
from modules.agent.router import _site_url


def test_site_url_no_longer_nameerror():
    # Was raising `NameError: name '_SITES' is not defined` in production.
    assert _site_url("youtube") == "https://www.youtube.com"
    assert _site_url("google") == "https://www.google.com"
    assert _site_url("example.com") == "https://example.com"
    assert _site_url("nonexistent-word-xyz") is None


def test_domain_scoring_hits_expected_domains():
    cases = {
        "play youtube": "browser",
        "skip this song": "spotify",
        "move chrome to my second monitor": "desktop",
        "what is on my second screen": "vision",
        "what am i looking at": "vision",
        "remind me tomorrow at noon": "automation",
        "what's the latest news": "web",
        "remember that i call aide my editor": "memory",
    }
    for text, expected in cases.items():
        assert top_domain(text) == expected, (text, top_domain(text), score_domains(text))


def test_capability_summary_is_nonempty_and_readable():
    s = capability_summary()
    assert s.startswith("You can help with:")
    # Never leak internal tool names.
    for bad in ("__", "{", "spotify__", "desktop__"):
        assert bad not in s

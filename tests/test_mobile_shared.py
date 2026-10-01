"""The iPhone app (mobile/ios) carries copies of the language packs and the shared test data.

They are copied by mobile/ios/scripts/sync_shared.py; this test fails if someone changes one side and forgets the other,
so the Python and Swift implementations keep being checked against the same thing.
"""
import filecmp
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IOS = os.path.join(ROOT, "mobile", "ios")


def _pairs():
    lex = os.path.join(ROOT, "modules", "lang", "lexicon")
    for name in sorted(os.listdir(lex)):
        if name.endswith(".json"):
            yield os.path.join(lex, name), os.path.join(IOS, "Sources", "SaintCore", "Resources", "Lexicon", name)
    for name in ("link_vectors.json", "lang_cases.json", "time_cases.json"):
        yield os.path.join(ROOT, "tests", "data", name), os.path.join(IOS, "Tests", "SaintCoreTests", "Resources", name)


def test_the_iphone_app_has_the_same_language_packs_and_test_data():
    stale = []
    for src, copy in _pairs():
        if not os.path.exists(copy) or not filecmp.cmp(src, copy, shallow=False):
            stale.append(os.path.relpath(copy, ROOT))
    assert stale == [], "run `python mobile/ios/scripts/sync_shared.py`; out of date: %s" % stale

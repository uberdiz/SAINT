#!/usr/bin/env python3
"""Copy what the desktop app and the iPhone app share into the iOS package.

The language packs (modules/lang/lexicon/*.json) are the single source of truth for how SAINT understands
Spanish, French, Portuguese, German and Italian; the test data (tests/data/*.json) is what both the Python and
the Swift tests check, so the two implementations can't drift apart without a test failing.

    python ios/scripts/sync_shared.py [path/to/SAINT/desktop/repo]

With no argument it uses the SAINT repo this folder lives in (mobile/ios/scripts -> repo root).
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
IOS = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(IOS))        # the SAINT repo: <root>/mobile/ios


def find_desktop(arg):
    candidates = [arg] if arg else [ROOT]
    for c in candidates:
        if c and os.path.isdir(os.path.join(c, "modules", "lang", "lexicon")):
            return c
    sys.exit("Couldn't find the SAINT desktop repo (looked in %s). Pass its path as an argument." % ", ".join(candidates))


def copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    print("  ", os.path.relpath(dst, ROOT))


def main():
    desktop = find_desktop(sys.argv[1] if len(sys.argv) > 1 else None)
    print("from", desktop)
    lex = os.path.join(desktop, "modules", "lang", "lexicon")
    for name in sorted(os.listdir(lex)):
        if name.endswith(".json"):
            copy(os.path.join(lex, name), os.path.join(IOS, "Sources", "SaintCore", "Resources", "Lexicon", name))
    data = os.path.join(desktop, "tests", "data")
    for name in ("link_vectors.json", "lang_cases.json", "time_cases.json"):
        copy(os.path.join(data, name), os.path.join(IOS, "Tests", "SaintCoreTests", "Resources", name))


if __name__ == "__main__":
    main()

"""
tools/profiles.py — test SAINT without your real data (core/profiles.py).

    python tools/profiles.py list
    python tools/profiles.py new <name> [--from empty|demo|real|<profile>] [--overwrite]
    python tools/profiles.py snapshot [<name>]     copy your real data into a profile (default snapshot-YYYYMMDD)
    python tools/profiles.py delete <name>

Then start SAINT on it:  run.bat --profile <name>   (--profile clean = a fresh install, demo = sample data)
Your real data (%LOCALAPPDATA%/SAINT) is never changed by any of this. A snapshot doesn't copy the
Spotify login or the SAINT Link key (they're in Windows Credential Manager) — sign in again inside it.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import profiles  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SAINT test profiles")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    new = sub.add_parser("new")
    new.add_argument("name")
    new.add_argument("--from", dest="source", default="empty")
    new.add_argument("--overwrite", action="store_true")
    snap = sub.add_parser("snapshot")
    snap.add_argument("name", nargs="?", default="")
    rm = sub.add_parser("delete")
    rm.add_argument("name")
    a = ap.parse_args(argv)

    if a.cmd == "list":
        rows = profiles.list_profiles()
        if not rows:
            print("No profiles yet. Try:  run.bat --profile demo")
        for r in rows:
            made = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created"])) if r["created"] else "?"
            print(f"{r['name']:<24} from {r['source']:<10} {r['size_mb']:>8} MB   {made}   {r['path']}")
    elif a.cmd == "new":
        print(profiles.create(a.name, a.source, overwrite=a.overwrite))
    elif a.cmd == "snapshot":
        name = a.name or time.strftime("snapshot-%Y%m%d-%H%M")
        print(profiles.create(name, "real"))
        print(f"Start it with:  run.bat --profile {name}")
    elif a.cmd == "delete":
        profiles.delete(a.name)
        print(f"Deleted {a.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

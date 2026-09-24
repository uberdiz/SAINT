"""
Test isolation: point SAINT's runtime data (config, memory DBs, automations,
logs) at a throwaway directory BEFORE any SAINT module is imported, and use
mock backends so tests never touch the real microphone, GPU models, Spotify
account or desktop.
"""

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="saint-test-")
os.environ["SAINT_DATA_DIR"] = _TMP

with open(os.path.join(_TMP, "config.json"), "w", encoding="utf-8") as f:
    json.dump({
        "config_version": 2,
        "ai": {"provider": "mock", "model": "mock-model"},
        "modules": {"ai": True, "voice": False, "automation": True, "vision": False,
                    "memory": True, "spotify": False, "desktop": False},
        "voice": {"stt_backend": "mock", "tts_backend": "mock", "auto_start": False,
                  "wake_word_enabled": False, "wake_word_chime": False},
        "desktop": {"enabled": False},
        "widgets": {"any_media": False},          # never read this PC's real media sessions
        "automation": {"speak_reminders": False},
        "spotify": {"client_id": ""},
        "logging": {"level": "Errors Only"},
    }, f)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)

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
                    "memory": True, "spotify": False, "desktop": False, "watch": False,
                    "notifications": False},
        "voice": {"stt_backend": "mock", "tts_backend": "mock", "auto_start": False,
                  "wake_word_enabled": False, "wake_word_chime": False},
        "desktop": {"enabled": False, "focus_guard": False},   # never depend on the real foreground window
        "widgets": {"any_media": False},          # never read this PC's real media sessions
        "automation": {"speak_reminders": False},
        # Never call the real local model or watch the real mouse/keyboard.
        "learning": {"planner": False, "watch_and_learn": False, "watch_after_failure": False,
                     "from_mistakes": False},
        "audio": {"voicemeeter": {"enabled": "off"}},
        "game_mode": {"enabled": False},          # never react to games running on this PC
        "spotify": {"client_id": ""},
        "logging": {"level": "Errors Only"},
    }, f)


class _MemoryKeyring:
    """Tests must never read or delete the real Spotify login in Windows'
    Credential Manager (a smoke test once refreshed it with an empty client
    ID, got a 400, and wiped it)."""
    priority = 1

    def __init__(self):
        self._data = {}

    def get_password(self, service, user):
        return self._data.get((service, user))

    def set_password(self, service, user, password):
        self._data[(service, user)] = password

    def delete_password(self, service, user):
        self._data.pop((service, user), None)

    def get_credential(self, service, user):
        return None


try:
    import keyring
    import keyring.backend

    class _Backend(_MemoryKeyring, keyring.backend.KeyringBackend):
        pass
    keyring.set_keyring(_Backend())
except Exception:
    import types
    sys.modules["keyring"] = types.SimpleNamespace(**{k: getattr(_MemoryKeyring(), k) for k in
                                                      ("get_password", "set_password", "delete_password")})


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)

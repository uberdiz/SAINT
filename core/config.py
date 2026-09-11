"""
core/config.py

Handles loading, saving, and providing default application configuration.
Backed by a simple JSON file (data/config.json). Extended for Milestone 1
with a full voice configuration section.
"""

import json
import os
import threading

DEFAULT_CONFIG = {
    "theme": "Dark",
    "ai": {
        "provider": "ollama",           # "mock" | "openai" | "ollama"
        "model": "llama3",
        "base_url": "http://localhost:11434",
        "api_key": "",
        "temperature": 0.7,
        "timeout_seconds": 30,
    },
    "logging": {
        "level": "Verbose",             # "Verbose" | "Normal" | "Errors Only"
    },
    "analytics": {
        "enabled": True,
    },
    "auto_save": True,
    "modules": {
        "ai": True,
        "voice": False,
        "automation": False,
        "vision": False,
        "memory": False,
    },

    # ------------------------------------------------------------------
    # Voice (Milestone 1)
    # ------------------------------------------------------------------
    "voice": {
        # Input mode
        "mode": "always_on",            # "always_on" | "push_to_talk"
        "mic_device": 0,                # sounddevice device index
        "mic_sensitivity": 0.015,       # RMS threshold for VAD (0.0–1.0)
        "silence_duration_ms": 700,     # ms of silence before utterance ends
        "noise_suppression": True,      # subtract rolling noise floor

        # STT
        "stt_backend": "faster_whisper",  # "faster_whisper" | "mock"
        "stt_model": "base.en",           # tiny.en / base.en / small.en / medium.en
        "stt_device": "cuda",             # "cuda" | "cpu"
        "stt_compute_type": "float16",    # "float16" | "int8" | "float32"
        "stt_language": "en",

        # TTS
        "tts_backend": "kokoro",          # "kokoro" | "mock"
        "tts_voice": "af_heart",          # kokoro voice name
        "tts_device": "cuda",             # "cuda" | "cpu"
        "tts_speed": 1.0,

        # Wake word (optional, disabled by default)
        "wake_word_enabled": False,
        "wake_word": "saint",
        "wake_word_sensitivity": 0.5,

        # Conversation
        "max_context_turns": 6,           # how many Q&A pairs to keep in context
        "system_prompt": (
            "You are SAINT, a helpful AI assistant. "
            "Be concise. Respond naturally. "
            "If the user interrupts you, adapt immediately without apologising."
        ),
    },
}


class Config:
    """Thread-safe JSON-backed configuration store."""

    def __init__(self, path="data/config.json"):
        self._path = path
        self._lock = threading.Lock()
        self._data = {}
        self._load_or_create()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load_or_create(self):
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self._data = self._merge_defaults(loaded)
            except (json.JSONDecodeError, OSError):
                self._data = json.loads(json.dumps(DEFAULT_CONFIG))
        else:
            self._data = json.loads(json.dumps(DEFAULT_CONFIG))
            self.save()

    def _merge_defaults(self, loaded):
        """Ensure any newly-added default keys exist in an old config file."""
        merged = json.loads(json.dumps(DEFAULT_CONFIG))

        def deep_merge(base, override):
            for k, v in override.items():
                if isinstance(v, dict) and isinstance(base.get(k), dict):
                    deep_merge(base[k], v)
                else:
                    base[k] = v
            return base

        return deep_merge(merged, loaded)

    def save(self):
        with self._lock:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)

    def reload(self):
        self._load_or_create()

    # ------------------------------------------------------------------ #
    # Access
    # ------------------------------------------------------------------ #
    def get(self, dotted_key, default=None):
        """e.g. config.get('voice.stt_model')"""
        parts = dotted_key.split(".")
        node = self._data
        for p in parts:
            if isinstance(node, dict) and p in node:
                node = node[p]
            else:
                return default
        return node

    def set(self, dotted_key, value, persist=True):
        parts = dotted_key.split(".")
        node = self._data
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
        if persist and self.get("auto_save", True):
            self.save()

    def as_dict(self):
        return json.loads(json.dumps(self._data))


# Singleton instance used across the app
config = Config()

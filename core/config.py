"""
core/config.py

Handles loading, saving, and providing default application configuration.
Backed by a simple JSON file (data/config.json). Extended for Milestone 1
with a full voice configuration section.
"""

import json
import os
import threading

from core.paths import data_path

CONFIG_VERSION = 2

DEFAULT_CONFIG = {
    "config_version": CONFIG_VERSION,
    "theme": "Dark",                    # legacy key; mirrored from appearance.theme
    "ai": {
        "provider": "ollama",           # "mock" | "openai" | "ollama"
        "model": "llama3.1",
        "base_url": "http://localhost:11434",
        "api_key": "",
        "temperature": 0.7,
        "timeout_seconds": 30,
        # Let the LLM call SAINT's explicit, validated tools (Ollama models
        # with the "tools" capability, e.g. llama3.1 / llama3.2). Deterministic
        # intent routing always runs first, so common commands never wait on
        # the LLM.
        "tool_calling": True,
        "max_tool_steps": 4,
    },
    "logging": {
        "level": "Normal",              # "Verbose" | "Normal" | "Errors Only"
        "debug": False,                 # extra per-subsystem debug detail
    },
    "analytics": {
        "enabled": True,
    },
    "auto_save": True,
    "modules": {
        "ai": True,
        "voice": True,
        "automation": True,
        "vision": True,
        "memory": True,
        "spotify": False,
        "desktop": True,
    },

    # ------------------------------------------------------------------
    # Voice
    # ------------------------------------------------------------------
    "voice": {
        # Input mode
        "mode": "always_on",            # "always_on" | "push_to_talk"
        "auto_start": True,             # start the microphone when SAINT launches
        "mic_device": None,             # sounddevice device index (None = system default)
        "mic_sensitivity": 0.015,       # RMS threshold for VAD (0.0-1.0)
        "silence_duration_ms": 700,     # ms of silence before utterance ends
        "agent_response_delay_ms": 0,   # artificial delay before agent responds
        "noise_suppression": True,      # subtract rolling noise floor

        # VAD hysteresis thresholds (for start/end detection)
        "vad_start_threshold": 0.015,   # RMS threshold to START speech detection
        "vad_end_threshold": 0.0075,    # RMS threshold to END speech detection (lower = hysteresis)

        # Minimum speech validation (prevents false positives)
        "min_speech_duration_ms": 300,  # minimum speech duration before STT
        "min_speech_rms": 0.005,        # minimum RMS energy for valid speech

        # Activation gating on the STT transcript (applies to speech that
        # was not preceded by the wake word).
        "min_stt_confidence": 0.5,      # minimum Whisper confidence to act on
        "short_utterance_confidence": 0.7,  # stricter bar for 1-word utterances

        # Barge-in (interrupting SAINT while it speaks). The microphone is
        # never muted during TTS; instead SAINT compares mic energy with the
        # energy of the audio it is playing, so its own voice through the
        # speakers is not mistaken for the user.
        "barge_in_enabled": True,
        "barge_in_min_ms": 240,         # sustained speech needed to interrupt
        "barge_in_echo_margin": 2.0,    # how far above predicted echo the mic must be

        # STT
        "stt_backend": "faster_whisper",  # "faster_whisper" | "mock"
        "stt_model": "base.en",           # tiny.en / base.en / small.en / medium.en
        "stt_device": "cuda",             # "cuda" | "cpu"
        "stt_compute_type": "float16",    # "float16" | "int8" | "float32"
        "stt_language": "en",
        "stt_hotwords": "SAINT",          # biases Whisper toward spelling the wake word correctly

        # TTS
        "tts_backend": "kokoro",          # "kokoro" | "qwen" | "mock"
        "tts_model_dir": "data/tts",
        "tts_voice": "af_heart",          # kokoro voice name
        "tts_device": "cuda",             # "cuda" | "cpu" | "auto"
        "tts_allow_cpu_fallback": True,
        "tts_require_cuda": False,
        "tts_speed": 1.0,

        # Qwen TTS specific
        "tts_qwen_model": "Qwen/Qwen3-TTS",
        "tts_qwen_type": "custom_voice",
        "tts_qwen_speaker": "eric",
        "tts_qwen_language": "Auto",
        "tts_qwen_dtype": "bfloat16",
        "tts_qwen_voice_clone_audio": "",
        "tts_qwen_voice_clone_text": "",
        "tts_qwen_x_vector_only": False,
        "tts_qwen_instruct": "",
        "tts_qwen_flash_attention": "Auto",

        # Wake word ("SAINT" / "Hey SAINT"). Runs locally on the CPU through
        # onnxruntime. When enabled SAINT only transcribes speech after it
        # hears its name:
        #   WAKE_LISTENING -> WAKE_DETECTED -> COMMAND_LISTENING -> PROCESSING
        #   -> SPEAKING -> WAKE_LISTENING
        "wake_word_enabled": True,
        "wake_word": "saint",
        "wake_word_model_path": "data/wake/hey_saint.onnx",
        # Shared openWakeWord feature extractors (melspectrogram.onnx and
        # embedding_model.onnx), shipped in data/wake/.
        "wake_word_feature_dir": "data/wake",
        "wake_word_threshold": 0.5,         # detection score threshold (0-1); lower = more sensitive
        "wake_word_trigger_frames": 2,      # consecutive 80 ms frames above threshold (2 rejects one-frame spikes like "saved")
        "wake_word_refractory_sec": 2.0,    # cooldown between detections
        "wake_word_command_timeout_sec": 6.0,  # give up if no command starts in this time
        "wake_word_followup_sec": 0.0,      # listen for a follow-up without the wake word (0 = off)
        "wake_word_chime": True,            # short tone on detection
        "wake_word_debug_scores": False,    # log every score above 0.1

        # Conversation
        "max_context_turns": 6,
        "system_prompt": (
            "You are SAINT, a helpful local AI assistant running on the user's PC. "
            "Be concise and natural; your replies are spoken aloud. "
            "Only say an action happened if a tool result confirms it. "
            "If the user interrupts you, adapt immediately without apologising."
        ),
    },

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    "dashboard": {
        "complexity": "Standard",         # "Simple" | "Standard" | "Advanced" | "Developer"
        "panels": {
            "conversation": True,
            "spotify": True,
            "automations": True,
            "activity": True,
            "system": True,
        },
    },

    # ------------------------------------------------------------------
    # Appearance
    # ------------------------------------------------------------------
    "appearance": {
        "theme": "Dark",                  # "Dark" | "Light" | "System"
        "accent": "#2563eb",
        "opacity": 1.0,                   # window opacity 0.6-1.0
        "font_family": "Segoe UI",
        "font_size": 13,
        "compact": False,
        "animations": True,
        "always_on_top": False,
        "sidebar_labels": True,
    },

    # ------------------------------------------------------------------
    # Agent / tools
    # ------------------------------------------------------------------
    "agent": {
        "enabled": True,
        "confirm_timeout_sec": 30,        # pending confirmations expire after this
    },

    # ------------------------------------------------------------------
    # Automation / Permissions
    # ------------------------------------------------------------------
    "automation": {
        "enabled": True,
        "permission_mode": "confirm",     # "safe" | "confirm" | "autonomous"
        "confirm_dangerous": True,        # Require confirmation for HIGH permission tools
        "command_timeout": 30,            # seconds
        "speak_reminders": True,          # speak reminders aloud via TTS
        "missed_grace_hours": 12,         # fire one-shot reminders missed while SAINT was closed
        "default_morning_time": "08:00",  # used for "every morning"
    },

    # ------------------------------------------------------------------
    # Desktop control
    # ------------------------------------------------------------------
    "desktop": {
        "enabled": True,
        "allow_app_launch": True,
        "allow_window_control": True,
        "allow_keyboard": True,
        "allow_mouse": True,
        "confirm_close_apps": True,
        "max_type_length": 500,
        "apps": {},                       # custom "name": "path or URI" launch aliases
    },

    # ------------------------------------------------------------------
    # Vision / screen awareness
    # ------------------------------------------------------------------
    "vision": {
        "analyzer": "none",               # "none" | "ollama" (needs a vision-capable model)
        "model": "",                      # e.g. "llama3.2-vision"
        "keep_screenshots": 20,
    },

    # ------------------------------------------------------------------
    # Notifications / background
    # ------------------------------------------------------------------
    "notifications": {
        "tray": True,                     # tray balloon notifications
        "close_to_tray": True,            # closing the window keeps SAINT running
        "start_minimized": False,
    },

    # ------------------------------------------------------------------
    # Permissions and integrations
    # ------------------------------------------------------------------
    "permissions": {"overrides": {}},
    "spotify": {
        "client_id": "",
        "redirect_uri": "http://127.0.0.1:8888/callback",
        "preferred_device": "",
        "poll_interval_sec": 15,          # background listening-history sync
        "track_history": True,            # remember what you listen to / skip
        "auto_device": True,              # wake an available device if none is active
        "volume_step": 15,
    },

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------
    "memory": {
        "enabled": True,
        "auto_extract": True,             # learn explicit facts ("my favorite X is Y")
        "inject_context": True,           # give the LLM relevant stored memories
        "max_context_items": 6,
        "store_conversations": False,     # do not blindly store every turn
        "retention_days": 30,             # Conversation history retention
        "max_conversation_turns": 100,
    },

    # ------------------------------------------------------------------
    # System / Diagnostics
    # ------------------------------------------------------------------
    "system": {
        "startup_check": True,
        "log_level": "Verbose",
        "data_dir": "data",
    },
}


def _migrate(data: dict) -> dict:
    """Upgrade an older config.json in place (never drops user values)."""
    version = int(data.get("config_version", 1) or 1)
    if version < 2:
        modules = data.setdefault("modules", {})
        # Memory, automation, vision and desktop control are now core
        # features of the agent; the old defaults had them off.
        for key in ("memory", "automation", "vision", "desktop"):
            modules[key] = True
        data.setdefault("memory", {})["enabled"] = True
        data.setdefault("automation", {})["enabled"] = True
        voice = data.setdefault("voice", {})
        voice["wake_word_enabled"] = True
        if "wake_word_sensitivity" in voice and "wake_word_threshold" not in voice:
            voice["wake_word_threshold"] = voice.pop("wake_word_sensitivity")
        for stale in ("wake_word_inference_framework", "wake_word_window_sec", "wake_word_sensitivity"):
            voice.pop(stale, None)
        voice["system_prompt"] = DEFAULT_CONFIG["voice"]["system_prompt"]
        appearance = data.setdefault("appearance", {})
        if "theme" in data and "theme" not in appearance:
            appearance["theme"] = data["theme"]
        data["config_version"] = 2
    return data


class Config:
    """Thread-safe JSON-backed configuration store."""

    def __init__(self, path=None):
        self._path = str(path or data_path("config.json"))
        self._lock = threading.RLock()
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
                old_version = loaded.get("config_version", 1)
                self._data = self._merge_defaults(_migrate(loaded))
                if old_version != CONFIG_VERSION:
                    self.save()
            except (json.JSONDecodeError, OSError):
                # Keep the unreadable file for inspection instead of silently
                # overwriting the user's settings on the next save.
                try:
                    os.replace(self._path, self._path + ".corrupt")
                except OSError:
                    pass
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
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self._path)   # atomic: never leave a half-written config

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
        with self._lock:
            node = self._data
            for p in parts[:-1]:
                if not isinstance(node.get(p), dict):
                    node[p] = {}
                node = node[p]
            node[parts[-1]] = value
        if persist and self.get("auto_save", True):
            self.save()

    @property
    def path(self) -> str:
        return self._path

    def as_dict(self):
        return json.loads(json.dumps(self._data))


# Singleton instance used across the app
config = Config()

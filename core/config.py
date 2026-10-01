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

CONFIG_VERSION = 7

DEFAULT_CONFIG = {
    "config_version": CONFIG_VERSION,
    "theme": "Dark",                    # legacy key; mirrored from appearance.theme
    "ai": {
        "provider": "ollama",           # "mock" | "openai" | "ollama"
        "model": "llama3.1",
        "base_url": "http://localhost:11434",
        "api_key": "",
        "temperature": 0.5,
        "timeout_seconds": 30,
        # Hard cap on generated tokens per turn. SAINT is voice-first: long
        # replies rarely serve the user and often mean the model started
        # rambling. Applied as `num_predict` for Ollama.
        "max_tokens": 160,
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
        "watch": True,                    # "tell me when this finishes", "what changed?"
        "notifications": False,           # read Windows notifications aloud (opt-in)
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
        "vad_backend": "silero",        # "silero" (speech vs music/noise) | "rms"
        "vad_speech_prob": 0.5,         # silero speech probability to start an utterance
        "max_utterance_sec": 15.0,      # cut an utterance that never pauses (TV, music vocals)
        "followup_min_confidence": 0.25, # STT confidence floor inside the conversation window
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
        "barge_in_spotter": True,         # "stop" / "wait" / "shut up" heard while talking always interrupts
        "barge_in_min_rms": 0.025,        # quieter than this never interrupts (rises with your normal level)
        "barge_in_level_ratio": 0.4,
        "learn_my_voice": True,           # voice profile from "Hey SAINT, ..." commands (numbers only)
        "speaker_filter": False,          # ignore voices that aren't yours unless they say "SAINT"
        "barge_in_min_ms": 240,         # sustained speech needed to interrupt
        "barge_in_echo_margin": 2.0,    # how far above predicted echo the mic must be

        # STT
        "stt_backend": "faster_whisper",  # "faster_whisper" | "mock"
        "stt_model": "base.en",           # tiny.en / base.en / small.en / medium.en
        "stt_device": "cuda",             # "cuda" | "cpu"
        "stt_compute_type": "float16",    # "float16" | "int8" | "float32"
        "stt_language": "en",
        "stt_model_multilingual": "small",   # used when language.multilingual_stt is on
        "stt_initial_prompt": "Hey SAINT. Hola SAINT. Play some música.",
        "stt_hotwords": "SAINT",          # biases Whisper toward spelling the wake word correctly

        # TTS
        "tts_backend": "kokoro",          # "kokoro" | "qwen" | "mock"
        "tts_model_dir": "data/tts",
        "tts_voice": "af_heart",          # kokoro voice name
        "tts_voice_blend": "",            # a second Kokoro voice mixed in ("" = none)
        "tts_voice_blend_pct": 50,
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
        "wake_word_trigger_frames": 1,      # frames above threshold needed within the detection window
        "wake_word_window_frames": 4,       # detection window (80 ms frames) for trigger_frames
        "wake_word_transcript_check": True, # 2nd stage: transcribe short utterances the model missed and
                                            # accept them only if they start with "SAINT" / "Hey SAINT"
        "wake_word_transcript_max_sec": 7.0,
        "wake_word_refractory_sec": 2.0,    # cooldown between detections
        "wake_word_command_timeout_sec": 6.0,  # give up if no command starts in this time
        "wake_word_followup_sec": 12.0,     # conversation window: follow-ups need no wake word (0 = off);
                                            # counted from when SAINT stops talking
        # Adaptive follow-up: after an action reply ("Playing music."), extend
        # the window because a follow-up ("skip", "louder") is likely. Capped so
        # SAINT still returns to wake-word listening if the user walks away.
        "wake_word_followup_action_bonus_sec": 15.0,
        "wake_word_followup_max_sec": 45.0,
        # When Spotify is playing, follow-up transcripts get flooded with
        # song lyrics. Only follow-ups SAINT recognises as commands (or short
        # direct questions) are kept; "Hey SAINT, <anything>" always works.
        "music_strict_followup": True,
        # ...and a "command" that is a line of the song playing (synced lyrics) is dropped.
        "lyrics_filter": True,
        # Whisper scores short commands low; a follow-up SAINT recognises as a
        # command ("click it") only needs this much confidence.
        "followup_command_min_confidence": 0.1,
        "wake_word_chime": True,            # short tone on detection
        "wake_word_debug_scores": False,
        # While Spotify is playing, short playback commands ("skip", "pause",
        # "louder", "go back") work without saying "Hey SAINT" first.
        "music_hotwords": True,
        "hotword_min_confidence": 0.2,     # the whole utterance must be a playback command
        # Follow-ups without the wake word must look like a command, a direct
        # question or an answer to SAINT's question — chatter is ignored.
        "followup_requires_intent": True,
        # Whisper-quiet: when you speak quietly SAINT answers quietly.
        "whisper_replies": True,
        "whisper_rms": 0.02,              # utterances below this RMS count as whispering
        "whisper_gain": 0.45,             # reply volume multiplier while whispering
        # Drop Whisper hallucinations like "No, no, no, no, ..." (a repeated phrase)
        "repetition_filter": True,

        # Conversation
        "max_context_turns": 6,
        "system_prompt": (
            "You are SAINT, a voice-first assistant on the user's PC. "
            "Answer in ONE short sentence — usually under 15 words. "
            "For actions, a short confirmation like 'Skipped it.' or 'Spotify is open.' "
            "is enough; do not narrate steps. For facts, give the direct answer only "
            "(e.g. '828 meters.'). Never repeat the user's request, never explain what you're "
            "about to do, never use markdown, JSON, code fences or tool names. "
            "Only say an action happened if a tool result confirms it. "
            "Expand only when the user explicitly asks for detail."
        ),
    },

    # ------------------------------------------------------------------
    # Appearance
    # ------------------------------------------------------------------
    "appearance": {
        "theme": "Dark",                  # "Dark" | "Light" | "System"
        "accent": "#feaa34",             # SAINT orange (from the logo)
        "opacity": 1.0,                   # window opacity 0.6-1.0
        "font_family": "Segoe UI",
        "font_size": 13,
        "compact": False,
        "animations": True,
        "always_on_top": False,
        "sidebar_labels": True,
    },

    # ------------------------------------------------------------------
    # Halo (animated screen-edge glow), overlay and floating widgets
    # ------------------------------------------------------------------
    "overlay": {
        "halo": "minimized",              # "minimized" | "always" | "off"
        "halo_all_screens": False,
        "edge_tab": True,                 # rest the cursor at the top-centre edge to reveal the SAINT tab
        "hotkey": "alt+`",                # opens / closes the overlay from anywhere (the key left of 1)
        "layout": {},                     # card -> [x, y, w, h] (fractions of the screen), set by dragging
    },
    "widgets": {
        "spotify": False,                 # floating always-on-top now-playing widget
        "spotify_pos": None,              # [x, y], remembered after dragging
        "any_media": True,                # show any app's media (YouTube, VLC, ...), not only Spotify
        "lyrics": False,                  # synced lyrics panel under the mini player (LRCLIB)
    },

    # ------------------------------------------------------------------
    # Local usage history (data/history.jsonl — never uploaded)
    # ------------------------------------------------------------------
    "history": {
        "enabled": True,
        "max_entries": 5000,
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
        "focus_guard": True,              # never pull you out of a game / your editor unless you asked for a window
        # How long the window SAINT last worked in stays the default target
        # for "click X" / "scroll down" before the foreground window wins.
        "context_window_ttl_sec": 45.0,
        # Several matching windows: "smart" = the one you used most recently
        # (asks only when none was used lately) | "ask" | "recent" = newest
        "multi_window_policy": "smart",
        "recent_focus_sec": 600,          # "smart": how recently a window must have been used
        "preferred_browser": None,        # the browser window picked last time (kept until it closes)
        "max_type_length": 500,
        "apps": {},                       # custom "name": "path or URI" launch aliases
    },

    # ------------------------------------------------------------------
    # Files, storage and archives (modules/files). Nothing is ever deleted
    # permanently: removals go to the Recycle Bin after you confirm.
    # ------------------------------------------------------------------
    "files": {
        "games_dir": "D:\\Games",        # "my games folder"
        "known": {},                      # extra named folders: {"emulators": "D:\\Emulators"}
        "scan_cache_hours": 24,
        "temp_age_days": 2,               # temp files older than this count as junk
        "recordings_age_days": 60,        # Medal / clips older than this are listed (never removed automatically)
    },

    # ------------------------------------------------------------------
    # Steam (modules/steam)
    # ------------------------------------------------------------------
    "steam": {
        "extra_libraries": ["D:\\SteamLibrary"],   # libraries Steam may not know about
    },

    # ------------------------------------------------------------------
    # Game Mode (core/game_mode.py): while a game runs, SAINT hides its
    # always-on-top windows (Halo, edge tab, action pill, mini player) — they
    # upset anti-cheat and fullscreen — and won't capture the screen or
    # click/type into the game. Voice, Spotify and volume keep working.
    # ------------------------------------------------------------------
    "game_mode": {
        "enabled": True,                  # Auto Gaming Mode: turn Gaming Mode on when a game starts
        "detect": True,                   # notice running games at all (safety rules + "what am I playing")
        "protect_overlays": True,         # no see-through top-most windows over a running game (anti-cheat)
        "features": {},                   # per-feature Gaming Mode settings (core/game_mode.FEATURE_DEFAULTS)
        "saint_monitor": "auto",          # where SAINT goes while gaming: "auto" (not the game's) or a monitor number
        "workspace_spotify": True,        # "set up my gaming workspace" also opens Spotify
        "processes": [],                  # extra game .exe names ("mygame.exe")
        "folders": [],                    # extra folders whose programs are games
        "ignore": [],                     # .exe names that are never games
        "block_capture": True,            # no screenshots / screen reading while a game runs
        "block_input": True,              # no synthetic clicks / typing into the game window
        "announce": True,                 # toast when Game Mode turns on / off
    },

    # ------------------------------------------------------------------
    # Learning (modules/learning): SAINT working out what it can't do yet,
    # learning from "no, I meant ..." and from watching the user do it once.
    # Learned requests live in data/skills.json ("what have you learned",
    # "forget that").
    # ------------------------------------------------------------------
    "learning": {
        "planner": True,                  # ask the local model to plan unknown/failed requests
        "planner_timeout_sec": 25,
        "watch_and_learn": True,          # may watch the user show it how (announced, time-limited)
        "watch_after_failure": True,      # start watching by itself after "I don't know how yet"
        "from_mistakes": True,            # unlearn on "that's wrong", learn rephrasings, keep a mistake journal
        "watch_max_sec": 120,
        "watch_idle_sec": 15,             # stop 15 s after the last thing the user did
    },

    # ------------------------------------------------------------------
    # Sound: Voicemeeter and the bare "mute" command
    # ------------------------------------------------------------------
    "audio": {
        "bare_mute": "mic",               # "mute" / "unmute" alone: "mic" or "system"
        "turn_it_means": "system",        # bare "turn it down": "system" (Voicemeeter/Windows) or "music"
        "voicemeeter": {
            "enabled": "auto",            # auto (when running) / on / off
            "mic_strip": -1,              # -1 = the input strip with a microphone on it
        },
    },

    # ------------------------------------------------------------------
    # Developer mode (modules/dev)
    # ------------------------------------------------------------------
    "dev": {
        "project_dir": "",                # "run the tests" runs here (blank = SAINT's own folder)
        "test_command": "python -m pytest -q",
        "editor": "code",                 # opens "the file causing the error"
    },

    # ------------------------------------------------------------------
    # Watching (modules/watch): "tell me when this finishes", "what changed?"
    # ------------------------------------------------------------------
    "watch": {
        "snapshot_sec": 10,               # how often the screen state is noted
        "keep_min": 30,                   # how far back "what changed?" can look
        "screen_hash": True,              # also note a tiny per-monitor image hash
        "max_watchers": 5,
        "expire_min": 120,
    },

    # ------------------------------------------------------------------
    # Vision / screen awareness
    # ------------------------------------------------------------------
    "vision": {
        "analyzer": "none",               # "none" | "ollama" | "flux"
        "allow_screen_context": True,     # read windows/controls/text via UI Automation (on demand)
        "model": "",                      # e.g. "llama3.2-vision"
        "keep_screenshots": 20,
        # FLUX.2 Klein 4B (local vision-language model). Loaded on demand and
        # kept in memory between calls. When required packages/weights are
        # missing SAINT reports the actual dependency instead of falling back
        # silently. Get weights: `hf download black-forest-labs/FLUX.2-Klein-4B`.
        "flux_model_id": "black-forest-labs/FLUX.2-Klein-4B",
        "flux_model_dir": "data/vision/flux2-klein-4b",
        "flux_device": "auto",           # "auto" | "cuda" | "cpu"
        "flux_dtype": "float16",          # "float16" | "bfloat16" | "float32"
        "flux_max_image_side": 1024,
        "flux_max_new_tokens": 160,
    },

    # ------------------------------------------------------------------
    # Web / current information
    # ------------------------------------------------------------------
    # SAINT distinguishes "search Google for X" (browser automation) from
    # "what's the latest news about X" (a factual current-info question).
    # When no provider is configured the current-info intent replies honestly
    # instead of opening the browser.
    "web": {
        # Default: open DuckDuckGo in the user's existing browser (no key needed).
        # Alternatives: "duckduckgo" (Instant Answer API), "tavily", "serpapi", "none".
        "provider": "duckduckgo_browser",
        "api_key": "",
        "max_results": 4,
        "answer_style": "concise",       # "concise" | "detailed"
    },

    # ------------------------------------------------------------------
    # Notifications / background
    # ------------------------------------------------------------------
    "notifications": {
        "tray": True,                     # tray balloon notifications
        "close_to_tray": True,            # closing the window keeps SAINT running
        "start_minimized": False,
        "actions": True,                  # small pill at the bottom of the screen when SAINT does something
        # Windows notifications read aloud (modules/notifications, opt-in)
        "important_only": True,           # only announce notifications that match the rules below
        "allow": [],                      # apps always announced, e.g. ["Discord"]
        "deny": [],                       # apps never announced
        "keywords": ["failed", "error", "completed", "finished", "mentioned you", "calling"],
        "poll_sec": 5,
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
        "volume_step": 10,                # "turn Spotify up"; "a little" halves it, "a lot" doubles it
        "learn_playlist_dislikes": True,  # a song you keep skipping in a playlist is noted as disliked there
        "skip_disliked_in_playlists": True,
        "learn_singing": True,            # singing along (a lyric in your voice) counts as liking the song
        "autoqueue": True,                # a song / mood / "more like this" keeps going: 5 similar songs at a time
    },

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------
    "memory": {
        "enabled": True,
        "auto_extract": True,             # learn explicit facts ("my favorite X is Y")
        "learn_passive": True,            # pick up plain statements ("I'm a nurse", "I hate horror movies")
        "learn_with_ai": True,            # let the local model find lasting facts in what you say
        "learned_ttl_days": 60,           # learned facts that never come up again fade after this
        "inject_context": True,           # give the LLM relevant stored memories
        "max_context_items": 6,
        "store_conversations": False,     # do not blindly store every turn
        "retention_days": 30,             # Conversation history retention
        "max_conversation_turns": 100,
    },

    # ------------------------------------------------------------------
    # SAINT Link (modules/link): connect your phone and other PCs, or a friend's
    # SAINT, over IP:port. Nothing listens until "enabled" is on.
    # ------------------------------------------------------------------
    "link": {
        "enabled": False,
        "port": 8765,
        "bind": "0.0.0.0",                # "127.0.0.1" = this PC only (for testing)
        "device_name": "",                # blank = this PC's name
        "discoverable": True,             # mDNS + a small UDP beacon so your phone finds this PC
        "auto_connect": True,             # keep dialling paired devices
        "sync_interval_sec": 60,
        "share_context": True,            # tell your other devices what you just asked (memory only)
        "announce": True,                 # speak "Gian sent you a file" and friends
        "approval_timeout_sec": 60,       # how long a collaborator's request waits for your yes
        "max_file_mb": 1024,
        "max_prompt_chars": 2000,
        "inbox_dir": "",                  # blank = data/link/inbox
        "prompt_targets": {},             # extra apps for "send this prompt to ... on <app>"
        "shared_scenes": [],              # scenes collaborators may run
    },

    # ------------------------------------------------------------------
    # Languages (modules/lang): understand and answer in the user's language,
    # including when two are mixed in one sentence.
    # ------------------------------------------------------------------
    "language": {
        "auto_detect": True,              # work out the language of each request
        "reply_in_user_language": True,   # answer in it (else always English)
        "preferred": [],                  # e.g. ["es", "en"]: tie-breakers, and what the phone offers first
        "mixed_mode": "mirror",           # "mirror" = answer in the mix you used, "dominant" = one language
        "llm_translate": True,            # let the local model translate what the phrasebook can't
        "sticky_minutes": 10,             # a one-word answer keeps the language of the conversation
        "multilingual_stt": False,        # Whisper auto-detects the language (needs a multilingual model)
    },

    # ------------------------------------------------------------------
    # System / Diagnostics
    # ------------------------------------------------------------------
    "system": {
        "start_with_windows": False,      # mirrors the Startup-folder shortcut (core/autostart.py)
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
    if version < 3:
        # SAINT's accent is now orange; only replace the old *default* blue.
        appearance = data.setdefault("appearance", {})
        if str(appearance.get("accent", "#2563eb")).lower() == "#2563eb":
            appearance["accent"] = "#feaa34"
        data["config_version"] = 3
    if version < 4:
        # Reworked wake word + conversation window (see modules/voice/module.py).
        voice = data.setdefault("voice", {})
        if float(voice.get("wake_word_followup_sec", 0) or 0) <= 0:
            voice["wake_word_followup_sec"] = 15.0
        if int(voice.get("wake_word_trigger_frames", 1) or 1) >= 2:
            voice["wake_word_trigger_frames"] = 1
        voice["wake_word_enabled"] = True
        data["config_version"] = 4
    if version < 5:
        # Accent now matches the SAINT logo; only earlier *defaults* are replaced.
        appearance = data.setdefault("appearance", {})
        if str(appearance.get("accent", "")).lower() in ("#f97316", "#2563eb", ""):
            appearance["accent"] = "#feaa34"
        data["config_version"] = 5
    if version < 6:
        # Voice-first response policy: replace the older, longer default prompt
        # with the concise-by-default one. Only touches the default text — a
        # user-customised prompt is preserved.
        voice = data.setdefault("voice", {})
        old_prompts = {
            "You are SAINT, a helpful AI assistant. Be concise.",
            ("You are SAINT, a helpful local AI assistant running on the user's PC. "
             "Be concise and natural; your replies are spoken aloud. "
             "Only say an action happened if a tool result confirms it. "
             "If the user interrupts you, adapt immediately without apologising."),
        }
        if voice.get("system_prompt") in old_prompts:
            voice["system_prompt"] = DEFAULT_CONFIG["voice"]["system_prompt"]
        ai = data.setdefault("ai", {})
        ai.setdefault("max_tokens", 160)
        if ai.get("temperature") in (0.7, None):
            ai["temperature"] = 0.5
        data["config_version"] = 6
    if version < 7:
        # 'llama3' was never an installed tag, so every turn warned and fell back.
        ai = data.setdefault("ai", {})
        if ai.get("model") in ("llama3", "llama3:latest"):
            ai["model"] = "llama3.1"
        # Asking "which browser?" every time was the most common failed command.
        desktop = data.setdefault("desktop", {})
        if desktop.get("multi_window_policy", "ask") == "ask":
            desktop["multi_window_policy"] = "smart"
        data["config_version"] = 7
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

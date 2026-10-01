"""
core/runtime.py

SaintRuntime boots and owns the long-running services, independent of any
window:

    TTS service ─┐
    AI + agent  ─┼─ ConversationController ── event bus ── UI (optional)
    Voice/wake  ─┘
    Scheduler (reminders / automations)
    Spotify poller (started by the Spotify module)

The Qt window only renders state and forwards user input, so SAINT keeps
listening for its wake word, speaking reminders and controlling Spotify when
the window is minimised, hidden to the tray, or not focused.
"""

import logging
import threading
from typing import Optional

from core.assistant_state import assistant_state, AssistantState
from core.config import config
from core.events import event_bus, EventType
from modules.voice.voices import kokoro_voice

log = logging.getLogger("saint.runtime")

# Settings that require the microphone loop / wake detector to restart.
_VOICE_RESTART_KEYS = ("voice.mic_device", "voice.mode", "voice.silence_duration_ms",
                       "voice.mic_sensitivity", "voice.noise_suppression", "voice.vad_start_threshold",
                       "voice.vad_end_threshold", "voice.barge_in_enabled", "voice.barge_in_min_ms",
                       "voice.barge_in_echo_margin", "voice.wake_word_debug_scores")
_WAKE_KEYS = ("voice.wake_word_enabled", "voice.wake_word_model_path", "voice.wake_word_feature_dir",
              "voice.wake_word_threshold", "voice.wake_word_trigger_frames", "voice.wake_word_refractory_sec")
_TTS_KEYS = ("voice.tts_backend", "voice.tts_voice", "voice.tts_voice_blend", "voice.tts_voice_blend_pct",
             "voice.tts_device", "voice.tts_speed",
             "voice.tts_qwen_model", "voice.tts_qwen_speaker", "voice.tts_qwen_type")
_STT_KEYS = ("voice.stt_backend", "voice.stt_model", "voice.stt_device", "voice.stt_compute_type",
             "voice.stt_language", "language.multilingual_stt")


def tts_settings() -> dict:
    backend = config.get("voice.tts_backend", "kokoro")
    if backend == "qwen":
        return {
            "model_name": config.get("voice.tts_qwen_model", "Qwen/Qwen3-TTS"),
            "model_type": config.get("voice.tts_qwen_type", "custom_voice"),
            "speaker": config.get("voice.tts_qwen_speaker", "eric"),
            "language": config.get("voice.tts_qwen_language", "Auto"),
            "device": config.get("voice.tts_device", "cuda"),
            "dtype": config.get("voice.tts_qwen_dtype", "bfloat16"),
            "voice_clone_audio": config.get("voice.tts_qwen_voice_clone_audio", "") or None,
            "voice_clone_text": config.get("voice.tts_qwen_voice_clone_text", "") or None,
            "x_vector_only": config.get("voice.tts_qwen_x_vector_only", False),
            "instruct": config.get("voice.tts_qwen_instruct", "") or None,
            "speed": config.get("voice.tts_speed", 1.0),
            "flash_attention": config.get("voice.tts_qwen_flash_attention", "Auto"),
            "allow_cpu_fallback": config.get("voice.tts_allow_cpu_fallback", True),
            "require_cuda": config.get("voice.tts_require_cuda", False),
        }
    if backend == "kokoro":
        return {
            "voice": kokoro_voice(config.get("voice.tts_voice", "af_heart"), config.get("voice.tts_voice_blend", ""),
                                  config.get("voice.tts_voice_blend_pct", 50)),
            "device": config.get("voice.tts_device", "cuda"),
            "speed": config.get("voice.tts_speed", 1.0),
            "allow_cpu_fallback": config.get("voice.tts_allow_cpu_fallback", True),
            "require_cuda": config.get("voice.tts_require_cuda", False),
        }
    return {}


class SaintRuntime:
    def __init__(self):
        self.controller = None
        self.tts = None
        self._started = False
        self._snapshot = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
        from core.module_manager import module_manager
        from core.conversation import init_controller
        from modules.voice.tts_service import get_tts_service

        self._snapshot = config.as_dict()
        ai = module_manager.get("ai")
        voice = module_manager.get("voice")

        # TTS loads in the background; the controller works immediately (the
        # TTS queue waits for the engine).
        from core.startup import startup
        startup.run("config", "Settings", lambda: None)
        self.tts = get_tts_service()

        def start_tts():
            try:
                self.tts.initialize(backend=config.get("voice.tts_backend", "kokoro"), blocking=False,
                                    **tts_settings())
            except Exception as e:
                event_bus.emit_event(EventType.TTS_ERROR, {"error": f"Text-to-speech failed to start: {e}"})
                raise
        startup.run("tts", "Speech output", start_tts)
        self.controller = init_controller(ai_module=ai, voice_module=voice, tts_engine=self.tts)

        # Scheduler: reminders are spoken through the controller, scheduled
        # commands run through the agent.
        from modules.automation.scheduler import scheduler
        from modules.agent.agent import agent
        scheduler.announce = self.controller.announce
        scheduler.run_command = agent.run_command
        def load_memory():
            from modules.automation.scenes import scenes
            scenes.ensure_defaults()           # Gaming mode / Done gaming / Dev environment (once)
            from modules.agent.task_memory import task_memory
            task_memory.load()                 # "continue what we were doing" after a restart
        startup.run("memory", "Memory and tasks", load_memory)
        startup.run("automation", "Reminders and automations", scheduler.start,
                    enabled=bool(config.get("modules.automation", True) and config.get("automation.enabled", True)))

        # Voice: always-on wake-word listening.
        if config.get("modules.voice", True):
            module_manager.set_enabled("voice", True)
            if config.get("voice.auto_start", True):
                def listen():
                    if not self.start_listening():
                        raise RuntimeError("the microphone didn't start — check Settings > Voice > Microphone")
                startup.run("voice", "Voice and wake word", listen)
        else:
            assistant_state.set_resting(AssistantState.OFFLINE, detail="Voice module disabled")

        # What's playing in any app (Windows media controls). Started here, not
        # only by the UI, so the Spotify agent notices track changes / skips
        # made in the Spotify app instantly even with no window open.
        def start_media():
            from modules.desktop.media import media
            media.start()
        startup.run("media", "Now playing (Spotify and media)", start_media)

        # SAINT Link: listen for / dial your phone, other PCs and paired friends (off until turned on).
        def start_link():
            from modules.link.service import get_link
            get_link().start()
        startup.run("link", "SAINT Link (phone and other PCs)", start_link,
                    enabled=bool(config.get("link.enabled", False)))

        # Game Mode: hide overlays and stop screen capture while a game runs.
        def start_game_mode():
            from core.game_mode import game_mode
            game_mode.start()
        startup.run("game_mode", "Game detection and Gaming Mode", start_game_mode)

        def detect_monitors():
            from modules.desktop.controller import desktop
            mons = desktop.monitors()
            log.info("runtime.monitors %s", [(m.index, m.width, m.height, m.primary) for m in mons])
        startup.run("monitors", "Monitors", detect_monitors)

        threading.Thread(target=self._check_services, daemon=True, name="startup-checks").start()
        event_bus.subscribe(self._on_event)
        log.info("runtime.started voice=%s wake=%s automation=%s",
                 config.get("modules.voice", True), config.get("voice.wake_word_enabled", True),
                 config.get("automation.enabled", True))

    @staticmethod
    def _check_services():
        """The AI model and Spotify are network services: check them off the startup path."""
        from core.startup import startup
        from core.module_manager import module_manager

        def check_ai():
            provider = config.get("ai.provider", "ollama")
            if provider != "ollama":
                return
            import requests
            base = config.get("ai.base_url", "http://localhost:11434").rstrip("/")
            try:
                ok = requests.get(base + "/api/version", timeout=3).ok
            except Exception:
                ok = False
            if not ok:
                raise RuntimeError(f"Ollama isn't answering at {base} — start Ollama, then Retry")
        startup.run("ai", "AI model (Ollama)", check_ai)

        def check_spotify():
            sp = module_manager.get("spotify")
            if sp is None or not sp.enabled:
                return
            ok, why = sp.availability()
            if not ok:
                raise RuntimeError(why)
            sp.start_poller()
        startup.run("spotify", "Spotify", check_spotify, enabled=bool(config.get("modules.spotify", True)))

    def start_listening(self) -> bool:
        from core.module_manager import module_manager
        voice = module_manager.get("voice")
        if not voice.enabled:
            module_manager.set_enabled("voice", True)
        return voice.start_listening()

    def stop_listening(self):
        from core.module_manager import module_manager
        module_manager.get("voice").stop_listening()

    @property
    def listening(self) -> bool:
        from core.module_manager import module_manager
        return module_manager.get("voice").voice_active

    def shutdown(self):
        log.info("runtime.shutdown")
        try:
            self.stop_listening()
        except Exception:
            pass
        try:
            from modules.automation.scheduler import scheduler
            scheduler.stop()
        except Exception:
            pass
        try:
            from core.module_manager import module_manager
            sp = module_manager.get("spotify")
            if sp:
                sp.stop_poller()
        except Exception:
            pass
        # Background watchers are daemon threads, but stop them explicitly so
        # nothing keeps polling or capturing while the process winds down.
        for stop in (self._stop_link, self._stop_game_mode, self._stop_watch, self._stop_focus_history):
            try:
                stop()
            except Exception:
                pass
        if self.controller:
            self.controller.shutdown()

    @staticmethod
    def _stop_link():
        from modules.link.service import get_link
        get_link().stop()

    @staticmethod
    def _stop_game_mode():
        from core.game_mode import game_mode
        game_mode.stop()

    @staticmethod
    def _stop_watch():
        from modules.watch.snapshots import snapshot_log
        snapshot_log.stop()

    @staticmethod
    def _stop_focus_history():
        from modules.desktop.focus_history import focus_history
        focus_history.stop()

    # ------------------------------------------------------------------ #
    # Live settings
    # ------------------------------------------------------------------ #
    def _changed(self, keys) -> bool:
        def get(d, k):
            for p in k.split("."):
                if not isinstance(d, dict):
                    return None
                d = d.get(p)
            return d
        return any(get(self._snapshot, k) != config.get(k) for k in keys)

    def _on_event(self, ev):
        if ev.type == EventType.TASK_DONE:
            # Background work (scans, extraction) finishing: say so, and show a toast.
            p = ev.payload or {}
            summary = p.get("summary", "")
            if summary and p.get("announce", True) and p.get("status") != "cancelled":
                event_bus.emit_event(EventType.NOTIFY, {"title": "SAINT", "message": summary})
                if self.controller:
                    self.controller.announce(summary, source="task")
            return
        if ev.type == EventType.TTS_ERROR and (ev.payload or {}).get("phase") == "initialization":
            # Speech output loads in the background: a failure shows up here, after startup said "started".
            from core.startup import startup

            def retry():
                self.tts.initialize(backend=config.get("voice.tts_backend", "kokoro"), blocking=False,
                                    **tts_settings())
            startup.report("tts", "Speech output", False, str((ev.payload or {}).get("error", ""))[:200], retry=retry)
            return
        if ev.type != EventType.SETTINGS_CHANGED:
            return
        threading.Thread(target=self.apply_settings, daemon=True, name="apply-settings").start()

    def apply_settings(self):
        from core.logger import apply_level
        from core.module_manager import module_manager
        apply_level(config.get("logging.level", "Normal"), config.get("logging.debug", False))
        voice = module_manager.get("voice")
        ai = module_manager.get("ai")

        try:
            ai.context._system_prompt = config.get("voice.system_prompt", ai.context._system_prompt)
            ai.context._max_turns = int(config.get("voice.max_context_turns", 6))
        except Exception:
            pass

        if self._changed(_TTS_KEYS) and self.tts is not None:
            log.info("runtime.settings tts changed → reinitialising TTS")
            self.tts.reinitialize(blocking=False, backend=config.get("voice.tts_backend", "kokoro"),
                                  **tts_settings())
        restart_voice = self._changed(_VOICE_RESTART_KEYS) or self._changed(_STT_KEYS)
        if self._changed(_STT_KEYS):
            voice._stt = None
            voice._vad = None
        if self._changed(_WAKE_KEYS) and not restart_voice:
            log.info("runtime.settings wake word changed → reloading detector")
            voice.reload_wake_word()
        if restart_voice and voice.voice_active:
            log.info("runtime.settings audio changed → restarting microphone")
            voice.stop_listening()
            voice._vad = None if voice._vad is not None and self._changed(_VOICE_RESTART_KEYS) else voice._vad
            if voice._vad is None or voice._stt is None:
                voice._init_engines()
            voice.start_listening()

        from modules.automation.scheduler import scheduler
        if config.get("automation.enabled", True) and config.get("modules.automation", True):
            scheduler.start()
        self._snapshot = config.as_dict()


runtime = SaintRuntime()

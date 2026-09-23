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

log = logging.getLogger("saint.runtime")

# Settings that require the microphone loop / wake detector to restart.
_VOICE_RESTART_KEYS = ("voice.mic_device", "voice.mode", "voice.silence_duration_ms",
                       "voice.mic_sensitivity", "voice.noise_suppression", "voice.vad_start_threshold",
                       "voice.vad_end_threshold", "voice.barge_in_enabled", "voice.barge_in_min_ms",
                       "voice.barge_in_echo_margin", "voice.wake_word_debug_scores")
_WAKE_KEYS = ("voice.wake_word_enabled", "voice.wake_word_model_path", "voice.wake_word_feature_dir",
              "voice.wake_word_threshold", "voice.wake_word_trigger_frames", "voice.wake_word_refractory_sec")
_TTS_KEYS = ("voice.tts_backend", "voice.tts_voice", "voice.tts_device", "voice.tts_speed",
             "voice.tts_qwen_model", "voice.tts_qwen_speaker", "voice.tts_qwen_type")
_STT_KEYS = ("voice.stt_backend", "voice.stt_model", "voice.stt_device", "voice.stt_compute_type",
             "voice.stt_language")


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
            "voice": config.get("voice.tts_voice", "af_heart"),
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
        self.tts = get_tts_service()
        try:
            self.tts.initialize(backend=config.get("voice.tts_backend", "kokoro"), blocking=False,
                                **tts_settings())
        except Exception as e:
            log.exception("runtime.tts_init_failed")
            event_bus.emit_event(EventType.TTS_ERROR, {"error": f"Text-to-speech failed to start: {e}"})
        self.controller = init_controller(ai_module=ai, voice_module=voice, tts_engine=self.tts)

        # Scheduler: reminders are spoken through the controller, scheduled
        # commands run through the agent.
        from modules.automation.scheduler import scheduler
        from modules.agent.agent import agent
        scheduler.announce = self.controller.announce
        scheduler.run_command = agent.run_command
        if config.get("modules.automation", True) and config.get("automation.enabled", True):
            try:
                scheduler.start()
            except Exception:
                log.exception("runtime.scheduler_failed")

        # Voice: always-on wake-word listening.
        if config.get("modules.voice", True):
            module_manager.set_enabled("voice", True)
            if config.get("voice.auto_start", True):
                self.start_listening()
        else:
            assistant_state.set_resting(AssistantState.OFFLINE, detail="Voice module disabled")

        event_bus.subscribe(self._on_event)
        log.info("runtime.started voice=%s wake=%s automation=%s",
                 config.get("modules.voice", True), config.get("voice.wake_word_enabled", True),
                 config.get("automation.enabled", True))

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
        if self.controller:
            self.controller.shutdown()

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

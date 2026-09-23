"""
modules/voice/tts.py

Text-to-Speech engine abstraction.

Backends:
  KokoroTTS  — `kokoro` (Hexgrad) package via KPipeline; downloads model/voices
               lazily from HuggingFace on first use.  24 kHz float32 audio.
  QwenTTS    — Qwen3-TTS via HuggingFace transformers; supports CustomVoice,
               VoiceDesign, Base (voice clone)
  MockTTS    — prints tokens to simulate speech; no audio hardware required

Both expose:
  speak(text)    — synthesise and play; blocks until done or interrupted
  interrupt()    — stop playback immediately
  is_speaking()  — True while audio is playing

Word streaming: text is split into individual words and each word is
synthesised + started playing before the next is generated. This gives
near-instantaneous TTFB on a modern GPU and enables word-by-word UI updates.
"""

import os
import torch
import threading
import time
from typing import Optional, Callable, Tuple, List, Union

import numpy as np

from core.audio_echo import playback_monitor


# ---------------------------------------------------------------------------
# Word splitter
# ---------------------------------------------------------------------------
def _split_words(text: str):
    """Split text into individual words, preserving punctuation attached to words."""
    return text.strip().split()


# Shown when CUDA is present but has no compiled kernels for the installed GPU
# architecture (e.g. Blackwell / sm_120 on a torch build that predates it).
_CUDA_ARCH_HINT = (
    "Your GPU architecture may be newer than the installed PyTorch supports; "
    "for RTX 50-series (sm_120) use the CUDA 12.8+ wheels "
    "(pip install --index-url https://download.pytorch.org/whl/cu128 torch)."
)


# ---------------------------------------------------------------------------
# FlashAttention2 resolution
# ---------------------------------------------------------------------------
_FLASH_ATTENTION_CHOICES = {"Auto", "Enabled", "Disabled"}


def _is_flash_attention_available() -> bool:
    """True if the ``flash_attn`` package is importable in this environment."""
    try:
        import py  # noqa: F401
        return True
    except Exception:
        return False


def _resolve_attn_implementation(flash_mode: str, device_is_cuda: bool) -> str:
    """Resolve which attention implementation the Qwen model should use.

    The absence of flash_attn must NEVER prevent the model from loading.

    Behaviour by mode:
      * ``Auto``     — use flash_attention_2 only when the package is actually
                       installed (and we are on CUDA), otherwise fall back to SDPA.
      * ``Enabled``  — attempt flash_attention_2, but gracefully fall back to
                       SDPA if the package is unavailable / incompatible.
      * ``Disabled`` — never attempt to import or use flash_attention_2.

    SDPA (TorchSDPA) is the standard, always-available PyTorch/TorchSDPA backend
    that Transformers ships with, so it is a safe default for a fresh install.
    """
    mode = (flash_mode or "Auto")
    if mode not in _FLASH_ATTENTION_CHOICES:
        mode = "Auto"

    if mode == "Disabled":
        return "sdpa"

    use_flash = device_is_cuda and _is_flash_attention_available()
    if use_flash:
        return "flash_attention_2"

    import logging
    logger = logging.getLogger("saint.tts")
    if mode == "Enabled" and device_is_cuda:
        logger.warning(
            "FlashAttention2 was requested (Flash Attention: Enabled) but the "
            "flash_attn package is not installed. Falling back to SDPA. "
            "Install flash-attn only if you want this backend; SDPA is fully supported."
        )
    elif device_is_cuda:
        logger.info(
            "flash_attn not installed; using SDPA attention (Flash Attention: Auto)."
        )
    return "sdpa"


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class TTSEngine:
    def speak(self, text: str, turn_id: int = 0, on_chunk_start: Optional[Callable[[str], None]] = None):
        t_start = time.perf_counter()
        raise NotImplementedError

    def interrupt(self, turn_id: int = None):
        raise NotImplementedError

    def is_speaking(self) -> bool:
        raise NotImplementedError

    def warm_up(self):
        pass


# ---------------------------------------------------------------------------
# Kokoro TTS (kokoro / KPipeline)
# ---------------------------------------------------------------------------
class KokoroTTS(TTSEngine):
    """
    Word-streaming TTS using the Hexgrad ``kokoro`` package via KPipeline.

    The pipeline lazily downloads the model + voice files from HuggingFace on
    first use, so no manual model-file placement is required.  Audio is
    emitted at 24 kHz as float32 numpy arrays.

    Each word is synthesised on-GPU then played via sounddevice.  While word N
    is playing, word N+1 is being generated — giving very low perceived
    latency.
    """

    def __init__(
        self,
        voice: str = "af_heart",
        speed: float = 1.0,
        device: str = "cuda",           # "cuda" | "cuda:N" | "cpu" | "auto"
        allow_cpu_fallback: bool = True,  # degrade to CPU if CUDA unusable
        require_cuda: bool = False,      # hard-fail (with diagnosis) if no CUDA
    ):
        self._voice = voice
        self._speed = speed
        self._device = device                    # requested device (from config)
        self._allow_cpu_fallback = allow_cpu_fallback
        self._require_cuda = require_cuda
        self._resolved_device: Optional[str] = None  # actual device after _load()
        self._device_resolution = None            # DeviceResolution from core.device
        self._pipeline = None
        self._interrupt_event = threading.Event()
        self._speaking = False
        self._currently_playing = False
        self._playback_active = False
        self._lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._load_error: Optional[Exception] = None
        self._load_attempted = False
        self._active_turn_id: int = -1

        import queue
        self._audio_queue = queue.Queue()
        self._playback_thread = threading.Thread(target=self._playback_loop, daemon=True)
        self._playback_thread.start()

    def _playback_loop(self):
        import sounddevice as sd
        import time
        from core.events import event_bus, EventType

        sample_rate = 24000
        stream = sd.OutputStream(samplerate=sample_rate, channels=1)
        stream.start()

        try:
            while True:
                if self._interrupt_event.is_set():
                    while not self._audio_queue.empty():
                        try:
                            self._audio_queue.get_nowait()
                        except:
                            pass
                    self._currently_playing = False
                    self._playback_active = False
                    time.sleep(0.05)
                    continue

                try:
                    item = self._audio_queue.get(timeout=0.1)
                except Exception:
                    self._currently_playing = False
                    self._playback_active = False
                    continue

                self._currently_playing = True
                self._playback_active = True
                samples, chunk_text, chunk_index, on_chunk_start, turn_id = item

                if self._active_turn_id != -1 and turn_id != self._active_turn_id:
                    self._currently_playing = False
                    self._playback_active = False
                    self._audio_queue.task_done()
                    continue

                if on_chunk_start and chunk_text:
                    on_chunk_start(chunk_text)

                event_bus.emit_event(EventType.TTS_PLAYBACK_START, {"word_index": chunk_index})

                # Write blocks in smaller chunks to allow quick cancellation
                chunk_size = sample_rate // 10  # 100ms chunks
                for i in range(0, len(samples), chunk_size):
                    if self._interrupt_event.is_set() or (self._active_turn_id != -1 and turn_id != self._active_turn_id):
                        break
                    block = samples[i:i+chunk_size]
                    # Report what is being played so barge-in can tell SAINT's
                    # own voice (echo) apart from the user.
                    playback_monitor.note_block(float(np.sqrt(np.mean(np.square(block)))) if len(block) else 0.0,
                                                len(block) / sample_rate)
                    stream.write(block)

                event_bus.emit_event(EventType.TTS_PLAYBACK_END, {"word_index": chunk_index})
                self._audio_queue.task_done()
        finally:
            self._currently_playing = False
            self._playback_active = False
            stream.stop()
            stream.close()

    def _load(self):
        """Lazy-load the KPipeline (downloads model + voices on first call).

        Device selection goes through core.device.resolve_torch_device — the
        single, shared source of truth — so Kokoro no longer re-implements its
        own ``torch.cuda.is_available()`` logic. Behaviour:

          * CUDA available            -> run on CUDA.
          * CUDA unavailable + fallback allowed
                                       -> run on CPU (real Kokoro, not a mock),
                                          logging the *actual* reason.
          * CUDA required/unavailable -> raise a descriptive error that explains
                                          the diagnosis and how to fix it.

        The historical sm_120 "no kernel image" runtime failure (CUDA present
        but no compiled kernels for the GPU arch) is still caught and degraded
        to CPU when fallback is allowed.
        """
        if self._pipeline is not None:
            return
        with self._load_lock:
            if self._pipeline is not None:
                return
            if self._load_attempted and self._load_error is not None:
                raise self._load_error
            self._load_attempted = True
            try:
                from kokoro import KPipeline
                import os
                import torch
                import logging
                from core.device import resolve_torch_device
                from core.events import event_bus, EventType

                logger = logging.getLogger("saint.tts")

                # Set CPU threads based on available cores
                num_threads = min(4, os.cpu_count() or 4)
                torch.set_num_threads(num_threads)

                # --- single, shared device resolution ---------------------
                res = resolve_torch_device(
                    self._device,
                    allow_cpu_fallback=self._allow_cpu_fallback,
                    require_cuda=self._require_cuda,
                )
                self._device_resolution = res

                if not res.usable:
                    # CUDA required (or fallback disabled) but unavailable.
                    hint = f" {res.fix_hint}" if res.fix_hint else ""
                    raise RuntimeError(f"{res.reason}.{hint}")

                if res.fell_back:
                    logger.warning(
                        "Kokoro TTS: %s. Falling back to CPU (real TTS, reduced "
                        "throughput).%s",
                        res.reason,
                        f" {res.fix_hint}" if res.fix_hint else "",
                    )
                    event_bus.emit_event(EventType.TTS_FALLBACK, {
                        "engine": "kokoro",
                        "requested": res.requested,
                        "device": res.device,
                        "reason": res.reason,
                    })
                else:
                    logger.info("Kokoro TTS: %s", res.reason)

                device = res.device
                try:
                    self._pipeline = KPipeline(lang_code="a", device=device)
                except RuntimeError as e:
                    # CUDA present but no compiled kernels for this GPU arch
                    # (e.g. sm_120 / Blackwell on an older torch build).
                    if ("no kernel image is available" in str(e)
                            and str(device).startswith("cuda")
                            and self._allow_cpu_fallback):
                        logger.warning(
                            "Kokoro TTS: CUDA device present but no compiled "
                            "kernels for this GPU (%s). Falling back to CPU. %s",
                            e, _CUDA_ARCH_HINT,
                        )
                        device = "cpu"
                        self._pipeline = KPipeline(lang_code="a", device=device)
                        event_bus.emit_event(EventType.TTS_FALLBACK, {
                            "engine": "kokoro",
                            "requested": res.requested,
                            "device": "cpu",
                            "reason": f"no compiled CUDA kernels for GPU: {e}",
                        })
                    else:
                        raise

                # Enable a phoneme fallback for out-of-dictionary words.
                # Without it, misaki returns None phonemes for OOV words (e.g.
                # "Lua", "Kokoro") and Kokoro crashes with
                # "unsupported operand type(s) for +: 'NoneType' and 'str'",
                # silently dropping the whole sentence.
                self._enable_g2p_fallback(logger)

                self._resolved_device = device
                logger.info("Kokoro TTS pipeline ready on device=%s", device)
                event_bus.emit_event(EventType.TTS_DEVICE_INFO, {
                    "engine": "kokoro",
                    "requested_device": res.requested,
                    "resolved_device": device,
                    "cuda_available": res.cuda_available,
                    "fell_back": res.fell_back or device != res.device,
                })
            except Exception as e:
                self._load_error = RuntimeError(f"Could not load Kokoro TTS pipeline: {e}")
                raise self._load_error from e

    def _enable_g2p_fallback(self, logger):
        """Give misaki an espeak-ng phoneme fallback for OOV words.

        Uses the bundled ``espeakng_loader`` package (no system install needed).
        If anything is missing we log a clear warning — SAINT still runs, but
        out-of-dictionary words may fail — rather than crashing at load time.
        """
        if getattr(self, "_g2p_fallback_ready", False):
            return
        try:
            g2p = getattr(self._pipeline, "g2p", None)
            if g2p is None or not hasattr(g2p, "fallback"):
                return
            if getattr(g2p, "fallback", None) is not None:
                self._g2p_fallback_ready = True
                return
            import espeakng_loader
            from phonemizer.backend.espeak.wrapper import EspeakWrapper
            EspeakWrapper.set_library(espeakng_loader.get_library_path())
            try:
                EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
            except Exception:
                pass
            from misaki import espeak as _misaki_espeak
            g2p.fallback = _misaki_espeak.EspeakFallback(british=False)
            self._g2p_fallback_ready = True
            logger.info("Kokoro TTS: espeak G2P fallback enabled for OOV words")
        except Exception as e:
            logger.warning(
                "Kokoro TTS: could not enable espeak G2P fallback (%s). "
                "Out-of-dictionary words may fail to synthesize; install "
                "'espeakng_loader' to fix.", e,
            )

    @property
    def device_info(self) -> dict:
        """Resolved device diagnostics for UI/logging."""
        res = self._device_resolution
        return {
            "requested_device": self._device,
            "resolved_device": self._resolved_device,
            "cuda_available": bool(getattr(res, "cuda_available", False)),
            "fell_back": bool(getattr(res, "fell_back", False))
            or (self._resolved_device is not None
                and self._resolved_device != getattr(res, "device", self._resolved_device)),
            "reason": getattr(res, "reason", ""),
        }

    def warm_up(self):
        """Pre-warm CUDA kernels with a short generation (no playback)."""
        self._load()
        try:
            import torch
            for _, _, audio in self._pipeline("Warming up.", voice=self._voice, speed=self._speed):
                pass
            if str(self._resolved_device or "").startswith("cuda"):
                torch.cuda.synchronize()
        except Exception:
            pass

    def speak(self, text: str, turn_id: int = 0, on_chunk_start: Optional[Callable[[str], None]] = None):
        """Synthesise full text via the Kokoro pipeline and stream audio chunks.

        The pipeline handles G2P and optimal text chunking internally (sentence
        boundaries, 510-phoneme model limit).  Each yielded Result contains a
        sentence-level audio tensor that we play while the next chunk is being
        generated — giving ~30-40x realtime throughput on a modern GPU.
        """
        import sounddevice as sd
        import torch
        import time
        import logging

        t_start = time.perf_counter()
        sample_rate = 24000

        from core.events import event_bus, EventType

        self._load()
        self._interrupt_event.clear()
        self._active_turn_id = turn_id

        with self._lock:
            self._speaking = True

        event_bus.emit_event(EventType.TTS_SPEAK_START, {
            "text": text,
            "word_count": len(text.split()),
        })

        error: Optional[Exception] = None
        try:
            # Single pipeline call for the full text — the generator yields
            # sentence-level Result objects with pre-synthesised audio.
            t_infer_start = time.perf_counter()
            generator = self._pipeline(
                text,
                voice=self._voice,
                speed=self._speed,
            )

            for i, result in enumerate(generator):
                if self._interrupt_event.is_set():
                    break

                if result.audio is None:
                    continue

                samples = result.audio.cpu().numpy()
                if len(samples) == 0:
                    continue

                t_infer_end = time.perf_counter()
                inference_ms = (t_infer_end - t_infer_start) * 1000
                logging.info(f"tts.timing inference chunk {i}: {inference_ms:.1f}ms")

                event_bus.emit_event(EventType.TTS_INFERENCE_END, {
                    "word_index": i,
                    "inference_ms": round(inference_ms, 1),
                    "sample_rate": sample_rate,
                    "samples": len(samples),
                    "duration_ms": round(len(samples) / sample_rate * 1000, 1),
                })

                if self._interrupt_event.is_set():
                    break

                event_bus.emit_event(EventType.TTS_AUDIO_READY, {
                    "word_index": i,
                    "samples": len(samples),
                    "sample_rate": sample_rate,
                    "channels": 1,
                    "duration_ms": round(len(samples) / sample_rate * 1000, 1),
                })

                # Queue the audio for the background playback thread
                chunk_text = result.graphemes or ""
                self._audio_queue.put((samples, chunk_text, i, on_chunk_start, turn_id))

                # Reset inference timer for next chunk
                t_infer_start = time.perf_counter()
                
            # We return immediately after inference so the next sentence can
            # start generating while this one plays from the queue.
        except Exception as e:
            # A synthesis failure (e.g. a phonemization error on an OOV word)
            # must be reported as an error and MUST NOT be logged as
            # tts.speak.done, or the caller will think the sentence was spoken.
            error = e
            logging.error(f"tts.speak.error turn_id={turn_id}: {e}")
            event_bus.emit_event(EventType.TTS_SPEAK_ERROR, {
                "error": str(e),
                "turn_id": turn_id,
                "text_preview": text[:100],
            })
            event_bus.emit_event(EventType.TTS_ERROR, {
                "error": str(e),
                "turn_id": turn_id,
                "phase": "synthesis",
            })
        finally:
            with self._lock:
                self._speaking = False

            t_total_end = time.perf_counter()
            logging.info(f"tts.timing total {(t_total_end - t_start) * 1000:.1f}ms")
            # Only a genuinely completed (or cleanly interrupted) synthesis
            # counts as "done". A failed one already emitted tts.speak.error.
            if error is None:
                event_bus.emit_event(EventType.TTS_SPEAK_DONE, {
                    "text": text,
                })

        # True on success/clean-interrupt, False on synthesis error, so the
        # caller (TTS worker) can retry or report a lost sentence.
        return error is None

    def interrupt(self, turn_id: int = None):
        with self._lock:
            if turn_id is not None and self._active_turn_id != -1 and turn_id != self._active_turn_id:
                return
            self._active_turn_id = -1
        self._interrupt_event.set()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass

    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking or not self._audio_queue.empty() or self._currently_playing


# ---------------------------------------------------------------------------
# Qwen TTS (Qwen3-TTS via HuggingFace transformers)
# ---------------------------------------------------------------------------
class QwenTTS(TTSEngine):
    """
    TTS using Qwen3-TTS via HuggingFace transformers.

    Supports three model types:
      - CustomVoice: predefined speakers with optional style instructions
      - VoiceDesign: natural language voice style instructions
      - Base: voice cloning from reference audio

    Uses word-level streaming similar to KokoroTTS for low latency.
    """

    def __init__(
        self,
        model_name: str = "Qwen/Qwen3-TTS",
        model_type: str = "custom_voice",  # "custom_voice" | "voice_design" | "base"
        speaker: str = "eric",  # for CustomVoice (validated at load time)
        language: str = "Auto",
        device: str = "cuda",  # Ensure CUDA is available,
        dtype: str = "bfloat16",
        voice_clone_audio: Optional[str] = None,  # path to reference audio for Base model
        voice_clone_text: Optional[str] = None,  # reference text for ICL mode
        x_vector_only: bool = False,  # for Base model: True = speaker embedding only
        instruct: Optional[str] = None,  # for CustomVoice/VoiceDesign: style instruction
        speed: float = 1.0,
        flash_attention: str = "Auto",  # "Auto" | "Enabled" | "Disabled"
        allow_cpu_fallback: bool = True,  # degrade to CPU if CUDA unusable
        require_cuda: bool = False,      # hard-fail (with diagnosis) if no CUDA
    ):
        self._model_name = model_name
        self._model_type = model_type
        self._speaker = speaker
        self._language = language
        self._device = device
        self._allow_cpu_fallback = allow_cpu_fallback
        self._require_cuda = require_cuda
        self._resolved_device: Optional[str] = None
        self._device_resolution = None
        self._dtype = dtype
        self._voice_clone_audio = voice_clone_audio
        self._voice_clone_text = voice_clone_text
        self._x_vector_only = x_vector_only
        self._instruct = instruct
        self._speed = speed
        self._flash_attention = flash_attention

        self._model = None
        self._interrupt_event = threading.Event()
        self._speaking = False
        self._lock = threading.Lock()
        # Guards one-time model initialisation so concurrent callers (e.g. a
        # background warm-up racing the first streamed chunk) never trigger two
        # simultaneous loads of the same Qwen model.
        self._load_lock = threading.Lock()
        # Remember a prior load failure so we don't re-attempt the expensive
        # download/initialisation on every queued TTS chunk.
        self._load_error: Optional[Exception] = None
        self._load_attempted: bool = False
        self._load_failure_reported: bool = False
        self._voice_clone_prompt = None  # cached prompt for Base model

    def _load(self):
        # Fast path: already loaded.
        if self._model is not None:
            return
        # If a previous load attempt failed, don't re-attempt the expensive
        # download/weight init on every queued chunk — surface the cached error
        # quickly instead. This also prevents duplicate simultaneous init.
        if self._load_attempted and self._load_error is not None:
            raise self._load_error

        with self._load_lock:
            # Re-check inside the lock (double-checked locking).
            if self._model is not None:
                return
            self._load_attempted = True
            try:
                import torch
                import time
                import logging
                # pyrefly: ignore [missing-import]
                from qwen2_tts import Qwen3TTSModel

                # Map dtype string to torch dtype
                dtype_map = {
                    "float16": torch.float16,
                    "bfloat16": torch.bfloat16,
                    "float32": torch.float32,
                }
                torch_dtype = dtype_map.get(self._dtype, torch.bfloat16)

                # Device validation via the shared resolver (single source of
                # truth). If CUDA was requested but is unavailable (e.g. CPU-only
                # torch build), fall back to CPU with the actual reason logged
                # instead of hard-crashing. FlashAttention2 is never *required*.
                from core.device import resolve_torch_device
                from core.events import event_bus, EventType

                res = resolve_torch_device(
                    self._device,
                    allow_cpu_fallback=self._allow_cpu_fallback,
                    require_cuda=self._require_cuda,
                )
                self._device_resolution = res
                if not res.usable:
                    hint = f" {res.fix_hint}" if res.fix_hint else ""
                    raise RuntimeError(f"{res.reason}.{hint}")
                if res.fell_back:
                    logging.warning(
                        "QwenTTS: %s. Falling back to CPU for Qwen inference.%s",
                        res.reason, f" {res.fix_hint}" if res.fix_hint else "",
                    )
                    event_bus.emit_event(EventType.TTS_FALLBACK, {
                        "engine": "qwen",
                        "requested": res.requested,
                        "device": res.device,
                        "reason": res.reason,
                    })
                device = res.device
                self._resolved_device = device
                device_is_cuda = str(device).lower().startswith("cuda")

                # Resolve the attention backend. Defaults to SDPA (standard
                # PyTorch/TorchSDPA) unless flash_attn is installed and enabled.
                attn_impl = _resolve_attn_implementation(
                    self._flash_attention, device_is_cuda
                )

                # Load model
                self._model = Qwen3TTSModel.from_pretrained(
                    self._model_name,
                    device_map=device,
                    dtype=torch_dtype,
                    attn_implementation=attn_impl,
                )

                # Verify model type matches
                actual_type = self._model.model.tts_model_type
                if actual_type != self._model_type:
                    import logging
                    logging.warning(
                        f"QwenTTS: Model type mismatch. Config: {self._model_type}, "
                        f"Model: {actual_type}. Using model's type: {actual_type}"
                    )
                    self._model_type = actual_type

                # Pre-build voice clone prompt if using Base model with reference audio
                if self._model_type == "base" and self._voice_clone_audio:
                    self._voice_clone_prompt = self._model.create_voice_clone_prompt(
                        ref_audio=self._voice_clone_audio,
                        ref_text=self._voice_clone_text,
                        x_vector_only_mode=self._x_vector_only,
                    )

                # Validate the configured speaker against what the loaded
                # model actually supports (the config default may be invalid for
                # a given checkpoint, e.g. a Kokoro name). Fall back to the
                # first supported speaker so TTS keeps working.
                supported_speakers = None
                try:
                    supported_speakers = self._model.get_supported_speakers()
                except Exception:
                    supported_speakers = None
                if supported_speakers:
                    if not self._speaker or str(self._speaker).lower() not in supported_speakers:
                        import logging
                        fallback = supported_speakers[0]
                        logging.warning(
                            f"QwenTTS: speaker '{self._speaker}' is not supported by "
                            f"{self._model_name} (supported: {supported_speakers}). "
                            f"Falling back to speaker '{fallback}'."
                        )
                        self._speaker = fallback

                # Report the resolved backend so operators can confirm CUDA + SDPA.
                resolved_device = getattr(self._model, "device", device)
                resolved_attn = getattr(
                    getattr(self._model, "model", None).config,
                    "_attn_implementation",
                    attn_impl,
                )
                import logging
                logging.info(
                    f"QwenTTS model loaded: model={self._model_name}, "
                    f"device={resolved_device}, attention={resolved_attn}, "
                    f"flash_attention={self._flash_attention}, speaker={self._speaker}"
                )

            except Exception as e:
                self._load_error = RuntimeError(
                    f"Could not load Qwen TTS model: {e}"
                )
                raise self._load_error from e

    @property
    def device_info(self) -> dict:
        """Resolved device diagnostics for UI/logging."""
        res = self._device_resolution
        return {
            "requested_device": self._device,
            "resolved_device": self._resolved_device,
            "cuda_available": bool(getattr(res, "cuda_available", False)),
            "fell_back": bool(getattr(res, "fell_back", False)),
            "reason": getattr(res, "reason", ""),
        }

    def warm_up(self):
        self._load()
        try:
            self.speak(".", on_chunk_start=None)
        except Exception:
            pass

    def speak(self, text: str, turn_id: int = 0, on_chunk_start: Optional[Callable[[str], None]] = None):
        t_start = time.perf_counter()
        import sounddevice as sd  # type: ignore
        import torch
        import time
        import logging

        from core.events import event_bus, EventType

        # Lazy, one-time model init. If it failed previously we do not retry
        # the expensive load on every streamed chunk — emit a single error and
        # let the (already-rendered) LLM response continue uninterrupted.
        try:
            self._load()
        except Exception as e:
            if not self._load_failure_reported:
                self._load_failure_reported = True
                event_bus.emit_event(EventType.TTS_ERROR, {
                    "error": f"Qwen TTS model failed to load: {e}",
                })
            t_total_end = time.perf_counter()
            logging.info(f"tts.timing total { (t_total_end - t_start) * 1000:.1f}ms")
            event_bus.emit_event(EventType.TTS_SPEAK_DONE, {"text": text})
            return

        self._interrupt_event.clear()

        words = _split_words(text)
        t_phoneme = time.perf_counter()
        logging.info(f"tts.timing phonemization { (t_phoneme - t_start) * 1000:.1f}ms")
        with self._lock:
            self._speaking = True

        event_bus.emit_event(EventType.TTS_SPEAK_START, {
            "text": text,
            "word_count": len(words),
        })

        try:
            for i, word in enumerate(words):
                if self._interrupt_event.is_set():
                    break
                if not word.strip():
                    continue

                if on_chunk_start:
                    on_chunk_start(word)

                event_bus.emit_event(EventType.TTS_INFERENCE_START, {
                    "word_index": i,
                    "word": word,
                })

                t_inference_start = time.perf_counter()

                # Generate audio for this word
                wavs, sample_rate = self._generate_word(word)

                t_inference_end = time.perf_counter()
                inference_ms = (t_inference_end - t_inference_start) * 1000

                # Convert to numpy array (first channel if stereo)
                samples = wavs[0]
                if samples.ndim > 1:
                    samples = samples[0]  # take first channel

                # Convert float32 [-1, 1] to int16 for sounddevice
                samples_int16 = (samples * 32767).astype(np.int16)

                event_bus.emit_event(EventType.TTS_INFERENCE_END, {
                    "word_index": i,
                    "inference_ms": round(inference_ms, 1),
                    "sample_rate": sample_rate,
                    "samples": len(samples_int16),
                    "duration_ms": round(len(samples_int16) / sample_rate * 1000, 1) if sample_rate > 0 else 0,
                })

                if self._interrupt_event.is_set():
                    break

                event_bus.emit_event(EventType.TTS_AUDIO_READY, {
                    "word_index": i,
                    "samples": len(samples_int16),
                    "sample_rate": sample_rate,
                    "channels": 1,
                    "duration_ms": round(len(samples_int16) / sample_rate * 1000, 1) if sample_rate > 0 else 0,
                })

                # Play audio
                event_bus.emit_event(EventType.TTS_PLAYBACK_START, {
                    "word_index": i,
                })
                t_playback_start = time.perf_counter()
                t_play_start = time.perf_counter()
                duration = len(samples_int16) / sample_rate if sample_rate > 0 else 0
                playback_monitor.note_block(float(np.sqrt(np.mean(np.square(samples)))) if len(samples) else 0.0,
                                            duration)
                sd.play(samples_int16, sample_rate)
                end_time = time.perf_counter() + duration
                while time.perf_counter() < end_time:
                    if self._interrupt_event.is_set():
                        sd.stop()
                        break
                    time.sleep(0.01)
                t_playback_end = time.perf_counter()
                logging.info(f"tts.timing playback { (t_playback_end - t_play_start) * 1000:.1f}ms")
                playback_ms = (t_playback_end - t_playback_start) * 1000
                event_bus.emit_event(EventType.TTS_PLAYBACK_END, {
                    "word_index": i,
                    "playback_ms": round(playback_ms, 1),
                })
        finally:
            with self._lock:
                self._speaking = False

            t_total_end = time.perf_counter()
            logging.info(f"tts.timing total { (t_total_end - t_start) * 1000:.1f}ms")
            event_bus.emit_event(EventType.TTS_SPEAK_DONE, {
                "text": text,
            })

    def _generate_word(self, text: str) -> Tuple[List[np.ndarray], int]:
        """Generate audio for a single word/text segment."""
        import torch
        import time
        import logging

        # Prepare generation kwargs
        gen_kwargs = {
            "do_sample": True,
            "top_k": 50,
            "top_p": 1.0,
            "temperature": 0.9,
            "repetition_penalty": 1.05,
            "max_new_tokens": 2048,
        }

        if self._model_type == "custom_voice":
            wavs, sr = self._model.generate_custom_voice(
                text=text,
                speaker=self._speaker,
                language=self._language,
                instruct=self._instruct,
                non_streaming_mode=True,
                **gen_kwargs,
            )
        elif self._model_type == "voice_design":
            wavs, sr = self._model.generate_voice_design(
                text=text,
                instruct=self._instruct or "",
                language=self._language,
                non_streaming_mode=True,
                **gen_kwargs,
            )
        elif self._model_type == "base":
            if self._voice_clone_prompt is None:
                if not self._voice_clone_audio:
                    raise ValueError("Base model requires voice_clone_audio to be set")
                self._voice_clone_prompt = self._model.create_voice_clone_prompt(
                    ref_audio=self._voice_clone_audio,
                    ref_text=self._voice_clone_text,
                    x_vector_only_mode=self._x_vector_only,
                )

            wavs, sr = self._model.generate_voice_clone(
                text=text,
                language=self._language,
                voice_clone_prompt=self._voice_clone_prompt,
                non_streaming_mode=True,
                **gen_kwargs,
            )
        else:
            raise ValueError(f"Unknown Qwen TTS model type: {self._model_type}")

        return wavs, sr

    def interrupt(self, turn_id: int = None):
        self._interrupt_event.set()
        try:
            import sounddevice as sd  # type: ignore
            sd.stop()
        except Exception:
            pass

    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking


# ---------------------------------------------------------------------------
# Mock TTS (for tests — no audio hardware, no model files)
# ---------------------------------------------------------------------------
class MockTTS(TTSEngine):
    """
    Simulates TTS output at a fixed words-per-second rate.
    Emits word-by-word callbacks; no audio produced.
    """

    def __init__(self, words_per_second: float = 10.0):
        self._wps = words_per_second
        self._interrupt_event = threading.Event()
        self._speaking = False
        self._spoken_chunks = []     # record of what was "spoken"

    def speak(self, text: str, turn_id: int = 0, on_chunk_start: Optional[Callable[[str], None]] = None):
        t_start = time.perf_counter()
        from core.events import event_bus, EventType
        import logging

        self._interrupt_event.clear()
        self._speaking = True
        self._spoken_chunks.append(text)

        words = _split_words(text)
        t_phoneme = time.perf_counter()
        logging.info(f"tts.timing phonemization { (t_phoneme - t_start) * 1000:.1f}ms")

        event_bus.emit_event(EventType.TTS_SPEAK_START, {
            "text": text,
            "word_count": len(words),
        })

        try:
            for i, word in enumerate(words):
                if self._interrupt_event.is_set():
                    break

                if on_chunk_start:
                    on_chunk_start(word)

                event_bus.emit_event(EventType.TTS_INFERENCE_START, {
                    "word_index": i,
                    "word": word,
                })

                # Simulate inference time per word
                time.sleep(0.01)

                event_bus.emit_event(EventType.TTS_INFERENCE_END, {
                    "word_index": i,
                    "inference_ms": 10.0,
                    "sample_rate": 24000,
                    "samples": len(word) * 100,
                    "duration_ms": len(word) * 10,
                })

                event_bus.emit_event(EventType.TTS_AUDIO_READY, {
                    "word_index": i,
                    "samples": len(word) * 100,
                    "sample_rate": 24000,
                    "channels": 1,
                    "duration_ms": len(word) * 10,
                })

                event_bus.emit_event(EventType.TTS_PLAYBACK_START, {
                    "word_index": i,
                })

                # Simulate playback time (paced per word)
                playback_monitor.note_block(0.1, 1.0 / self._wps)
                time.sleep(1.0 / self._wps)

                event_bus.emit_event(EventType.TTS_PLAYBACK_END, {
                    "word_index": i,
                    "playback_ms": round(1000.0 / self._wps, 1),
                })
        finally:
            self._speaking = False

            t_total_end = time.perf_counter()
            logging.info(f"tts.timing total { (t_total_end - t_start) * 1000:.1f}ms")
            event_bus.emit_event(EventType.TTS_SPEAK_DONE, {
                "text": text,
            })

    def interrupt(self, turn_id: int = None):
        self._interrupt_event.set()

    def is_speaking(self) -> bool:
        return self._speaking

    def get_spoken(self):
        return list(self._spoken_chunks)

    def clear_spoken(self):
        self._spoken_chunks.clear()


def make_tts(backend: str = "kokoro", **kwargs) -> TTSEngine:
    """Factory."""
    if backend == "kokoro":
        try:
            tts = KokoroTTS(**kwargs)
            import logging
            logging.info(f"TTS initialized: model=kokoro, device={kwargs.get('device', 'cuda')}, voice={kwargs.get('voice', 'af_heart')}")
            return tts
        except Exception as e:
            import logging
            logging.warning(f"Failed to initialize Kokoro TTS: {e}. Falling back to MockTTS.")
            return MockTTS()
    elif backend == "qwen":
        try:
            tts = QwenTTS(**kwargs)
            import logging
            logging.info(
                f"TTS initialized: model=qwen, type={kwargs.get('model_type', 'custom_voice')}, "
                f"device={kwargs.get('device', 'cuda')}, "
                f"flash_attention={kwargs.get('flash_attention', 'Auto')}"
            )
            return tts
        except Exception as e:
            import logging
            logging.warning(f"Failed to initialize Qwen TTS: {e}. Falling back to MockTTS.")
            return MockTTS()
    elif backend == "mock":
        import logging
        logging.info("TTS initialized: model=mock")
        return MockTTS()
    else:
        import logging
        logging.warning(f"Unknown TTS backend: {backend}, falling back to MockTTS")
        return MockTTS()

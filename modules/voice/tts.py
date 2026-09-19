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


# ---------------------------------------------------------------------------
# Word splitter
# ---------------------------------------------------------------------------
def _split_words(text: str):
    """Split text into individual words, preserving punctuation attached to words."""
    return text.strip().split()


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
        device: str = "cuda",  # Ensure CUDA is available,
    ):
        self._voice = voice
        self._speed = speed
        self._device = device
        self._pipeline = None
        self._interrupt_event = threading.Event()
        self._speaking = False
        self._active_turn_id: int = -1
        self._lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._load_error: Optional[Exception] = None
        self._load_attempted = False

    def _load(self):
        """Lazy-load the KPipeline (downloads model + voices on first call)."""
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
                # Set CPU threads based on available cores
                num_threads = min(4, os.cpu_count() or 4)
                torch.set_num_threads(num_threads)
                
                # Try CUDA first, fall back to CPU if cuDNN kernels not available
                device = self._device
                try:
                    self._pipeline = KPipeline(
                        lang_code="en-us",
                        device=device,
                    )
                except RuntimeError as e:
                    if "no kernel image is available" in str(e) and device == "cuda":
                        import logging
                        logging.warning(f"CUDA not fully supported on this GPU (sm_120), falling back to CPU for Kokoro TTS")
                        device = "cpu"
                        self._pipeline = KPipeline(
                            lang_code="en-us",
                            device=device,
                        )
                    else:
                        raise
            except Exception as e:
                self._load_error = RuntimeError(f"Could not load Kokoro TTS pipeline: {e}")
                raise self._load_error from e

    def warm_up(self):
        self._load()
        try:
            self.speak(".", on_chunk_start=None)
        except Exception:
            pass

    def speak(self, text: str, turn_id: int = 0, on_chunk_start: Optional[Callable[[str], None]] = None):
        import sounddevice as sd
        import torch
        import time
        import logging

        t_start = time.perf_counter()

        from core.events import event_bus, EventType

        self._load()
        self._interrupt_event.clear()
        self._active_turn_id = turn_id

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

                # Use a quiet pipeline so we don't get the repo-id warning on
                # every call; pass model=True to auto-load once, then reuse.
                t_infer_start = time.perf_counter()
                generator = self._pipeline(
                    word,
                    voice=self._voice,
                    speed=self._speed,
                    model=self._pipeline.model,
                )
                with torch.inference_mode():
                    result = next(generator, None)
                samples = result.audio.cpu().numpy() if result is not None and result.audio is not None else np.array([], dtype=np.float32)
                t_infer_end = time.perf_counter()
                logging.info(f"tts.timing inference { (t_infer_end - t_infer_start) * 1000:.1f}ms")
                sample_rate = 24000

                t_inference_end = time.perf_counter()
                inference_ms = (t_inference_end - t_inference_start) * 1000

                event_bus.emit_event(EventType.TTS_INFERENCE_END, {
                    "word_index": i,
                    "inference_ms": round(inference_ms, 1),
                    "sample_rate": sample_rate,
                    "samples": len(samples),
                    "duration_ms": round(len(samples) / sample_rate * 1000, 1) if sample_rate > 0 else 0,
                })

                if self._interrupt_event.is_set():
                    break

                event_bus.emit_event(EventType.TTS_AUDIO_READY, {
                    "word_index": i,
                    "samples": len(samples),
                    "sample_rate": sample_rate,
                    "channels": 1,
                    "duration_ms": round(len(samples) / sample_rate * 1000, 1) if sample_rate > 0 else 0,
                })

                # Play audio — sd.play is non-blocking; sd.wait blocks here
                event_bus.emit_event(EventType.TTS_PLAYBACK_START, {
                    "word_index": i,
                })
                t_playback_start = time.perf_counter()
                t_play_start = time.perf_counter()
                sd.play(samples, sample_rate)
                duration = len(samples) / sample_rate if sample_rate > 0 else 0
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
            return self._speaking


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
    ):
        self._model_name = model_name
        self._model_type = model_type
        self._speaker = speaker
        self._language = language
        self._device = device
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

                # Device validation: if CUDA was requested but is unavailable
                # (e.g. CPU-only torch build), fall back to CPU instead of
                # hard-crashing. FlashAttention2 is never *required*.
                device = self._device
                device_is_cuda = str(device).lower().startswith("cuda")
                if device_is_cuda and not torch.cuda.is_available():
                    import logging
                    logging.warning(
                        f"QwenTTS: CUDA requested (device={device}) but torch.cuda is not "
                        "available. Falling back to CPU for Qwen inference."
                    )
                    device = "cpu"
                    device_is_cuda = False

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
                sd.play(samples_int16, sample_rate)
                duration = len(samples_int16) / sample_rate if sample_rate > 0 else 0
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

"""
modules/vision/flux.py

FLUX.2 Klein 4B local vision backend.

Loaded on demand. If the required packages (transformers, torch, safetensors)
or the model weights aren't present, `available()` reports what's missing so
Settings > Vision can display the real problem. No silent substitution: if
FLUX isn't ready, an analyze() call raises ToolError with a clear reason.

Install:
    pip install transformers accelerate safetensors pillow
    hf download black-forest-labs/FLUX.2-Klein-4B --local-dir data/vision/flux2-klein-4b
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional, Tuple

from core.config import config
from core.paths import resolve_project_path
from modules.automation.tools import ToolError

log = logging.getLogger("saint.vision.flux")


class FluxAnalyzer:
    """Lazy-loaded FLUX.2 Klein 4B vision-language pipeline."""

    def __init__(self):
        self._lock = threading.Lock()
        self._model = None
        self._processor = None
        self._device = ""
        self._loaded_from = ""
        self._load_error = ""

    # ------------------------------------------------------------------ #
    # Availability
    # ------------------------------------------------------------------ #
    def available(self) -> Tuple[bool, str]:
        try:
            import transformers                                             # noqa: F401
        except Exception as e:
            return False, (f"FLUX.2 Klein needs the `transformers` package ({e}). "
                           "Install it with `pip install transformers accelerate safetensors`.")
        try:
            import torch                                                    # noqa: F401
        except Exception as e:
            return False, f"FLUX.2 Klein needs PyTorch ({e})."
        path = self._model_path()
        if not path or not path.exists():
            return False, (f"FLUX.2 Klein weights not found at {path}. Download with "
                           "`hf download black-forest-labs/FLUX.2-Klein-4B "
                           f"--local-dir {config.get('vision.flux_model_dir', '')}`.")
        # Weight file presence check (allow either safetensors shards or bin).
        has_weights = any(path.rglob("*.safetensors")) or any(path.rglob("*.bin"))
        if not has_weights:
            return False, (f"FLUX.2 Klein directory {path} exists but has no model weights. "
                           "Re-run the `hf download` above.")
        if self._load_error and self._model is None:
            return False, self._load_error
        return True, ""

    def _model_path(self) -> Optional[Path]:
        d = config.get("vision.flux_model_dir", "")
        if not d:
            return None
        return Path(resolve_project_path(d))

    # ------------------------------------------------------------------ #
    # Load & run
    # ------------------------------------------------------------------ #
    def _resolve_device(self) -> str:
        want = (config.get("vision.flux_device", "auto") or "auto").lower()
        if want in ("cuda", "cpu"):
            return want
        try:
            import torch
            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"

    def _resolve_dtype(self):
        import torch
        return {
            "float16": torch.float16,
            "bfloat16": torch.bfloat16,
            "float32": torch.float32,
        }.get((config.get("vision.flux_dtype", "float16") or "float16").lower(), torch.float16)

    def _ensure_loaded(self):
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            ok, reason = self.available()
            if not ok:
                raise ToolError(reason, "VISION_UNAVAILABLE")
            try:
                from transformers import AutoProcessor, AutoModelForVision2Seq
                import torch
                path = str(self._model_path())
                device = self._resolve_device()
                dtype = self._resolve_dtype() if device == "cuda" else None
                log.info("flux.load path=%s device=%s dtype=%s", path, device, dtype)
                self._processor = AutoProcessor.from_pretrained(path, trust_remote_code=True)
                self._model = AutoModelForVision2Seq.from_pretrained(
                    path,
                    torch_dtype=dtype,
                    trust_remote_code=True,
                    device_map="auto" if device == "cuda" else None,
                )
                if device == "cpu":
                    self._model = self._model.to("cpu")
                self._model.eval()
                self._device = device
                self._loaded_from = path
            except Exception as e:
                self._load_error = f"FLUX.2 Klein failed to load: {e}"
                self._model = None
                self._processor = None
                log.exception("flux.load_failed")
                raise ToolError(self._load_error, "VISION_LOAD_FAILED") from e

    def analyze(self, image, question: str, system: str = "") -> str:
        self._ensure_loaded()
        try:
            import torch
            img = image.convert("RGB") if image.mode != "RGB" else image.copy()
            max_side = int(config.get("vision.flux_max_image_side", 1024))
            img.thumbnail((max_side, max_side))
            prompt = (system + "\n\n" if system else "") + question
            inputs = self._processor(images=img, text=prompt, return_tensors="pt")
            if self._device == "cuda":
                inputs = {k: v.to("cuda") for k, v in inputs.items()}
            with torch.no_grad():
                out = self._model.generate(
                    **inputs,
                    max_new_tokens=int(config.get("vision.flux_max_new_tokens", 160)),
                    do_sample=False,
                )
            text = self._processor.batch_decode(out, skip_special_tokens=True)[0]
            # Strip the echoed prompt if the processor didn't already.
            if text.startswith(prompt):
                text = text[len(prompt):]
            return text.strip()
        except ToolError:
            raise
        except Exception as e:
            log.exception("flux.analyze_failed")
            raise ToolError(f"FLUX.2 Klein failed while analysing the screen: {e}", "VISION_FAILED") from e

"""
core/device.py

Single source of truth for compute-device (CPU/CUDA) selection across SAINT.

Historically each subsystem decided independently whether CUDA was available,
using slightly different logic:

  * faster-whisper (STT) uses CTranslate2, which ships its OWN bundled CUDA
    runtime and therefore can see the GPU even when PyTorch cannot.
  * Kokoro TTS and Qwen TTS run through PyTorch, so they depend on the installed
    torch build having CUDA support.

That mismatch produced confusing situations such as STT running happily on the
GPU while Kokoro TTS reported the opaque error "CUDA requested but not
available" — because the installed PyTorch was a CPU-only wheel
(``torch==X+cpu``, ``torch.version.cuda is None``).

This module centralises *torch-based* device selection and exposes rich, safe
diagnostics so CUDA problems are easy to understand and fix instead of being
hidden behind a generic message or silently swallowed.

Nothing here logs secrets or sensitive configuration — only library versions,
CUDA availability, and GPU model/name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("saint.device")


# ---------------------------------------------------------------------------
# Raw diagnostics
# ---------------------------------------------------------------------------
def torch_cuda_report() -> Dict[str, Any]:
    """Collect a safe snapshot of the torch/CUDA environment.

    Never raises: any probing failure is captured in the returned dict so
    callers can log a complete picture even on a broken install.
    """
    report: Dict[str, Any] = {
        "torch_installed": False,
        "torch_version": None,
        "torch_cuda_version": None,      # CUDA toolkit torch was built against
        "cuda_available": False,
        "device_count": 0,
        "gpu_names": [],
        "cudnn_available": None,
        "probe_error": None,
    }
    try:
        import torch  # noqa: WPS433 (local import is intentional)

        report["torch_installed"] = True
        report["torch_version"] = getattr(torch, "__version__", None)
        report["torch_cuda_version"] = getattr(getattr(torch, "version", None), "cuda", None)

        try:
            report["cuda_available"] = bool(torch.cuda.is_available())
        except Exception as exc:  # pragma: no cover - defensive
            report["probe_error"] = f"torch.cuda.is_available() raised: {exc}"

        if report["cuda_available"]:
            try:
                report["device_count"] = int(torch.cuda.device_count())
                report["gpu_names"] = [
                    torch.cuda.get_device_name(i) for i in range(report["device_count"])
                ]
            except Exception as exc:  # pragma: no cover - defensive
                report["probe_error"] = f"device enumeration raised: {exc}"
            try:
                report["cudnn_available"] = bool(torch.backends.cudnn.is_available())
            except Exception:
                report["cudnn_available"] = None
    except Exception as exc:  # torch not importable
        report["probe_error"] = f"import torch failed: {exc}"

    return report


def ctranslate2_cuda_device_count() -> Optional[int]:
    """Return the CUDA device count seen by CTranslate2 (the STT backend).

    CTranslate2 bundles its own CUDA runtime, so this can differ from torch.
    Returns ``None`` if CTranslate2 is not installed / cannot be probed.
    """
    try:
        import ctranslate2  # noqa: WPS433

        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
@dataclass
class DeviceResolution:
    """Outcome of resolving a requested device against reality."""

    requested: str
    device: str                    # the device the caller should actually use
    cuda_available: bool
    usable: bool                   # False => caller must fail (see reason)
    fell_back: bool = False        # True => requested CUDA, resolved to CPU
    require_failed: bool = False    # True => CUDA required but unavailable
    reason: str = ""               # human-readable explanation
    fix_hint: str = ""             # actionable remediation (when relevant)
    report: Dict[str, Any] = field(default_factory=dict)

    def summary_line(self) -> str:
        gpu = ", ".join(self.report.get("gpu_names") or []) or "n/a"
        if self.device.startswith("cuda") and self.cuda_available:
            return f"TTS device: CUDA | GPU: {gpu} | CUDA available: True"
        if self.fell_back:
            return f"TTS device: CPU | Reason: {self.reason}"
        return f"TTS device: {self.device.upper()} | {self.reason}"


_CUDA_FIX_HINT = (
    "To enable GPU TTS, install a CUDA-enabled PyTorch build that matches your "
    "GPU. NVIDIA Blackwell cards (RTX 50-series, e.g. RTX 5060 Ti / sm_120) need "
    "the CUDA 12.8+ wheels: "
    "pip install --index-url https://download.pytorch.org/whl/cu128 torch"
)


def cuda_unavailable_reason(report: Dict[str, Any]) -> str:
    """Explain *why* torch cannot use CUDA, based on a torch_cuda_report()."""
    if not report.get("torch_installed"):
        return "PyTorch is not installed in this environment"
    if report.get("probe_error"):
        return f"probing CUDA failed: {report['probe_error']}"
    if report.get("torch_cuda_version") is None:
        ver = report.get("torch_version") or "unknown"
        return (
            f"the installed PyTorch is a CPU-only build (torch {ver}); it has no "
            "CUDA runtime, so torch.cuda.is_available() is False"
        )
    if not report.get("device_count"):
        return (
            "PyTorch has CUDA support but no CUDA devices are visible "
            "(check GPU drivers and the CUDA_VISIBLE_DEVICES environment variable)"
        )
    return "torch.cuda.is_available() returned False"


def resolve_torch_device(
    requested: str,
    *,
    allow_cpu_fallback: bool = True,
    require_cuda: bool = False,
    report: Optional[Dict[str, Any]] = None,
) -> DeviceResolution:
    """Resolve a requested torch device against the real environment.

    Args:
        requested: "cuda", "cuda:N", "cpu", or "auto".
        allow_cpu_fallback: if True, a CUDA request that cannot be satisfied
            degrades to CPU instead of failing.
        require_cuda: if True, CUDA is mandatory — an unsatisfiable CUDA request
            fails (``usable=False``) with a diagnostic reason instead of falling
            back. Takes precedence over ``allow_cpu_fallback``.
        report: optional pre-computed torch_cuda_report() (avoids re-probing).

    This is the ONE place torch device selection is decided. Callers should not
    re-implement ``torch.cuda.is_available()`` checks.
    """
    report = report or torch_cuda_report()
    cuda_available = bool(report.get("cuda_available"))
    req = (requested or "cuda").strip().lower()

    # "auto": prefer CUDA when present, else CPU — never fails.
    if req == "auto":
        if cuda_available:
            gpu = ", ".join(report.get("gpu_names") or []) or "CUDA device"
            return DeviceResolution(
                requested=req, device="cuda", cuda_available=True, usable=True,
                reason=f"auto-selected CUDA ({gpu})", report=report,
            )
        return DeviceResolution(
            requested=req, device="cpu", cuda_available=False, usable=True,
            reason=f"auto-selected CPU: {cuda_unavailable_reason(report)}",
            report=report,
        )

    # Explicit CPU request.
    if req.startswith("cpu"):
        return DeviceResolution(
            requested=req, device="cpu", cuda_available=cuda_available, usable=True,
            reason="CPU requested by configuration", report=report,
        )

    # Explicit CUDA request.
    if req.startswith("cuda"):
        if cuda_available:
            gpu = ", ".join(report.get("gpu_names") or []) or "CUDA device"
            return DeviceResolution(
                requested=req, device=req, cuda_available=True, usable=True,
                reason=f"CUDA available ({gpu})", report=report,
            )
        # CUDA requested but unavailable.
        reason = cuda_unavailable_reason(report)
        if require_cuda:
            return DeviceResolution(
                requested=req, device=req, cuda_available=False, usable=False,
                require_failed=True,
                reason=f"CUDA is required but unavailable: {reason}",
                fix_hint=_CUDA_FIX_HINT, report=report,
            )
        if allow_cpu_fallback:
            return DeviceResolution(
                requested=req, device="cpu", cuda_available=False, usable=True,
                fell_back=True,
                reason=f"CUDA unavailable: {reason}",
                fix_hint=_CUDA_FIX_HINT, report=report,
            )
        return DeviceResolution(
            requested=req, device=req, cuda_available=False, usable=False,
            reason=f"CUDA unavailable and CPU fallback disabled: {reason}",
            fix_hint=_CUDA_FIX_HINT, report=report,
        )

    # Unknown device string — pass through, let the backend validate.
    return DeviceResolution(
        requested=req, device=req, cuda_available=cuda_available, usable=True,
        reason=f"using device string as-is: {req}", report=report,
    )


# ---------------------------------------------------------------------------
# Formatting / logging
# ---------------------------------------------------------------------------
def format_diagnostics(report: Optional[Dict[str, Any]] = None) -> List[str]:
    """Produce a list of human-readable diagnostic lines (no secrets)."""
    report = report or torch_cuda_report()
    ct2 = ctranslate2_cuda_device_count()
    lines = [
        f"PyTorch version    : {report.get('torch_version')}",
        f"PyTorch CUDA build : {report.get('torch_cuda_version')}",
        f"torch.cuda.is_available: {report.get('cuda_available')}",
        f"CUDA device count  : {report.get('device_count')}",
        f"GPU name(s)        : {', '.join(report.get('gpu_names') or []) or 'n/a'}",
        f"cuDNN available    : {report.get('cudnn_available')}",
        f"CTranslate2 CUDA devices (STT backend): {ct2 if ct2 is not None else 'n/a'}",
    ]
    if report.get("probe_error"):
        lines.append(f"probe error        : {report['probe_error']}")
    return lines


def log_diagnostics(target_logger: Optional[logging.Logger] = None,
                    report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Log the CUDA/torch diagnostics block and return the raw report."""
    target_logger = target_logger or logger
    report = report or torch_cuda_report()
    target_logger.info("---- SAINT device diagnostics ----")
    for line in format_diagnostics(report):
        target_logger.info(line)
    target_logger.info("----------------------------------")
    return report

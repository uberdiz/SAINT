"""
tests/test_device.py

Unit tests for core.device — the single source of truth for torch device
selection. These tests inject synthetic torch_cuda_report() dicts so they are
fully deterministic and require neither a GPU nor a CUDA-enabled torch.
"""

from core.device import (
    resolve_torch_device,
    cuda_unavailable_reason,
    torch_cuda_report,
    format_diagnostics,
)


# Synthetic environment snapshots ------------------------------------------------
CUDA_OK = {
    "torch_installed": True,
    "torch_version": "2.4.0+cu128",
    "torch_cuda_version": "12.8",
    "cuda_available": True,
    "device_count": 1,
    "gpu_names": ["NVIDIA GeForce RTX 5060 Ti"],
    "cudnn_available": True,
    "probe_error": None,
}

CPU_ONLY_BUILD = {
    "torch_installed": True,
    "torch_version": "2.14.0+cpu",
    "torch_cuda_version": None,
    "cuda_available": False,
    "device_count": 0,
    "gpu_names": [],
    "cudnn_available": None,
    "probe_error": None,
}

CUDA_BUILD_NO_DEVICE = {
    "torch_installed": True,
    "torch_version": "2.4.0+cu128",
    "torch_cuda_version": "12.8",
    "cuda_available": False,
    "device_count": 0,
    "gpu_names": [],
    "cudnn_available": None,
    "probe_error": None,
}


# resolve_torch_device -----------------------------------------------------------
def test_cuda_used_when_available():
    res = resolve_torch_device("cuda", report=CUDA_OK)
    assert res.device == "cuda"
    assert res.usable is True
    assert res.fell_back is False
    assert res.cuda_available is True


def test_cpu_only_build_falls_back_with_real_reason():
    res = resolve_torch_device("cuda", allow_cpu_fallback=True, report=CPU_ONLY_BUILD)
    assert res.device == "cpu"
    assert res.usable is True
    assert res.fell_back is True
    # The reason must name the *actual* diagnosis, not a generic message.
    assert "CPU-only build" in res.reason
    assert res.fix_hint  # actionable remediation present


def test_require_cuda_fails_with_diagnosis():
    res = resolve_torch_device(
        "cuda", allow_cpu_fallback=True, require_cuda=True, report=CPU_ONLY_BUILD
    )
    assert res.usable is False
    assert res.require_failed is True
    assert "required" in res.reason.lower()
    assert res.fix_hint


def test_fallback_disabled_is_unusable():
    res = resolve_torch_device(
        "cuda", allow_cpu_fallback=False, require_cuda=False, report=CPU_ONLY_BUILD
    )
    assert res.usable is False
    assert res.fell_back is False


def test_auto_prefers_cuda_then_cpu():
    on = resolve_torch_device("auto", report=CUDA_OK)
    assert on.device == "cuda" and on.usable is True
    off = resolve_torch_device("auto", report=CPU_ONLY_BUILD)
    assert off.device == "cpu" and off.usable is True and off.fell_back is False


def test_explicit_cpu_never_touches_cuda():
    res = resolve_torch_device("cpu", report=CUDA_OK)
    assert res.device == "cpu"
    assert res.usable is True
    assert res.fell_back is False


def test_cuda_build_but_no_visible_device():
    res = resolve_torch_device("cuda", allow_cpu_fallback=True, report=CUDA_BUILD_NO_DEVICE)
    assert res.device == "cpu"
    assert res.fell_back is True
    assert "no CUDA devices" in res.reason


# reason / diagnostics -----------------------------------------------------------
def test_cuda_unavailable_reason_identifies_cpu_build():
    reason = cuda_unavailable_reason(CPU_ONLY_BUILD)
    assert "CPU-only build" in reason


def test_torch_cuda_report_has_expected_keys():
    report = torch_cuda_report()
    for key in ("torch_installed", "cuda_available", "device_count", "gpu_names"):
        assert key in report


def test_format_diagnostics_is_safe_lines():
    lines = format_diagnostics(CPU_ONLY_BUILD)
    assert any("PyTorch version" in ln for ln in lines)
    # CTranslate2 line is always present (may say n/a).
    assert any("CTranslate2" in ln for ln in lines)

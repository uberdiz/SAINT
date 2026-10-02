"""Release model ZIPs install into the layout SAINT reads (offline, via file:// base)."""
import importlib
import zipfile

import pytest


def _make_assets(root):
    with zipfile.ZipFile(root / "faster-whisper-base.en.zip", "w") as z:
        z.writestr("commit.txt", "abc123")
        for f in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
            z.writestr(f"snapshot/{f}", "x")
    with zipfile.ZipFile(root / "kokoro-onnx.zip", "w") as z:
        z.writestr("kokoro-onnx/kokoro-v1.0.onnx", "x")
        z.writestr("kokoro-onnx/voices-v1.0.bin", "x")


def test_ensure_all_installs_both(tmp_path, monkeypatch):
    assets = tmp_path / "assets"; assets.mkdir()
    _make_assets(assets)
    monkeypatch.setenv("SAINT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SAINT_MODEL_ASSET_BASE", assets.as_uri())
    from core import model_assets
    model_assets = importlib.reload(model_assets)
    assert model_assets.ensure_all() == {"whisper": True, "kokoro": True}


def test_unsafe_zip_rejected(tmp_path):
    from core.model_assets import _safe_extract
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../evil.txt", "x")
    with zipfile.ZipFile(bad) as z, pytest.raises(RuntimeError):
        _safe_extract(z, tmp_path / "out")

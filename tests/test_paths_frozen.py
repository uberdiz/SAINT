"""SAINT.exe: shipped assets come from the bundle, user data goes to %LOCALAPPDATA%\\SAINT."""

import importlib
import sys

import pytest

import core.paths as paths


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    bundle = tmp_path / "SAINT" / "_internal"
    (bundle / "data" / "wake").mkdir(parents=True)
    (bundle / "data" / "wake" / "hey_saint.onnx").write_bytes(b"x")
    (bundle / "SAINT.png").write_bytes(b"x")
    local = tmp_path / "local"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "SAINT" / "SAINT.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("SAINT_DATA_DIR", raising=False)
    mod = importlib.reload(paths)
    yield mod, bundle, local
    monkeypatch.undo()
    importlib.reload(paths)


def test_user_data_goes_to_local_app_data(frozen):
    mod, _bundle, local = frozen
    assert mod.data_dir() == local / "SAINT"
    assert mod.data_path("config.json").parent == local / "SAINT"


def test_shipped_assets_come_from_the_bundle(frozen):
    mod, bundle, local = frozen
    assert mod.resolve_project_path("SAINT.png") == bundle / "SAINT.png"
    assert mod.resolve_project_path("data/wake/hey_saint.onnx") == bundle / "data" / "wake" / "hey_saint.onnx"
    # Something the app creates (a downloaded model) lives with the user's data.
    assert mod.resolve_project_path("data/vision/flux") == local / "SAINT" / "vision" / "flux"


def test_a_user_copy_of_an_asset_wins(frozen):
    mod, _bundle, local = frozen
    mine = local / "SAINT" / "wake" / "hey_saint.onnx"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_bytes(b"mine")
    assert mod.resolve_project_path("data/wake/hey_saint.onnx") == mine


def test_running_from_source_is_unchanged():
    assert not paths.FROZEN
    assert paths.resolve_project_path("SAINT.png") == paths.PROJECT_ROOT / "SAINT.png"


@pytest.fixture
def source_with_install(monkeypatch, tmp_path):
    """Run from source on a PC where the installer put SAINT.exe in %LOCALAPPDATA%\\Programs\\SAINT."""
    local = tmp_path / "local"
    (local / "Programs" / "SAINT").mkdir(parents=True)
    (local / "Programs" / "SAINT" / "SAINT.exe").write_bytes(b"x")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("SAINT_DATA_DIR", raising=False)
    mod = importlib.reload(paths)
    yield mod, local
    monkeypatch.undo()
    importlib.reload(paths)


def test_source_run_shares_the_installed_apps_data(source_with_install):
    mod, local = source_with_install
    assert not mod.FROZEN
    assert mod.data_dir() == local / "SAINT"                     # one memory and history, one lock
    assert mod.resolve_project_path("data/memory/saint_memory.db") == local / "SAINT" / "memory" / "saint_memory.db"
    # Shipped assets still come from the checkout.
    assert mod.resolve_project_path("data/wake/hey_saint.onnx") == mod.PROJECT_ROOT / "data" / "wake" / "hey_saint.onnx"


def test_source_run_starts_the_installed_app(source_with_install, monkeypatch):
    mod, local = source_with_install
    import core.autostart as autostart
    monkeypatch.setattr(autostart, "installed_exe", mod.installed_exe)
    target, args, _ = autostart.launch_command()
    assert target == str(local / "Programs" / "SAINT" / "SAINT.exe")
    assert args == "--background"


def test_source_run_without_install_keeps_project_data(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty"))
    monkeypatch.delenv("SAINT_DATA_DIR", raising=False)
    mod = importlib.reload(paths)
    try:
        assert mod.installed_exe() is None
        assert mod.data_dir() == mod.PROJECT_ROOT / "data"
    finally:
        monkeypatch.undo()
        importlib.reload(paths)

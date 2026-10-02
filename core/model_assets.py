"""
core/model_assets.py

Download large optional model files from SAINT GitHub Releases instead of
putting them inside the Windows installer.
"""

from __future__ import annotations
import logging, os, shutil, tempfile, time, zipfile
from pathlib import Path
from urllib.request import Request, urlopen
from core.paths import data_dir

log = logging.getLogger("saint.model_assets")
RELEASE_TAG = "unified-latest"
RELEASE_BASE = os.environ.get("SAINT_MODEL_ASSET_BASE") or f"https://github.com/uberdiz/SAINT/releases/download/{RELEASE_TAG}"
WHISPER_ASSET = "faster-whisper-base.en.zip"
KOKORO_ASSET = "kokoro-onnx.zip"
WHISPER_REPO = "models--Systran--faster-whisper-base.en"

def _cache_root() -> Path:
    root = data_dir() / "models" / "hf" / "hub"
    root.mkdir(parents=True, exist_ok=True)
    return root

def whisper_ready() -> bool:
    repo = _cache_root() / WHISPER_REPO
    ref = repo / "refs" / "main"
    if not ref.exists(): return False
    commit = ref.read_text(encoding="utf-8").strip()
    return bool(commit) and (repo / "snapshots" / commit / "model.bin").exists()

def kokoro_ready() -> bool:
    d = data_dir() / "tts" / "kokoro-onnx"
    return (d / "kokoro-v1.0.onnx").exists() and (d / "voices-v1.0.bin").exists()

def _download(name: str) -> Path:
    """Download a release asset to a temp file (large: never hold it in memory)."""
    url = f"{RELEASE_BASE}/{name}"
    last: Exception | None = None
    for attempt in range(1, 4):
        fd, tmp = tempfile.mkstemp(prefix="saint-asset-", suffix=".zip", dir=str(data_dir()))
        path = Path(tmp)
        try:
            log.info("Downloading SAINT model asset: %s", url)
            req = Request(url, headers={"User-Agent": "SAINT"})
            with urlopen(req, timeout=60) as response, os.fdopen(fd, "wb") as out:
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk: break
                    out.write(chunk); done += len(chunk)
                    if total and done % (50 * 1024 * 1024) < 1024 * 1024:
                        log.info("SAINT model download %s: %.0f%%", name, done * 100 / total)
            if total and done != total: raise OSError(f"incomplete download ({done}/{total} bytes)")
            return path
        except Exception as e:
            last = e
            path.unlink(missing_ok=True)
            log.warning("Download of %s failed (attempt %d/3): %s", name, attempt, e)
            time.sleep(2 * attempt)
    raise RuntimeError(f"Could not download {url}: {last}")

def _safe_extract(zf: zipfile.ZipFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in zf.infolist():
        target = (destination / member.filename).resolve()
        if target != destination and destination not in target.parents:
            raise RuntimeError(f"Unsafe model archive path: {member.filename}")
    zf.extractall(destination)

def ensure_whisper() -> bool:
    if whisper_ready(): return True
    archive = _download(WHISPER_ASSET)
    temp = Path(tempfile.mkdtemp(prefix="saint-whisper-", dir=str(_cache_root())))
    try:
        with zipfile.ZipFile(archive) as zf:
            commit = zf.read("commit.txt").decode("utf-8").strip()
            if not commit: raise RuntimeError("Whisper release asset has no commit identifier")
            _safe_extract(zf, temp)
        snapshot_src = temp / "snapshot"
        if not snapshot_src.is_dir(): raise RuntimeError("Whisper release asset is missing snapshot/")
        repo = _cache_root() / WHISPER_REPO
        snapshot_dst = repo / "snapshots" / commit
        snapshot_dst.parent.mkdir(parents=True, exist_ok=True)
        if snapshot_dst.exists(): shutil.rmtree(snapshot_dst)
        shutil.copytree(snapshot_src, snapshot_dst)
        (repo / "refs").mkdir(parents=True, exist_ok=True)
        (repo / "refs" / "main").write_text(commit, encoding="utf-8")
    finally:
        shutil.rmtree(temp, ignore_errors=True)
        archive.unlink(missing_ok=True)
    return whisper_ready()

def ensure_kokoro() -> bool:
    if kokoro_ready(): return True
    archive = _download(KOKORO_ASSET)
    destination = data_dir() / "tts" / "kokoro-onnx"
    temp = Path(tempfile.mkdtemp(prefix="saint-kokoro-", dir=str(data_dir())))
    try:
        with zipfile.ZipFile(archive) as zf: _safe_extract(zf, temp)
        src = temp / "kokoro-onnx"
        if not src.is_dir(): raise RuntimeError("Kokoro release asset is missing kokoro-onnx/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists(): shutil.rmtree(destination)
        shutil.copytree(src, destination)
    finally:
        shutil.rmtree(temp, ignore_errors=True)
        archive.unlink(missing_ok=True)
    return kokoro_ready()

def ensure_all() -> dict[str, bool]:
    results = {"whisper": whisper_ready(), "kokoro": kokoro_ready()}
    for key, fn in (("whisper", ensure_whisper), ("kokoro", ensure_kokoro)):
        if results[key]: continue
        try: results[key] = bool(fn())
        except Exception: log.exception("Could not download %s model asset", key)
    return results

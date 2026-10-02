"""Create the large model ZIPs used by the SAINT Windows release.

Run from anywhere:  python packaging/windows/prepare_model_assets.py
Output: build/release-assets/{faster-whisper-base.en.zip, kokoro-onnx.zip, model-assets.json}
Layouts match core/model_assets.py (snapshot/ + commit.txt, kokoro-onnx/).
"""
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Running a script puts its own folder on sys.path, not the repo root, so make
# `tools` / `core` importable regardless of PYTHONPATH or the working directory.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "build" / "release-assets"
WHISPER_REPO = "Systran/faster-whisper-base.en"
WHISPER_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
KOKORO_FILES = {"kokoro-v1.0.onnx": 300_000_000, "voices-v1.0.bin": 25_000_000}


def zip_tree(source: Path, archive: Path, root_name: str, extra=None):
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name, value in (extra or {}).items():
            zf.writestr(name, value)
        for path in sorted(source.rglob("*")):
            if path.is_file():
                zf.write(path, f"{root_name}/{path.relative_to(source).as_posix()}")


def verify_zip(archive: Path, required: list[str]):
    with zipfile.ZipFile(archive) as zf:
        bad = zf.testzip()
        if bad:
            raise RuntimeError(f"{archive.name}: corrupt member {bad}")
        names = set(zf.namelist())
        for n in zf.namelist():
            if n.startswith(("/", "\\")) or ".." in Path(n).parts:
                raise RuntimeError(f"{archive.name}: unsafe path {n}")
    missing = [r for r in required if r not in names]
    if missing:
        raise RuntimeError(f"{archive.name} is missing {missing}")


def main():
    from huggingface_hub import snapshot_download
    from tools.get_kokoro_onnx import ensure

    OUT.mkdir(parents=True, exist_ok=True)
    # HF_TOKEN (optional) is read from the environment by huggingface_hub.
    whisper_dir = Path(snapshot_download(WHISPER_REPO, allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.*"]))
    commit = whisper_dir.name
    zip_tree(whisper_dir, OUT / "faster-whisper-base.en.zip", "snapshot", {"commit.txt": commit})
    verify_zip(OUT / "faster-whisper-base.en.zip", ["commit.txt"] + [f"snapshot/{f}" for f in WHISPER_FILES])

    kokoro = ensure()  # downloads into data/tts/kokoro-onnx (skips files already present)
    zip_tree(kokoro, OUT / "kokoro-onnx.zip", "kokoro-onnx")
    verify_zip(OUT / "kokoro-onnx.zip", [f"kokoro-onnx/{f}" for f in KOKORO_FILES])

    (OUT / "model-assets.json").write_text(json.dumps({
        "whisper": "faster-whisper-base.en.zip", "kokoro": "kokoro-onnx.zip",
        "whisper_repo": WHISPER_REPO, "whisper_revision": commit}, indent=2), encoding="utf-8")
    for p in sorted(OUT.iterdir()):
        print(f"{p.name}: {p.stat().st_size / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()

"""Create the large model ZIPs used by the SAINT Windows release."""
from pathlib import Path
import json, zipfile
from huggingface_hub import snapshot_download
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "build" / "release-assets"; OUT.mkdir(parents=True, exist_ok=True)

def zip_tree(source: Path, archive: Path, root_name: str, extra=None):
    if archive.exists(): archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name, value in (extra or {}).items(): zf.writestr(name, value)
        for path in source.rglob("*"):
            if path.is_file(): zf.write(path, f"{root_name}/{path.relative_to(source).as_posix()}")

def main():
    whisper_dir = Path(snapshot_download("Systran/faster-whisper-base.en", allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.*"]))
    commit = whisper_dir.name
    zip_tree(whisper_dir, OUT / "faster-whisper-base.en.zip", "snapshot", {"commit.txt": commit})
    kokoro = ROOT / "data" / "tts" / "kokoro-onnx"
    if not (kokoro / "kokoro-v1.0.onnx").exists() or not (kokoro / "voices-v1.0.bin").exists():
        from tools.get_kokoro_onnx import ensure; ensure()
    zip_tree(kokoro, OUT / "kokoro-onnx.zip", "kokoro-onnx")
    (OUT / "model-assets.json").write_text(json.dumps({"whisper": "faster-whisper-base.en.zip", "kokoro": "kokoro-onnx.zip", "whisper_repo": "Systran/faster-whisper-base.en", "whisper_revision": commit}, indent=2), encoding="utf-8")
    for p in OUT.iterdir(): print(f"{p.name}: {p.stat().st_size / 1024 / 1024:.1f} MB")

if __name__ == "__main__": main()
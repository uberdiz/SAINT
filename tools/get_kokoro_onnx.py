"""
tools/get_kokoro_onnx.py — download SAINT's voice for onnxruntime (no PyTorch needed).

    python tools/get_kokoro_onnx.py

Puts kokoro-v1.0.onnx and voices-v1.0.bin (about 350 MB) in data/tts/kokoro-onnx/,
where modules/voice/kokoro_onnx.py looks for them. packaging/windows/build.py
runs this first and bundles the files into SAINT.exe.
"""

import shutil
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "data" / "tts" / "kokoro-onnx"
BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
FILES = {"kokoro-v1.0.onnx": 300_000_000, "voices-v1.0.bin": 25_000_000}     # name -> minimum size


def ensure() -> Path:
    DEST.mkdir(parents=True, exist_ok=True)
    for name, minimum in FILES.items():
        target = DEST / name
        if target.exists() and target.stat().st_size >= minimum:
            continue
        part = target.with_suffix(target.suffix + ".part")
        print(f"downloading {name} …", flush=True)
        for attempt in range(1, 4):
            try:
                req = urllib.request.Request(BASE + name, headers={"User-Agent": "SAINT"})
                with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as f:
                    shutil.copyfileobj(r, f, 1024 * 1024)
                break
            except OSError as e:
                if attempt == 3:
                    raise
                print(f"  retry {attempt} after error: {e}", flush=True)
                time.sleep(3 * attempt)
        if part.stat().st_size < minimum:
            raise RuntimeError(f"{name} looks incomplete ({part.stat().st_size} bytes)")
        part.replace(target)
    return DEST


if __name__ == "__main__":
    print(ensure())
    sys.exit(0)

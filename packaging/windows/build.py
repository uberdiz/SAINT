"""
packaging/windows/build.py — build SAINT for Windows.

    python packaging/windows/build.py              # SAINT.exe folder + SAINT-Setup.exe
    python packaging/windows/build.py --no-installer
    python packaging/windows/build.py --no-models  # don't bundle the speech models

Outputs (build/ is git-ignored):
    build/windows/SAINT/SAINT.exe      the app (with its _internal folder and bundled models)
    build/windows/SAINT-Setup.exe      per-user installer (Start menu + optional desktop shortcut)

Needs: the project's .venv with requirements installed, PyInstaller, and
Inno Setup 6 (ISCC.exe) for the installer. The speech models are copied from
the local Hugging Face cache (they're downloaded the first time SAINT speaks
or listens from source), so the packaged app works offline.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "build" / "windows"
WORK = OUT / "work"
DIST = OUT                                   # PyInstaller writes OUT / "SAINT"
APP = OUT / "SAINT"

from core.version import VERSION, VERSION_TUPLE  # noqa: E402

# Hugging Face repos SAINT loads by default (Settings > Voice): Whisper base.en. The voice (Kokoro on
# onnxruntime, data/tts/kokoro-onnx) is bundled by saint.spec.
MODELS = {
    "Systran/faster-whisper-{stt}": None,                       # everything in the snapshot
}


def make_icon():
    from PIL import Image
    WORK.mkdir(parents=True, exist_ok=True)
    img = Image.open(ROOT / "SAINT.png").convert("RGBA")
    img.save(WORK / "SAINT.ico", format="ICO",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def make_version_file():
    v = ", ".join(str(x) for x in VERSION_TUPLE)
    (WORK / "version_info.txt").write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({v}), prodvers=({v}), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'SAINT'),
      StringStruct('FileDescription', 'SAINT'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'SAINT'),
      StringStruct('OriginalFilename', 'SAINT.exe'),
      StringStruct('ProductName', 'SAINT'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")


def pyinstaller():
    env = dict(os.environ, SAINT_BUILD_WORK=str(WORK))
    cmd = [sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "windows" / "saint.spec"),
           "--noconfirm", "--clean", "--distpath", str(DIST), "--workpath", str(WORK / "pyinstaller")]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=str(ROOT), env=env)


def hf_cache() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    home = Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface")
    return home / "hub"


def bundle_models(stt: str):
    """Copy the speech models into SAINT/models/hf/hub in the Hugging Face cache layout."""
    src_root, dst_root = hf_cache(), APP / "models" / "hf" / "hub"
    for repo, keep in MODELS.items():
        repo = repo.format(stt=stt)
        name = "models--" + repo.replace("/", "--")
        src = src_root / name
        refs = src / "refs" / "main"
        if not refs.exists():
            print(f"!! {repo} isn't in the Hugging Face cache ({src}); SAINT will download it on first use.")
            continue
        commit = refs.read_text().strip()
        snap = src / "snapshots" / commit
        dst = dst_root / name
        (dst / "refs").mkdir(parents=True, exist_ok=True)
        (dst / "refs" / "main").write_text(commit)
        for f in snap.rglob("*"):
            rel = f.relative_to(snap).as_posix()
            if f.is_dir() or (keep and not any(rel == k or (k.endswith("/") and rel.startswith(k)) for k in keep)):
                continue
            out = dst / "snapshots" / commit / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f.resolve(), out)                # real files, not cache symlinks
        print(f"bundled {repo} @ {commit[:8]}")


def find_iscc() -> Path:
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("ProgramFiles", "")):
        p = Path(base) / ("Programs" if base == os.environ.get("LOCALAPPDATA") else "") / "Inno Setup 6" / "ISCC.exe"
        if p.exists():
            return p
    found = shutil.which("ISCC")
    if found:
        return Path(found)
    raise FileNotFoundError("Inno Setup 6 (ISCC.exe) isn't installed — winget install JRSoftware.InnoSetup")


def installer():
    iscc = find_iscc()
    cmd = [str(iscc), f"/DAppVersion={VERSION}", f"/DSourceDir={APP}", f"/DOutputDir={OUT}",
           f"/DIconFile={WORK / 'SAINT.ico'}", str(ROOT / "packaging" / "windows" / "SAINT.iss")]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=str(ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-installer", action="store_true")
    ap.add_argument("--no-models", action="store_true")
    ap.add_argument("--stt-model", default="base.en")
    args = ap.parse_args()
    from tools.get_kokoro_onnx import ensure as ensure_voice
    ensure_voice()                                   # the ONNX voice the spec bundles
    make_icon()
    make_version_file()
    pyinstaller()
    if not args.no_models:
        bundle_models(args.stt_model)
    if not args.no_installer:
        installer()
    print(f"\nSAINT {VERSION}: {APP / 'SAINT.exe'}")
    if not args.no_installer:
        print(f"Installer:   {OUT / 'SAINT-Setup.exe'}")


if __name__ == "__main__":
    main()

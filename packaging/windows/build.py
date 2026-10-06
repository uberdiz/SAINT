"""
packaging/windows/build.py — build SAINT for Windows.

    python packaging/windows/build.py              # SAINT.exe folder + SAINT-Setup.exe
    python packaging/windows/build.py --no-installer
    python packaging/windows/build.py --no-models  # don't bundle the speech models

Outputs (build/ is git-ignored):
    build/windows/SAINT/SAINT.exe      the app (with its _internal folder and bundled models)
    build/windows/SAINT-Setup.exe      per-user installer (Start menu + optional desktop shortcut)

Needs: Python 3.12, the project's dependencies, PyInstaller, and Inno Setup 6.
The release installer does not contain the large Whisper/Kokoro model files;
the installed app downloads those files from the SAINT GitHub Release.
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

# Large voice models are release assets, not part of SAINT.exe: the installer downloads the Kokoro voice
# (SAINT.iss) and the app downloads anything still missing from the rolling GitHub release on first use.

# What Kokoro needs inside SAINT.exe. Without any of these it speaks with the Windows voice instead
# (kokoro_onnx missing from requirements.txt is why 2026-10 builds sounded robotic).
VOICE_PACKAGES = ("kokoro_onnx", "misaki", "en_core_web_sm", "espeakng_loader", "onnxruntime")


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


def ensure_supported_python():
    """The native Windows dependency stack is released against Python 3.12."""
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError(
            f"SAINT Windows releases must be built with Python 3.12.x; "
            f"this interpreter is {sys.version.split()[0]}. "
            "Use the GitHub Actions release workflow instead of installing Python manually."
        )


def venv_python() -> Path:
    return ROOT / ".venv" / "Scripts" / "python.exe"


def run_checked(cmd, *, cwd=ROOT, env=None):
    print("+", " ".join(str(x) for x in cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=str(cwd), env=env)


def ensure_build_environment():
    """Create/repair the build venv without ever silently using an unsupported Python."""
    ensure_supported_python()
    # GitHub Actions already installed the pinned dependencies into its
    # Python 3.12 environment. Do not create a second venv there.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return

    target = venv_python()

    if Path(sys.executable).resolve() != target.resolve():
        if not target.exists():
            print("Creating SAINT .venv for the Windows release build...", flush=True)
            run_checked([sys.executable, "-m", "venv", str(ROOT / ".venv")])

        print("Installing/repairing SAINT dependencies...", flush=True)
        py = str(target)

        # Install the CUDA PyTorch wheel first on NVIDIA systems. This keeps the
        # normal requirements install from replacing it with a CPU-only wheel.
        if shutil.which("nvidia-smi"):
            run_checked([
                py, "-m", "pip", "install", "--upgrade",
                "--index-url", "https://download.pytorch.org/whl/cu128",
                "torch", "torchaudio",
            ])

        # No --upgrade: it would swap the CUDA torch above for PyPI's CPU wheel and leave torchaudio behind.
        run_checked([py, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])

        print("Re-launching build.py inside .venv...", flush=True)
        run_checked([py, str(Path(__file__).resolve()), *sys.argv[1:]])
        raise SystemExit(0)

    checks = ["PIL", "PyInstaller", "requests", "PySide6"] + list(VOICE_PACKAGES)
    missing = []
    for name in checks:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)

    if missing:
        print("Missing build dependencies:", ", ".join(missing), flush=True)
        run_checked([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])


def find_iscc() -> Path:
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("ProgramFiles", "")):
        if not base:
            continue
        candidates = [
            Path(base) / "Programs" / "Inno Setup 6" / "ISCC.exe",
            Path(base) / "Inno Setup 6" / "ISCC.exe",
        ]
        for p in candidates:
            if p.exists():
                return p
    found = shutil.which("ISCC")
    if found:
        return Path(found)

    # Make a normal build.py checkout self-contained on Windows when winget is available.
    winget = shutil.which("winget")
    if winget:
        print("Inno Setup 6 not found; installing it with winget...", flush=True)
        run_checked([
            winget, "install", "--id", "JRSoftware.InnoSetup", "-e",
            "--accept-source-agreements", "--accept-package-agreements",
        ])
        for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles(x86)", ""),
                     os.environ.get("ProgramFiles", "")):
            if not base:
                continue
            for p in (
                Path(base) / "Programs" / "Inno Setup 6" / "ISCC.exe",
                Path(base) / "Inno Setup 6" / "ISCC.exe",
            ):
                if p.exists():
                    return p
        found = shutil.which("ISCC")
        if found:
            return Path(found)

    raise FileNotFoundError(
        "Inno Setup 6 (ISCC.exe) isn't installed and winget was unavailable. "
        "Install Inno Setup 6, then run build.py again."
    )


def installer():
    iscc = find_iscc()
    cmd = [str(iscc), f"/DAppVersion={VERSION}", f"/DSourceDir={APP}", f"/DOutputDir={OUT}",
           f"/DIconFile={WORK / 'SAINT.ico'}", str(ROOT / "packaging" / "windows" / "SAINT.iss")]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=str(ROOT))


_SHIPPED_SOURCE = ("app.py", "core", "modules", "ui")


def _personal_strings() -> set:
    """Email addresses from the builder's own SAINT data (scenes, saved answers, settings)."""
    import re
    from core.paths import base_data_dir
    found = set()
    for name in ("scenes.json", "lesson_answers.json", "config.json", "skills.json"):
        try:
            text = (base_data_dir() / name).read_text(encoding="utf-8")
        except OSError:
            continue
        found |= {a.lower() for a in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)}
    return {a for a in found if not a.endswith(("@example.com", "@example.org"))}


def check_voice_bundle():
    """Fail the build when Kokoro couldn't run in the frozen app (it would fall back to the Windows voice)."""
    internal = APP / "_internal"
    missing = [p for p in VOICE_PACKAGES if not (internal / p).is_dir()]
    if missing:
        raise SystemExit("SAINT.exe would have no Kokoro voice — missing from the bundle: " + ", ".join(missing)
                         + "\nInstall requirements.txt into the build environment and build again.")
    print("Kokoro voice packages bundled.", flush=True)


def check_no_user_data():
    """A release ships without anyone's data: no memory, history, settings, logins or the
    builder's own addresses. Fails the build instead of shipping them."""
    from core.profiles import USER_DATA_FILES, USER_DATA_SUFFIXES, user_data_in
    problems = []
    data = APP / "_internal" / "data"
    if data.is_dir():
        problems += [f"_internal/data/{h}" for h in user_data_in(data)]
    problems += [f.name for f in APP.iterdir() if f.is_file() and f.name in USER_DATA_FILES]
    problems += [str(f.relative_to(APP)) for f in APP.rglob("*")
                 if f.is_file() and f.name.lower().endswith(USER_DATA_SUFFIXES)]
    personal = _personal_strings()
    if personal:
        for top in _SHIPPED_SOURCE:
            for f in ([ROOT / top] if top.endswith(".py") else (ROOT / top).rglob("*.py")):
                text = f.read_text(encoding="utf-8", errors="ignore").lower()
                problems += [f"{f.relative_to(ROOT)} contains {a}" for a in personal if a in text]
    if problems:
        raise SystemExit("Refusing to package personal data:\n  " + "\n  ".join(sorted(set(problems))))
    print("No user data in the bundle.", flush=True)


def packaged_selftest(voice: bool = False) -> dict:
    """Run build/windows/SAINT/SAINT.exe --selftest the way a user's PC would: its own folder as the
    working directory, a fresh data folder, and no Python anywhere on PATH. Fails the build when a
    required check fails (a missing library, model file, DLL or resource path)."""
    import json
    import tempfile
    exe = APP / "SAINT.exe"
    data = Path(tempfile.mkdtemp(prefix="saint-selftest-"))
    report = data / "selftest.json"
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                           if p and "python" not in p.lower() and ".venv" not in p.lower())
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTHON", "VIRTUAL_ENV", "CONDA"))}
    env.update(PATH=path, SAINT_DATA_DIR=str(data / "SAINT"), QT_QPA_PLATFORM="offscreen")
    if not voice:
        env["SAINT_SELFTEST_NO_VOICE"] = "1"
    print("+ SAINT.exe --selftest (isolated data, no Python on PATH)", flush=True)
    proc = subprocess.run([str(exe), "--selftest", str(report)], cwd=str(APP), env=env, timeout=1800)
    result = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {"passed": False, "checks": []}
    for c in result.get("checks", []):
        mark = "ok  " if c["ok"] else ("FAIL" if c["required"] else "warn")
        print(f"  {mark} {c['name']}: {str(c['detail'])[:160]}", flush=True)
    if proc.returncode != 0 or not result.get("passed"):
        raise SystemExit(f"The packaged SAINT.exe failed its self-test (exit {proc.returncode}); see above.")
    shutil.rmtree(data, ignore_errors=True)
    return result


def main():
    ensure_supported_python()
    ensure_build_environment()
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-installer", action="store_true")
    ap.add_argument("--no-models", action="store_true", help="do not bundle large speech models (default for releases)")
    ap.add_argument("--stt-model", default="base.en")
    ap.add_argument("--no-selftest", action="store_true", help="skip running the packaged SAINT.exe --selftest")
    ap.add_argument("--selftest-voice", action="store_true",
                    help="include the voice round trip in the self-test (downloads the models into a temp folder)")
    args = ap.parse_args()
    make_icon()
    make_version_file()
    pyinstaller()
    check_voice_bundle()
    check_no_user_data()
    if not args.no_selftest:
        packaged_selftest(voice=args.selftest_voice)
    # Models are intentionally not bundled into the installer. They are published
    # as separate GitHub Release assets and downloaded into the user's data folder.
    if not args.no_installer:
        installer()
    print(f"\nSAINT {VERSION}: {APP / 'SAINT.exe'}")
    if not args.no_installer:
        print(f"Installer:   {OUT / 'SAINT-Setup.exe'}")


if __name__ == "__main__":
    main()

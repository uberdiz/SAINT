# PyInstaller spec for SAINT.exe (one folder: SAINT\SAINT.exe + SAINT\_internal\).
# Built by packaging/windows/build.py, which also makes the icon and version file this reads.
# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parents[1]
WORK = Path(os.environ.get("SAINT_BUILD_WORK", ROOT / "build" / "windows" / "work"))

# SAINT's own packages: tools and modules are registered by name at runtime.
hidden = (
    collect_submodules("core")
    + collect_submodules("modules")
    + collect_submodules("ui")
    + collect_submodules("PySide6")
)
# Libraries that load parts of themselves lazily. The voice is Kokoro on onnxruntime (kokoro_onnx), so
# PyTorch and CUDA (2+ GB) aren't shipped; misaki + spaCy still turn text into phonemes.
for pkg in ("kokoro_onnx", "misaki", "faster_whisper", "ctranslate2", "onnxruntime", "sounddevice", "pycaw", "comtypes",
            "uiautomation", "winrt", "keyring", "spacy", "en_core_web_sm", "phonemizer", "espeakng_loader",
            "segno", "zeroconf", "cryptography", "psutil", "win32com", "pythoncom", "pywintypes",
            "spacy_legacy", "spacy_loggers", "thinc", "srsly", "catalogue", "confection", "blis", "cymem", "preshed",
            "murmurhash", "wasabi", "weasel", "langcodes", "language_data", "num2words", "addict"):
    try:
        hidden += collect_submodules(pkg)
    except Exception:
        pass

datas = [
    (str(ROOT / "SAINT.png"), "."),
    (str(ROOT / "data" / "wake" / "hey_saint.onnx"), "data/wake"),
    (str(ROOT / "data" / "wake" / "melspectrogram.onnx"), "data/wake"),
    (str(ROOT / "data" / "wake" / "embedding_model.onnx"), "data/wake"),
    (str(ROOT / "modules" / "lang" / "lexicon"), "modules/lang/lexicon"),
]
# Large Kokoro model files are deliberately excluded. The installed app downloads
# them from the SAINT GitHub Release into %LOCALAPPDATA%/SAINT/data.
for pkg in ("faster_whisper", "kokoro_onnx", "misaki", "espeakng_loader", "en_core_web_sm", "spacy", "language_tags",
            "phonemizer", "segments", "csvw", "jieba", "unidic_lite", "certifi", "openwakeword", "onnxruntime",
            "ctranslate2", "_sounddevice_data", "soundfile", "uiautomation"):
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass

# spaCy finds its English model (Kokoro's phonemiser needs it) and its plug-ins through package metadata.
# spacy-curated-transformers is left out on purpose: its metadata would make spaCy load it, and it needs PyTorch.
for pkg in ("en_core_web_sm", "spacy", "thinc", "catalogue", "confection", "srsly", "spacy_legacy", "spacy_loggers",
            "misaki", "kokoro_onnx", "onnxruntime", "phonemizer", "huggingface_hub", "faster_whisper",
            "tokenizers", "numpy", "regex", "tqdm", "requests", "packaging", "filelock", "pyyaml"):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass

binaries = []
for pkg in ("ctranslate2", "onnxruntime", "_sounddevice_data", "soundfile", "espeakng_loader"):
    try:
        binaries += collect_dynamic_libs(pkg)
    except Exception:
        pass

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "IPython", "jupyter", "notebook", "pytest", "tests", "tools",
              "PyQt5", "PyQt6", "PySide2",
              # PyTorch and what only it needs: the ONNX voice replaces it (and the Settings page says
              # "install from source" for the optional GPU extras like image captions).
              "torch", "torchvision", "torchaudio", "kokoro", "transformers", "accelerate", "diffusers",
              "safetensors", "sympy", "triton", "spacy_curated_transformers", "curated_transformers",
              "curated_tokenizers", "qwen_tts"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SAINT",
    icon=str(WORK / "SAINT.ico"),
    version=str(WORK / "version_info.txt"),
    console=False,                       # no terminal window
    disable_windowed_traceback=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="SAINT", upx=False)

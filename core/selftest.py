"""
core/selftest.py

``SAINT.exe --selftest report.json`` — checks a packaged (or source) SAINT end to end without a
microphone, a speaker, or the developer's Python, and writes what it found as JSON:

    imports      every library the app needs (PySide6, requests, Pillow, numpy, onnxruntime,
                 faster-whisper / CTranslate2, kokoro-onnx, misaki + spaCy model, sounddevice,
                 segno, WinRT OCR, pywin32, uiautomation, pycaw, keyring, psutil...)
    assets       the logo and the wake-word models where the frozen app looks for them
    data         the data folder is writable; what the migration still has pending
    ui           the main window and every page build (offscreen)
    qr           a pairing QR code renders whole
    ocr          Windows' OCR reads a rendered word
    voice        Kokoro says "Hey SAINT, open Discord" -> the wake-word model scores it -> Whisper
                 writes it back (models downloaded on first use, as a real first run would)
    agent        a goal plans, and the router routes a few commands

Exit code 0 when every required check passed. Nothing is played aloud, no window is shown, the
single-instance lock isn't taken, and no real action runs.
"""

import importlib
import io
import json
import os
import sys
import time
import traceback
from typing import Callable, Dict, List

REQUIRED_IMPORTS = ["PySide6.QtWidgets", "PySide6.QtSvg", "requests", "PIL.Image", "numpy", "psutil",
                    "onnxruntime", "sounddevice", "soundfile", "faster_whisper", "ctranslate2", "kokoro_onnx",
                    "misaki.en", "en_core_web_sm", "espeakng_loader", "phonemizer", "segno", "keyring",
                    "win32api", "win32gui", "pythoncom", "uiautomation", "pycaw.pycaw", "comtypes",
                    "winrt.windows.media.control", "winrt.windows.media.ocr", "zeroconf", "cryptography"]
OPTIONAL_IMPORTS = ["pyautogui"]


class Report:
    def __init__(self):
        self.checks: List[Dict] = []

    def check(self, name: str, fn: Callable, required: bool = True):
        t0 = time.perf_counter()
        try:
            detail = fn()
            ok = True
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            ok = False
            if os.environ.get("SAINT_SELFTEST_TRACE"):
                detail += "\n" + traceback.format_exc()
        self.checks.append({"name": name, "ok": ok, "required": required, "detail": detail,
                            "ms": round((time.perf_counter() - t0) * 1000)})
        return ok

    @property
    def passed(self) -> bool:
        return all(c["ok"] for c in self.checks if c["required"])


def _imports():
    missing = []
    for mod in REQUIRED_IMPORTS:
        try:
            importlib.import_module(mod)
        except Exception as e:
            missing.append(f"{mod} ({type(e).__name__}: {e})")
    if missing:
        raise ImportError("; ".join(missing))
    return f"{len(REQUIRED_IMPORTS)} modules"


def _assets():
    from core.paths import FROZEN, resolve_project_path
    files = ["SAINT.png", "data/wake/hey_saint.onnx", "data/wake/melspectrogram.onnx", "data/wake/embedding_model.onnx"]
    missing = [f for f in files if not resolve_project_path(f).exists()]
    if missing:
        raise FileNotFoundError(", ".join(missing))
    from modules.lang import pack
    langs = getattr(pack, "available", lambda: [])()
    return f"frozen={FROZEN}; assets ok; language packs: {len(langs) if langs else 'loaded'}"


def _data():
    from core import migration
    from core.paths import data_dir
    d = data_dir()
    probe = d / ".selftest"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    return f"{d} writable; migrations pending: {migration.pending(d) or 'none'}"


def _ui():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from core.config import config
    config.set("overlay.hotkey", "", persist=False)
    from ui.main_window import MainWindow
    w = MainWindow(app, None)
    pages = list(w.page_map)
    for key in pages:
        w.navigate(key, animate=False)
        app.processEvents()
    w.halo.shutdown()
    w.hotkey.unregister()
    w.hide()
    return f"{len(pages)} pages built: {', '.join(pages)}"


def _qr():
    from modules.link.identity import PairingOffer
    from modules.link.service import qr_matrix
    from ui.components.qr_view import MIN_MODULE_PX, module_px
    m = qr_matrix(PairingOffer(os.urandom(5), "own", 9e12).uri("192.168.1.20", 8765, "PC",
                                                               alternates=["100.64.1.2", "2601:18c::1"]))
    if not m:
        raise RuntimeError("segno produced no QR code")
    cell = module_px(260, len(m))
    if cell < MIN_MODULE_PX:
        raise RuntimeError(f"QR modules would be {cell}px")
    return f"{len(m)}x{len(m)} modules, {cell}px each at 100%"


def _ocr():
    from PIL import Image, ImageDraw, ImageFont
    from modules.vision import ocr
    if not ocr.available():
        raise RuntimeError("Windows OCR engine unavailable")
    img = Image.new("RGB", (360, 80), "white")
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 32)
    except OSError:
        font = ImageFont.load_default()
    ImageDraw.Draw(img).text((12, 18), "Submit order", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    text = ocr.recognize(buf.getvalue())["text"]
    if "submit" not in text.lower():
        raise RuntimeError(f"read {text!r}")
    return f"read {text!r}"


def _resample(audio, src: int, dst: int):
    import numpy as np
    if src == dst:
        return audio.astype("float32")
    n = int(len(audio) * dst / src)
    x = np.linspace(0, len(audio) - 1, n)
    return np.interp(x, np.arange(len(audio)), audio).astype("float32")


_SPOKEN = {}


def _tts():
    from core import model_assets
    if not model_assets.kokoro_ready():
        model_assets.ensure_kokoro()
    from modules.voice.kokoro_onnx import KokoroOnnxTTS, available
    if not available():
        raise RuntimeError("Kokoro voice files missing and couldn't be downloaded")
    import numpy as np
    tts = KokoroOnnxTTS(voice="af_heart")
    tts._load()
    chunks = [r.audio for r in tts._pipeline("Hey Saint. Open Discord.", voice="af_heart", speed=1.0)]
    audio = np.concatenate([np.asarray(c, dtype="float32").reshape(-1) for c in chunks])
    _SPOKEN["audio16"] = _resample(audio, 24000, 16000)
    return f"{len(audio) / 24000:.2f}s of speech synthesised (not played)"


def _wake():
    import numpy as np
    from modules.voice.wake_word import OnnxWakeWordDetector
    det = OnnxWakeWordDetector(threshold=0.5)
    if not det.ready:
        raise RuntimeError(det.status().error)
    audio = _SPOKEN.get("audio16")
    silence = np.zeros(16000, dtype=np.int16)
    det.feed(silence)
    if audio is None:
        return "model loaded (no synthetic speech to score)"
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    det.feed(np.concatenate([pcm, silence]))
    return f"model loaded; peak score on synthetic “Hey SAINT” {det.peak_score:.2f} (threshold 0.5)"


def _stt():
    from modules.voice.stt import FasterWhisperSTT
    import numpy as np
    stt = FasterWhisperSTT(model_name="base.en", device="cpu", compute_type="int8")
    audio = _SPOKEN.get("audio16")
    if audio is None:
        stt.warm_up()
        return "model loaded"
    # transcribe() takes microphone-scale samples (int16 range), like the audio thread gives it.
    res = stt.transcribe(np.concatenate([audio, np.zeros(8000, dtype="float32")]) * 32767.0)
    text = (res.text or "").lower()
    if "discord" not in text:
        raise RuntimeError(f"heard {res.text!r}")
    return f"heard {res.text!r}"


def _agent():
    from modules.agent.autonomy import goals, planner
    from modules.agent.router import route
    assert goals.match("set up my coding workspace") is not None
    assert planner.should_task("open discord, open spotify and tell me when everything is ready")
    names = {t: (route(t).name if route(t) else None) for t in ("pause spotify", "open discord", "run the tests")}
    if None in names.values():
        raise RuntimeError(f"unrouted: {names}")
    return names


def run(path: str = "") -> int:
    r = Report()
    from core.version import VERSION
    r.check("imports", _imports)
    for mod in OPTIONAL_IMPORTS:
        r.check(f"import {mod}", lambda m=mod: importlib.import_module(m) and "ok", required=False)
    r.check("assets", _assets)
    r.check("data folder", _data)
    r.check("ui", _ui)
    r.check("pairing qr", _qr)
    r.check("ocr", _ocr, required=False)
    if not os.environ.get("SAINT_SELFTEST_NO_VOICE"):
        r.check("tts (kokoro onnx)", _tts)
        r.check("wake word", _wake)
        r.check("stt (whisper)", _stt)
    r.check("agent", _agent)
    from core.paths import FROZEN
    out = {"version": VERSION, "frozen": FROZEN, "executable": sys.executable, "passed": r.passed,
           "python": sys.version.split()[0], "checks": r.checks}
    text = json.dumps(out, indent=1, default=str)
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)
    return 0 if r.passed else 1

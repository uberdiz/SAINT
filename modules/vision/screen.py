"""
modules/vision/screen.py

Screen awareness pipeline:

    SCREEN CAPTURE        capture()             real (PIL ImageGrab, all monitors)
          ↓
    VISUAL ANALYSIS       VisualAnalyzer        real only when a vision-capable
          ↓                                     Ollama model is configured
    UI ELEMENT CONTEXT    screen_context()      real (Windows UI Automation tree
          ↓                                     + window list + monitors)
    ACTION PLANNING       agent / LLM tools     uses the context above
          ↓
    MOUSE / KEYBOARD      modules.desktop       explicit validated tools

Nothing here pretends to "see": if no vision model is configured,
``VisualAnalyzer.available()`` is False and analysis requests return a clear
"not configured" error. Element-level understanding comes from the OS
accessibility tree, which is accurate for standard apps.
"""

import base64
import io
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

from core.config import config
from core.events import event_bus, EventType
from core.paths import data_path
from modules.automation.tools import ToolError

log = logging.getLogger("saint.vision")


def screenshots_dir() -> Path:
    d = data_path("screens", ".keep").parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def capture(monitor: Optional[int] = None, save: bool = True) -> Dict[str, Any]:
    """Capture all monitors (default) or one monitor (1-based)."""
    try:
        from PIL import ImageGrab
    except Exception as e:
        raise ToolError(f"Screen capture needs Pillow ({e}).", "UNSUPPORTED")
    bbox = None
    if monitor:
        from modules.desktop.controller import desktop
        mons = desktop.monitors()
        if not 1 <= int(monitor) <= len(mons):
            raise ToolError(f"There is no monitor {monitor}.", "NOT_FOUND")
        m = mons[int(monitor) - 1]
        bbox = (m.left, m.top, m.right, m.bottom)
    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    result: Dict[str, Any] = {"width": img.width, "height": img.height, "monitor": monitor or "all"}
    if save:
        path = screenshots_dir() / time.strftime("screen_%Y%m%d_%H%M%S.png")
        img.save(path)
        result["path"] = str(path)
        _prune(int(config.get("vision.keep_screenshots", 20)))
    event_bus.emit_event(EventType.SCREEN_CAPTURED, {k: v for k, v in result.items() if k != "image"})
    result["image"] = img
    return result


def _prune(keep: int):
    files = sorted(screenshots_dir().glob("screen_*.png"))
    for f in files[:-keep] if keep > 0 else files:
        try:
            f.unlink()
        except OSError:
            pass


def screen_context(include_elements: bool = True) -> Dict[str, Any]:
    """What is on screen, from the OS: windows, focus, UI elements."""
    from modules.desktop.controller import desktop
    ctx: Dict[str, Any] = {}
    wins = desktop.list_windows()
    fg = next((w for w in wins if w.foreground), None)
    ctx["active_window"] = {"title": fg.title, "process": fg.process, "monitor": fg.monitor} if fg else None
    ctx["open_windows"] = [{"title": w.title, "process": w.process, "monitor": w.monitor,
                            "minimized": w.minimized} for w in wins[:25]]
    ctx["monitors"] = [{"index": m.index, "width": m.width, "height": m.height, "primary": m.primary}
                       for m in desktop.monitors()]
    if include_elements:
        try:
            from modules.desktop import uia
            ctx["active_window_elements"] = uia.list_elements(limit=40)["elements"]
        except ToolError as e:
            ctx["active_window_elements"] = []
            ctx["elements_error"] = str(e)
    return ctx


class VisualAnalyzer:
    """Image understanding backend (pluggable). Currently: Ollama vision models."""

    def available(self) -> Tuple[bool, str]:
        backend = config.get("vision.analyzer", "none")
        model = config.get("vision.model", "")
        if backend != "ollama" or not model:
            return False, ("No vision model is configured. Pull one in Ollama (e.g. 'ollama pull "
                           "llama3.2-vision') and set it in Settings > Advanced > Vision.")
        caps = self._capabilities(model)
        if caps is None:
            return False, f"Couldn't reach Ollama to check the vision model '{model}'."
        if "vision" not in caps:
            return False, f"The model '{model}' can't read images (no 'vision' capability)."
        return True, ""

    def _capabilities(self, model: str):
        base = config.get("ai.base_url", "http://localhost:11434").replace("localhost", "127.0.0.1")
        try:
            r = requests.post(base.rstrip("/") + "/api/show", json={"model": model}, timeout=5)
            if r.status_code != 200:
                return []
            return r.json().get("capabilities", []) or []
        except requests.RequestException:
            return None

    def analyze(self, image, question: str) -> str:
        ok, reason = self.available()
        if not ok:
            raise ToolError(reason, "VISION_UNAVAILABLE")
        img = image.copy()
        img.thumbnail((1600, 1600))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        base = config.get("ai.base_url", "http://localhost:11434").replace("localhost", "127.0.0.1")
        try:
            r = requests.post(base.rstrip("/") + "/api/chat", timeout=120, json={
                "model": config.get("vision.model"), "stream": False,
                "messages": [{"role": "user", "content": question, "images": [b64]}]})
        except requests.RequestException as e:
            raise ToolError(f"The vision model didn't respond: {e}", "VISION_FAILED")
        if r.status_code != 200:
            raise ToolError(f"The vision model returned HTTP {r.status_code}.", "VISION_FAILED")
        return (r.json().get("message") or {}).get("content", "").strip()


visual_analyzer = VisualAnalyzer()

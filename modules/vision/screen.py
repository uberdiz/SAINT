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
import re
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


_APP_NAMES = {"opera": "Opera", "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "brave": "Brave",
              "spotify": "Spotify", "discord": "Discord", "code": "VS Code", "explorer": "File Explorer",
              "windowsterminal": "Terminal", "winword": "Word", "excel": "Excel", "steam": "Steam",
              "claude": "Claude", "notepad": "Notepad"}


def app_name(process: str, title: str = "") -> str:
    stem = (process or "").lower().replace(".exe", "")
    if stem in _APP_NAMES:
        return _APP_NAMES[stem]
    for k, v in _APP_NAMES.items():
        if stem.startswith(k):
            return v
    return (process or "").replace(".exe", "") or (title.split(" - ")[-1] if title else "an app")


def _window_dict(w) -> Dict[str, Any]:
    return {"title": w.title, "app": app_name(w.process, w.title), "process": w.process, "monitor": w.monitor,
            "rect": [w.left, w.top, w.width, w.height], "minimized": w.minimized, "maximized": w.maximized,
            "focused": w.foreground}


def _elements_for(hwnd: int, limit: int = 60) -> Dict[str, Any]:
    """Buttons, links, fields, menus, tabs and text of one window (UI Automation)."""
    from modules.desktop import uia
    out: Dict[str, Any] = {"buttons": [], "links": [], "inputs": [], "menus": [], "tabs": [], "dialogs": [],
                           "text": []}
    with uia.in_window(hwnd):
        els = uia.list_elements(limit=limit * 2, interactive_only=True)["elements"]
        text = uia.read_text(limit=25)["text"]
    kinds = {"Button": "buttons", "SplitButton": "buttons", "CheckBox": "buttons", "RadioButton": "buttons",
             "Hyperlink": "links", "Edit": "inputs", "ComboBox": "inputs", "Document": "inputs",
             "MenuItem": "menus", "TabItem": "tabs", "ListItem": "links", "TreeItem": "links"}
    chrome = {"minimize", "maximize", "restore", "close", "restore down", "tab island handle", "system",
              "new tab", "back", "forward", "reload", "tab search"}
    for e in els:
        key = kinds.get(e["type"])
        if (e.get("name") or "").strip().lower() in chrome:
            continue
        if key and e.get("name") and e["name"] not in out[key] and len(out[key]) < limit // 3:
            out[key].append(e["name"])
        elif key == "inputs" and not e.get("name") and "text field" not in out["inputs"]:
            out["inputs"].append("text field")
    seen = set(out["links"]) | set(out["buttons"]) | set(out["tabs"])
    out["text"] = [t for t in text if t not in seen][:20]
    return out


def screen_context(include_elements: bool = True, monitor=None) -> Dict[str, Any]:
    """What is on screen, from the OS (no pixels needed): monitors (position,
    resolution, scaling), every visible window grouped by monitor with its
    position/size/state, which one is focused, and — for the window the user
    is looking at (or the front window of the requested monitor) — its
    buttons, links, fields, menus, tabs and visible text (UI Automation)."""
    from modules.desktop.controller import desktop
    if not config.get("vision.allow_screen_context", True):
        raise ToolError("Screen reading is turned off in Settings > Vision.", "DISABLED")
    ctx: Dict[str, Any] = {}
    wins = [w for w in desktop.list_windows() if not desktop._is_own(w)]
    mons = desktop.monitors()
    ctx["monitors"] = [{"index": m.index, "label": desktop.monitor_label(m), "width": m.width, "height": m.height,
                        "primary": m.primary, "position": m.position, "scale": m.scale,
                        "windows": [_window_dict(w) for w in wins if w.monitor == m.index and not w.minimized][:12]}
                       for m in mons]
    ctx["minimized"] = [_window_dict(w) for w in wins if w.minimized][:10]
    focus = desktop.target_window(wins)
    target = None
    if monitor not in (None, "", "all"):
        mon = desktop.resolve_monitor(monitor, focus.monitor if focus else 1)
        ctx["monitor"] = mon.index
        ctx["monitor_label"] = desktop.monitor_label(mon)
        on = [w for w in wins if w.monitor == mon.index and not w.minimized]
        target = on[0] if on else None               # front-most window on that screen
    else:
        target = focus
    ctx["active_window"] = _window_dict(focus) if focus else None
    ctx["subject_window"] = _window_dict(target) if target else None
    # Compatibility with older callers
    ctx["open_windows"] = [_window_dict(w) for w in wins[:25]]
    ctx["active_window_elements"] = []
    if include_elements and target is not None:
        try:
            ctx["ui"] = _elements_for(target.hwnd)
            from modules.desktop import uia
            with uia.in_window(target.hwnd):
                ctx["active_window_elements"] = uia.list_elements(limit=40)["elements"]
        except ToolError as e:
            ctx["elements_error"] = str(e)
        except Exception as e:  # UIA on an exotic app — keep the window-level answer
            log.debug("screen.elements_failed %s", e)
            ctx["elements_error"] = "That window doesn't expose its controls to Windows."
    event_bus.emit_event(EventType.SCREEN_CAPTURED, {"kind": "context", "monitor": ctx.get("monitor", "all")})
    return ctx


def _short_title(w: Dict[str, Any]) -> str:
    title, app = w.get("title", ""), w.get("app", "")
    t = re.sub(r"\s*[-–—|]\s*(Opera|Google Chrome|Microsoft​? Edge|Mozilla Firefox|Brave)$", "", title).strip()
    t = re.sub(r"^\(\d+\)\s*", "", t)                     # "(491) YouTube" unread counters
    if not t or t.lower() == app.lower():
        return app
    if len(t) > 60:
        t = t[:60].rsplit(" ", 1)[0].rstrip(" :,-") + "…"
    return f"{app} showing {t}" if app.lower() not in t.lower() else t


def describe_context(ctx: Dict[str, Any], question: str = "") -> str:
    """Plain-language summary of a screen_context() result (no internals).

    Voice-first: ONE short sentence with the front window + at most a couple
    of others. The verbose variant (with buttons, headline, tabs, dialogs) is
    only emitted when the caller asks for it via ``question='detail'``.
    """
    detailed = bool(question) and re.search(r"\bdetail|verbose|everything|full\b", question, re.I)
    parts = []
    if ctx.get("monitor"):
        mon = next((m for m in ctx["monitors"] if m["index"] == ctx["monitor"]), None)
        label = ctx.get("monitor_label", "that screen")
        wins = mon["windows"] if mon else []
        if not wins:
            return f"Nothing is open on {label} — desktop."
        front = _short_title(wins[0])
        others = [_short_title(w) for w in wins[1:3]]
        summary = f"On {label}, {front}"
        if others:
            summary += f" and {', '.join(others)}"
        parts.append(summary)
    else:
        aw = ctx.get("subject_window")
        if not aw:
            return "I can't tell which window you're looking at."
        parts.append(f"You're on {_short_title(aw)}")
        if detailed:
            groups = []
            for m in ctx.get("monitors", []):
                names = [_short_title(w) for w in m["windows"] if w["title"] != aw["title"]][:3]
                if names:
                    where = f" on {m['label']}" if len(ctx["monitors"]) > 1 else ""
                    groups.append(", ".join(names) + where)
            if groups:
                parts.append("also open: " + "; ".join(groups))
    if detailed:
        ui = ctx.get("ui") or {}
        subject = ctx.get("subject_window") or {}
        is_browser = subject.get("app") in ("Opera", "Chrome", "Edge", "Firefox", "Brave")
        if ui.get("tabs") and len(ui["tabs"]) > 1:
            tabs = [_short_title({"title": t, "app": ""}).strip() for t in ui["tabs"][:3]]
            parts.append(f"{len(ui['tabs'])} tabs including " + ", ".join(tabs))
        if ui.get("dialogs"):
            parts.append("dialog: " + ", ".join(ui["dialogs"][:2]))
        tabs_seen = {x.lower() for x in ui.get("tabs", [])}
        headline = [t for t in ui.get("text", []) if len(t) > 12 and t.lower() not in tabs_seen][:1]
        if headline:
            parts.append("page says " + "; ".join(headline))
        if ui.get("buttons") and not is_browser:
            parts.append("buttons: " + ", ".join(ui["buttons"][:4]))
    if ctx.get("elements_error") and detailed:
        parts.append("no detailed view — " + ctx["elements_error"].rstrip(".").lower())
    text = ". ".join((p[0].upper() + p[1:]).rstrip(". ") for p in parts if p) + "."
    return text


def screen_summary(monitor=None) -> str:
    return describe_context(screen_context(True, monitor=monitor))


_VISION_RULES = ("You are looking at a screenshot of the user's desktop. Describe only what is clearly "
                 "visible: applications, windows, readable text, buttons and fields. Never guess. If something "
                 "is unreadable or you can't tell, say so plainly.")


class VisualAnalyzer:
    """Image understanding backend (pluggable): Ollama vision models or FLUX.2 Klein 4B."""

    _auto_model: Tuple[float, str] = (0.0, "")
    _flux = None                        # cached FluxAnalyzer instance

    def _backend(self) -> str:
        b = (config.get("vision.analyzer", "none") or "none").lower()
        return b if b in ("none", "ollama", "flux") else "none"

    def _model(self) -> str:
        """Configured vision model, or an installed vision-capable Ollama model."""
        if config.get("vision.analyzer", "none") == "ollama" and config.get("vision.model", ""):
            return config.get("vision.model")
        ts, cached = self._auto_model
        if time.time() - ts < 60:
            return cached
        found = ""
        base = config.get("ai.base_url", "http://localhost:11434").replace("localhost", "127.0.0.1")
        try:
            tags = requests.get(base.rstrip("/") + "/api/tags", timeout=3).json().get("models", [])
            for m in tags[:15]:
                if "vision" in (self._capabilities(m.get("name", "")) or []):
                    found = m["name"]
                    break
        except (requests.RequestException, ValueError):
            pass
        self._auto_model = (time.time(), found)
        return found

    def available(self) -> Tuple[bool, str]:
        backend = self._backend()
        if backend == "flux":
            return self._flux_backend().available()
        model = self._model()
        if not model:
            return False, ("No vision model is installed, so I can only use what Windows reports about "
                           "the window. Pull one in Ollama (e.g. 'ollama pull llama3.2-vision') or "
                           "enable FLUX.2 Klein 4B in Settings > Vision.")
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

    def _flux_backend(self):
        if self._flux is None:
            from modules.vision.flux import FluxAnalyzer
            self._flux = FluxAnalyzer()
        return self._flux

    def analyze(self, image, question: str) -> str:
        if self._backend() == "flux":
            return self._flux_backend().analyze(image, question, _VISION_RULES)
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
                "model": self._model(), "stream": False,
                "messages": [{"role": "system", "content": _VISION_RULES},
                             {"role": "user", "content": question, "images": [b64]}]})
        except requests.RequestException as e:
            raise ToolError(f"The vision model didn't respond: {e}", "VISION_FAILED")
        if r.status_code != 200:
            raise ToolError(f"The vision model returned HTTP {r.status_code}.", "VISION_FAILED")
        return (r.json().get("message") or {}).get("content", "").strip()


visual_analyzer = VisualAnalyzer()

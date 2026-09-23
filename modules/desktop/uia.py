"""
modules/desktop/uia.py

Windows UI Automation helpers: read the accessibility tree of the foreground
window and act on real UI elements ("type into the search box", "click
Send"). This is genuine element-level understanding from the OS, not image
guessing. Apps that do not expose accessibility info (some games, canvas UIs)
simply return fewer elements.
"""

import logging
import re
import threading
from typing import Dict, List, Optional

from modules.automation.tools import ToolError

log = logging.getLogger("saint.desktop.uia")

_INPUT_TYPES = {"EditControl", "ComboBoxControl", "DocumentControl"}
_CLICK_TYPES = {"ButtonControl", "HyperlinkControl", "MenuItemControl", "TabItemControl",
                "ListItemControl", "CheckBoxControl", "RadioButtonControl", "TreeItemControl",
                "SplitButtonControl", "TextControl", "ImageControl"}

# comtypes/UIA calls must be initialised per thread.
_tls = threading.local()


def _auto():
    try:
        import uiautomation as auto
    except Exception as e:
        raise ToolError(f"UI Automation isn't available ({e}). Install 'uiautomation'.", "UNSUPPORTED")
    if not getattr(_tls, "init", False):
        try:
            auto.InitializeUIAutomationInCurrentThread()
        except Exception:
            pass
        _tls.init = True
    return auto


def _words(s: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in {"the", "a", "box", "field", "bar"}]


def _walk(root, max_depth=18, limit=1500):
    out = []

    def rec(ctrl, depth):
        if len(out) >= limit or depth > max_depth:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for c in children:
            out.append(c)
            rec(c, depth + 1)

    rec(root, 0)
    return out


def _rect(c) -> Optional[Dict[str, int]]:
    try:
        r = c.BoundingRectangle
        if r.width() <= 0 or r.height() <= 0:
            return None
        return {"left": r.left, "top": r.top, "width": r.width(), "height": r.height()}
    except Exception:
        return None


def list_elements(limit: int = 60, interactive_only: bool = True) -> Dict:
    """Describe the foreground window's visible UI elements."""
    auto = _auto()
    win = auto.GetForegroundControl()
    if win is None:
        raise ToolError("There's no active window.", "NOT_FOUND")
    top = win.GetTopLevelControl() or win
    elements = []
    for c in _walk(top):
        try:
            ctype = c.ControlTypeName
            name = (c.Name or "").strip()
        except Exception:
            continue
        if interactive_only and ctype not in _INPUT_TYPES | _CLICK_TYPES:
            continue
        if not name and ctype not in _INPUT_TYPES:
            continue
        r = _rect(c)
        if r is None:
            continue
        elements.append({"type": ctype.replace("Control", ""), "name": name[:80],
                         "automation_id": (getattr(c, "AutomationId", "") or "")[:40], "rect": r})
        if len(elements) >= limit:
            break
    return {"window": top.Name, "elements": elements}


def _score(c, want: List[str], types) -> float:
    try:
        if c.ControlTypeName not in types:
            return 0.0
        hay = " ".join([c.Name or "", getattr(c, "AutomationId", "") or "",
                        getattr(c, "LocalizedControlType", "") or "",
                        getattr(c, "HelpText", "") or ""]).lower()
    except Exception:
        return 0.0
    if not want:
        return 0.1
    hits = sum(1 for w in want if w in hay)
    return hits / len(want)


def _find(target: str, types) -> Optional[object]:
    auto = _auto()
    win = auto.GetForegroundControl()
    if win is None:
        return None
    top = win.GetTopLevelControl() or win
    want = _words(target)
    best, best_score = None, 0.0
    for c in _walk(top):
        s = _score(c, want, types)
        if s > best_score and _rect(c) is not None:
            best, best_score = c, s
    return best if best_score >= 0.5 else None


def focus_input(target: str) -> str:
    """Focus an input element (e.g. 'search box') in the foreground window."""
    el = _find(target, _INPUT_TYPES)
    if el is None:
        raise ToolError(f"I can't see a '{target}' in the active window.", "ELEMENT_NOT_FOUND")
    try:
        el.SetFocus()
    except Exception:
        el.Click(simulateMove=False)
    return (el.Name or target)[:60]


def click_element(name: str) -> Dict:
    el = _find(name, _CLICK_TYPES | _INPUT_TYPES)
    if el is None:
        raise ToolError(f"I can't see anything called '{name}' in the active window.", "ELEMENT_NOT_FOUND")
    r = _rect(el)
    try:
        invoke = el.GetInvokePattern()
        invoke.Invoke()
        how = "invoke"
    except Exception:
        el.Click(simulateMove=False)
        how = "click"
    return {"clicked": (el.Name or name)[:60], "type": el.ControlTypeName, "method": how, "rect": r}

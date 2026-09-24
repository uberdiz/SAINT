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
import time
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


_STOP_WORDS = {"the", "a", "an", "box", "field", "bar", "in", "on", "at", "of", "my", "to", "for", "please"}


def _words(s: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _STOP_WORDS]


def _clean_name(name: str) -> str:
    """Taskbar buttons read "Settings pinned" / "Spotify - 1 running window pinned"."""
    n = re.sub(r"\s+pinned$", "", (name or "").strip(), flags=re.I)
    return re.sub(r"\s+-\s+\d+\s+running windows?$", "", n, flags=re.I)


def _walk(root, max_depth=40, limit=3000):
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


_window_override = threading.local()


class in_window:
    """Context manager: run UIA helpers against a specific window handle."""

    def __init__(self, hwnd: Optional[int]):
        self.hwnd = hwnd

    def __enter__(self):
        self._prev = getattr(_window_override, "hwnd", None)
        _window_override.hwnd = self.hwnd
        return self

    def __exit__(self, *exc):
        _window_override.hwnd = self._prev


def _top(activate: bool = False):
    """Root control of the window commands act on (see DesktopController.target_window)."""
    auto = _auto()
    try:
        from modules.desktop.controller import desktop
        hwnd = getattr(_window_override, "hwnd", None)
        if hwnd:
            w = desktop._info(hwnd)
            if activate and not w.foreground:
                w = desktop._activate(w)
        else:
            w = desktop.target_window(activate=activate)
        if w is not None:
            ctrl = auto.ControlFromHandle(w.hwnd)
            if ctrl is not None:
                return ctrl
    except ToolError:
        raise
    except Exception as e:
        log.debug("uia.target_window_failed %s", e)
    win = auto.GetForegroundControl()
    if win is None:
        raise ToolError("There's no active window.", "NOT_FOUND")
    return win.GetTopLevelControl() or win


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
    top = _top()
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


# Words that say what *kind* of element is meant. They narrow the control
# types instead of being matched against labels: otherwise "settings button"
# half-matches every button (its control type is "button") — e.g. Minimize.
_ROLE_TYPES = {
    "button": {"ButtonControl", "SplitButtonControl", "MenuItemControl"},
    "buttons": {"ButtonControl", "SplitButtonControl", "MenuItemControl"},
    "link": {"HyperlinkControl"}, "links": {"HyperlinkControl"},
    "icon": {"ListItemControl", "ButtonControl", "ImageControl", "TreeItemControl"},
    "tab": {"TabItemControl", "ButtonControl"},
    "menu": {"MenuItemControl", "ButtonControl", "SplitButtonControl"},
    "checkbox": {"CheckBoxControl"}, "toggle": {"ButtonControl", "CheckBoxControl"},
    "switch": {"ButtonControl", "CheckBoxControl"}, "option": {"RadioButtonControl", "ListItemControl",
                                                              "MenuItemControl", "CheckBoxControl"},
    "item": set(), "thing": set(), "element": set(),
}


def _target_words(target: str):
    """'the settings button' -> (['settings'], {button types}); types may be None."""
    words = _words(target)
    types = set()
    content = []
    for w in words:
        if w in _ROLE_TYPES:
            types |= _ROLE_TYPES[w]
        else:
            content.append(w)
    # "the X button" is the close button; spoken names for common icons.
    content = [{"x": "close", "minus": "minimize", "dots": "more", "cog": "settings", "gear": "settings"}.get(w, w)
               for w in content]
    return content, (types or None)


def _is_exact(el, target: str) -> bool:
    """The element's label is exactly what was asked for ("Settings", not
    "Dictation settings")."""
    try:
        return bool(el is not None and _words(_clean_name(el.Name)) == _target_words(target)[0])
    except Exception:
        return False


def _word_hit(w: str, hay: str, hay_words: List[str]) -> bool:
    if w in hay:
        return True
    # Loose stem: "recycling" ~ "recycle", "settings" ~ "setting".
    return len(w) >= 5 and any(len(h) >= 5 and h[:5] == w[:5] for h in hay_words)


def _score(c, want: List[str], types) -> float:
    try:
        if c.ControlTypeName not in types:
            return 0.0
        hay = " ".join([c.Name or "", getattr(c, "AutomationId", "") or "",
                        getattr(c, "HelpText", "") or ""]).lower()
    except Exception:
        return 0.0
    if not want:
        return 0.1
    hay_words = re.findall(r"[a-z0-9]+", hay)
    hits = sum(1 for w in want if _word_hit(w, hay, hay_words))
    return hits / len(want)


def _in(r, box) -> bool:
    return bool(r and box and r["left"] >= box["left"] and r["top"] >= box["top"]
                and r["left"] + r["width"] <= box["left"] + box["width"]
                and r["top"] + r["height"] <= box["top"] + box["height"])


def _find_once(top, target: str, types) -> Optional[object]:
    want, role_types = _target_words(target)
    if role_types and role_types & set(types):
        types = role_types & set(types)
    if not want:
        return None                       # "the button" alone names nothing
    ctrls = _walk(top)
    # Web page content lives in a DocumentControl; prefer it over browser
    # chrome ("Search" on YouTube vs Chrome's "Address and search bar").
    doc = next((_rect(c) for c in ctrls if _safe_type(c) == "DocumentControl" and _rect(c)), None)
    best, best_score = None, 0.0
    for c in ctrls:
        s = _score(c, want, types)
        if s <= 0:
            continue
        r = _rect(c)
        if r is None:
            continue
        try:
            extra = len(set(_words(_clean_name(c.Name))) - set(want))
            if want and extra == 0:
                s += 0.3
            else:
                s -= min(0.2, 0.05 * extra)       # prefer "Settings" over "Dictation settings"
        except Exception:
            pass
        if _in(r, doc):
            s += 0.2
        if s > best_score:
            best, best_score = c, s
    return best if best_score >= 0.5 else None


def _safe_type(c) -> str:
    try:
        return c.ControlTypeName
    except Exception:
        return ""


_SCOPE = re.compile(r"\s+(?:in|on|from)\s+(?:my|the)\s+(task\s?bar|system tray|tray|desktop|home screen)$")


def _split_scope(spec: str):
    """'settings in my taskbar' -> ('settings', 'taskbar')."""
    m = _SCOPE.search((spec or "").strip().lower())
    if not m:
        return spec, None
    where = "taskbar" if m.group(1).replace(" ", "") in ("taskbar", "systemtray", "tray") else "desktop"
    return spec.strip()[:m.start()].strip(), where


_SCOPE_LABEL = {"taskbar": "the taskbar", "desktop": "your desktop"}


def _scope_root(scope: str):
    """Root control of the taskbar or the desktop icons (None if unavailable)."""
    auto = _auto()
    try:
        if scope == "taskbar":
            c = auto.PaneControl(searchDepth=1, ClassName="Shell_TrayWnd")
            return c if c.Exists(0, 0) else None
        # The icons live in Progman (or a WorkerW) > SHELLDLL_DefView > SysListView32.
        for cls in ("Progman", "WorkerW"):
            root = auto.PaneControl(searchDepth=1, ClassName=cls)
            if not root.Exists(0, 0):
                continue
            icons = root.ListControl(searchDepth=3, ClassName="SysListView32")
            if icons.Exists(0, 0):
                return icons
    except Exception as e:
        log.debug("uia.scope_root_failed %s %s", scope, e)
    return None


def _desktop_icon_covered(x: int, y: int) -> bool:
    """Is another window on top of the desktop icon at (x, y)?"""
    try:
        import win32gui
        hwnd = win32gui.GetAncestor(win32gui.WindowFromPoint((x, y)), 2)   # GA_ROOT
        return win32gui.GetClassName(hwnd) not in ("Progman", "WorkerW")
    except Exception:
        return False


def _find(target: str, types, wait: float = 2.5, activate: bool = True, top=None) -> Optional[object]:
    """Find an element in the target window, polling briefly while a page loads."""
    top = top if top is not None else _top(activate=activate)
    deadline = time.monotonic() + wait
    while True:
        el = _find_once(top, target, types)
        if el is not None or time.monotonic() >= deadline:
            return el
        time.sleep(0.3)


_ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
             "fifth": 5, "5th": 5, "top": 1, "last": -1}


_NAV_LINK = re.compile(r"new content available|^(show more|show less|home|shorts|subscriptions|upload|you|history|"
                       r"library|explore|trending|view channel|mix|youtube music|sign in|settings|skip navigation|"
                       r"all|images|videos|news|shopping|maps|more|tools|next|previous)$", re.I)
_VIDEO_HINT = re.compile(r"\b\d+\s*(?:hours?|minutes?|seconds?)\b|\b\d[\d.,]*[kmb]?\s+views?\b|\b\d+:\d{2}\b", re.I)


def _visible_results(top, kind: str = "result"):
    """Result links the user can actually see, in reading order.

    Visible = inside the window (carousels keep off-screen items in the
    tree). Page chrome is skipped: the left sidebar, header, and navigation
    labels. For videos, a duration/views text is required when any link has
    one (YouTube, Twitch, Vimeo all label result links that way).
    """
    box = _rect(top)
    ctrls = _walk(top, max_depth=45, limit=6000)
    links, seen = [], set()
    for c in ctrls:
        if _safe_type(c) != "HyperlinkControl":
            continue
        try:
            name = " ".join((c.Name or "").split())
        except Exception:
            continue
        r = _rect(c)
        if not r or not name or name in seen or _NAV_LINK.search(name):
            continue
        if box and not _in(r, box):
            continue                                  # scrolled away / off-screen carousel item
        if box and r["left"] < box["left"] + 0.16 * box["width"] and r["width"] < 0.25 * box["width"]:
            continue                                  # left navigation sidebar
        if box and r["top"] < box["top"] + 90:
            continue                                  # browser/site header
        if len(name) < 12 or len(name.split()) < 2:
            continue
        seen.add(name)
        links.append((r["top"], r["left"], name, c))
    if kind == "video":
        vids = [x for x in links if _VIDEO_HINT.search(x[2])]
        if vids:
            links = vids
    # reading order with a row tolerance (items in one row share roughly a top)
    links.sort(key=lambda x: (x[0] // 40, x[1]))
    return links


def _nth_result(n: int, wait: float = 4.0, kind: str = "result") -> Optional[object]:
    """The n-th visible result (search result, video, article) in the target
    window, waiting briefly for a page that is still loading. n = -1: last."""
    top = _top(activate=True)
    deadline = time.monotonic() + wait
    while True:
        links = _visible_results(top, kind)
        if links and (len(links) >= n or n == -1):
            return links[n if n == -1 else n - 1][3]
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.4)


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


_REGION = re.compile(r"^(?:the\s+)?(?:(button|link|icon|thing|one|item)\s+)?(?:in|at|on)\s+(?:the\s+)?"
                     r"(top|bottom|upper|lower|middle|center|centre)?\s*-?\s*(left|right|middle|center|centre)?"
                     r"(?:\s+(?:corner|side|edge))?(?:\s+of\s+the\s+(?:screen|window|page))?$")


def _snapshot(top) -> tuple:
    """Cheap state fingerprint used to verify that an action changed the UI."""
    try:
        title = top.Name or ""
    except Exception:
        title = ""
    try:
        auto = _auto()
        focused = auto.GetFocusedControl()
        fname = (focused.Name or "", focused.ControlTypeName) if focused else ("", "")
    except Exception:
        fname = ("", "")
    return title, fname


# "the X button in the bottom right" / "the settings icon at the top": a
# named element, nearest the named part of the window.
_NAMED_REGION = re.compile(r"^(.+?)\s+(?:in|at|on|near)\s+(?:the\s+)?(top|bottom|upper|lower|middle|center|centre)?"
                           r"\s*-?\s*(left|right|middle|center|centre)?(?:\s+(?:corner|side|edge|part))?"
                           r"(?:\s+of\s+the\s+(?:screen|window|page))?$")


def _split_region(spec: str):
    """'x button in the bottom right' -> ('x button', 'bottom', 'right'); None if no region."""
    m = _NAMED_REGION.match((spec or "").strip().lower())
    if not m or not (m.group(2) or m.group(3)) or _REGION.match((spec or "").strip().lower()):
        return None
    return m.group(1), m.group(2) or "", m.group(3) or ""


def _element_by_region(top, spec: str):
    """'the button in the bottom right' -> clickable element nearest that spot;
    'the X button in the bottom right' -> the element named X nearest it."""
    named = _split_region(spec)
    if named:
        name, vword, hword = named
    else:
        m = _REGION.match(spec.strip().lower())
        if not m or not (m.group(2) or m.group(3)):
            return None
        name, vword, hword = "", m.group(2) or "", m.group(3) or ""
        role = m.group(1) or ""
    box = _rect(top)
    if not box:
        return None
    v = {"top": 0.0, "upper": 0.15, "bottom": 1.0, "lower": 0.85}.get(vword, 0.5)
    h = {"left": 0.0, "right": 1.0}.get(hword, 0.5)
    tx, ty = box["left"] + h * box["width"], box["top"] + v * box["height"]
    if name:
        want, role_types = _target_words(name)
        types = role_types or _CLICK_TYPES
        if not want:
            named = None
    if not named:
        want, types = [], {"link": {"HyperlinkControl"}}.get(role if not name else "",
                                                          {"ButtonControl", "SplitButtonControl",
                                                           "HyperlinkControl", "MenuItemControl"})
    best, best_d = None, None
    for c in _walk(top, limit=3000):
        if _safe_type(c) not in types:
            continue
        if want and _score(c, want, types) < 0.5:
            continue                      # named: only elements that match the name
        r = _rect(c)
        if not r or not _in(r, box):
            continue
        cx, cy = r["left"] + r["width"] / 2, r["top"] + r["height"] / 2
        d = (cx - tx) ** 2 + (cy - ty) ** 2
        if best_d is None or d < best_d:
            best, best_d = c, d
    return best


def find_element(name: str, activate: bool = True):
    """Resolve a spoken element reference in the target window."""
    spec = (name or "").strip().lower()
    m = re.match(r"^(?:the\s+)?(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|top|last)\s+"
                 r"(?:search\s+)?(result|video|link|item|one|song|article|post|clip)s?$", spec)
    if m:
        kind = "video" if m.group(2) in ("video", "clip", "song") else "result"
        return _nth_result(_ORDINALS[m.group(1)], kind=kind)
    if _REGION.match(spec) or _split_region(spec):
        return _element_by_region(_top(activate=activate), spec)
    return _find(name, _CLICK_TYPES | _INPUT_TYPES, activate=activate)


def _find_anywhere(name: str, activate: bool, wait: float = 2.5):
    """(element, root, scope): the named element in the target window, or on
    the taskbar / desktop when the user said so ("…in my taskbar") or when the
    window doesn't have it ("the recycle bin" lives on the desktop)."""
    spec, scope = _split_scope(name)
    if scope:
        root = _scope_root(scope)
        if root is None:
            raise ToolError(f"I can't read {_SCOPE_LABEL[scope]} right now.", "UNSUPPORTED")
        return _find(spec, _CLICK_TYPES | _INPUT_TYPES, wait=0.5, top=root), root, scope
    top = _top(activate=activate)
    low = spec.strip().lower()
    if _REGION.match(low) or _split_region(low) or re.match(
            r"^(?:the\s+)?(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|top|last)\s", low):
        return find_element(spec, activate=activate), top, None
    el = _find(spec, _CLICK_TYPES | _INPUT_TYPES, wait=wait, activate=activate, top=top)
    if el is not None and _is_exact(el, spec):
        return el, top, None
    # Not in the window, or only a partial match there ("Dictation settings"
    # for "settings"): an exact match on the desktop / taskbar wins.
    for fallback in ("desktop", "taskbar"):
        root = _scope_root(fallback)
        other = _find(spec, _CLICK_TYPES, wait=0, top=root) if root is not None else None
        if other is not None and (el is None or _is_exact(other, spec)):
            log.info("uia.found_elsewhere %r in %s (window match: %r)", spec, fallback,
                     getattr(el, "Name", None))
            return other, root, fallback
    return el, top, None


def click_element(name: str, action: str = "click") -> Dict:
    """Click (or double/right-click, hover) a named element and report whether
    the UI changed afterwards (title / focus), so success is not assumed."""
    import pyautogui
    el, top, scope = _find_anywhere(name, activate=True)
    where = _SCOPE_LABEL.get(scope) or top.Name or "the active window"
    if el is None:
        raise ToolError(f"I can't see anything called '{name}' in {where}.", "ELEMENT_NOT_FOUND")
    before = _snapshot(top)
    r = _rect(el)
    if r is None:
        raise ToolError(f"'{name}' isn't visible on screen right now.", "ELEMENT_NOT_FOUND")
    x, y = r["left"] + r["width"] // 2, r["top"] + r["height"] // 2
    if scope == "desktop" and _desktop_icon_covered(x, y):
        # Windows cover the icon: show the desktop so the click lands on it.
        pyautogui.hotkey("win", "d")
        time.sleep(0.6)
        r = _rect(el) or r
        x, y = r["left"] + r["width"] // 2, r["top"] + r["height"] // 2
    label = re.sub(r"\s+\d+\s*(?:hours?|minutes?|seconds?)(?:,?\s*\d+\s*(?:minutes?|seconds?))*$", "",
                   " ".join(_clean_name(el.Name or name).split()))[:70]
    how = action
    if action == "click":
        try:
            el.GetInvokePattern().Invoke()
            how = "invoke"
        except Exception:
            pyautogui.click(x, y)
    elif action == "double_click":
        pyautogui.doubleClick(x, y, interval=0.08)
    elif action == "right_click":
        pyautogui.rightClick(x, y)
    elif action == "middle_click":
        pyautogui.middleClick(x, y)
    elif action == "hover":
        pyautogui.moveTo(x, y, duration=0.15)
    else:
        raise ToolError(f"Unknown element action '{action}'.", "INVALID")
    try:
        from modules.agent.context import desktop_context
        desktop_context.note_element(label, x, y)
    except Exception:
        pass
    changed = False
    if action in ("click", "double_click"):
        deadline = time.monotonic() + (4.0 if _safe_type(el) == "HyperlinkControl" else 1.2)
        while time.monotonic() < deadline:
            time.sleep(0.2)
            if _snapshot(top) != before:
                changed = True
                break
    return {"clicked": label, "type": _safe_type(el).replace("Control", ""), "method": how,
            "rect": r, "x": x, "y": y, "changed": changed,
            "window": _SCOPE_LABEL.get(scope) or top.Name or "", "scope": scope or ""}


def read_text(limit: int = 40) -> Dict:
    """Visible text of the target window, top-to-bottom (for "read the screen")."""
    top = _top()
    seen, rows = set(), []
    for c in _walk(top, limit=4000):
        if _safe_type(c) not in ("TextControl", "HyperlinkControl", "HeaderControl", "HeaderItemControl"):
            continue
        try:
            name = " ".join((c.Name or "").split())
        except Exception:
            continue
        r = _rect(c)
        if not r or len(name) < 2 or name in seen:
            continue
        seen.add(name)
        rows.append((r["top"], r["left"], name[:200]))
    rows.sort()
    return {"window": top.Name, "text": [t for _, _, t in rows[:limit]]}


def locate(name: str) -> Dict:
    """Where a named element is: screen coordinates and a plain-words position."""
    el, top, scope = _find_anywhere(name, activate=False, wait=0.5)
    if el is None:
        raise ToolError(f"I can't see a '{name}' in {top.Name or 'the active window'}.", "ELEMENT_NOT_FOUND")
    r = _rect(el)
    box = _rect(top) if not scope else None
    x, y = r["left"] + r["width"] // 2, r["top"] + r["height"] // 2
    where = "" if scope else "middle"
    if box:
        fx = (x - box["left"]) / max(1, box["width"])
        fy = (y - box["top"]) / max(1, box["height"])
        v = "top" if fy < 0.33 else "bottom" if fy > 0.67 else "middle"
        h = "left" if fx < 0.33 else "right" if fx > 0.67 else "center"
        where = "center" if (v, h) == ("middle", "center") else f"{v} {h}" if v != "middle" else f"{h} side"
    try:
        from modules.agent.context import desktop_context
        desktop_context.note_element(_clean_name(el.Name or name)[:60], x, y)
    except Exception:
        pass
    return {"name": _clean_name(el.Name or "")[:60], "type": _safe_type(el).replace("Control", ""), "x": x, "y": y,
            "rect": r, "where": where, "window": _SCOPE_LABEL.get(scope) or top.Name or ""}

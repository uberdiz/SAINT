"""
ui/placement.py

Which monitor SAINT's own windows (main window, mini player, overlay) go on.

Monitors are numbered the way the rest of SAINT numbers them
(modules/desktop/controller.py: left to right, "main" = primary), and a
Windows monitor is matched to its Qt screen by its top-left corner (Qt keeps
each screen's origin in native pixels; QScreen.name() is the monitor model),
so positions stay right with different scaling on each monitor.

While gaming, SAINT keeps its windows off the game's monitor: the monitor in
``game_mode.saint_monitor``, or ("auto") the biggest monitor without the game.
"""

import logging
from typing import List, Optional

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QGuiApplication, QScreen

from core.config import config

log = logging.getLogger("saint.ui.placement")


def screens() -> List[QScreen]:
    """Qt screens ordered like SAINT's monitor numbers (left to right, then top)."""
    return sorted(QGuiApplication.screens(), key=lambda s: (s.geometry().left(), s.geometry().top()))


def _monitor_origin(rect):
    """Native top-left of the monitor showing most of a (l, t, r, b) rectangle."""
    try:
        import win32api
        hmon = win32api.MonitorFromRect(tuple(int(v) for v in rect), 2)        # MONITOR_DEFAULTTONEAREST
        m = win32api.GetMonitorInfo(hmon).get("Monitor")
        return (int(m[0]), int(m[1])) if m else None
    except Exception:
        return None


def screen_of_rect(rect) -> Optional[QScreen]:
    """The Qt screen showing a native (l, t, r, b) rectangle, e.g. a game window."""
    origin = _monitor_origin(rect)
    for s in screens():
        if origin and (s.geometry().left(), s.geometry().top()) == origin:
            return s
    l, t, r, b = rect
    return QGuiApplication.screenAt(QPoint((l + r) // 2, (t + b) // 2))


def screen_for(target) -> Optional[QScreen]:
    """'2', 'second', 'left', 'main', 'other' -> a Qt screen (None if there's no such monitor)."""
    try:
        from modules.desktop.controller import desktop
        cur = 1
        m = desktop.resolve_monitor(target, cur)
        return screen_of_rect((m.left, m.top, m.right, m.bottom))
    except Exception:
        log.debug("placement.resolve_failed %r", target, exc_info=True)
    all_ = screens()
    t = str(target).strip().lower()
    if t.isdigit() and 1 <= int(t) <= len(all_):
        return all_[int(t) - 1]
    return None


def gaming_screen(game_rect) -> Optional[QScreen]:
    """Where SAINT goes while gaming, or None to stay put (one monitor, or no better one)."""
    all_ = screens()
    if len(all_) < 2:
        return None
    want = str(config.get("game_mode.saint_monitor", "auto") or "auto").strip().lower()
    if want not in ("", "auto"):
        s = screen_for(want)
        if s is not None:
            return s
    game = screen_of_rect(game_rect) if game_rect else None
    others = [s for s in all_ if s is not game]
    if not others:
        return None
    if game is None:
        prim = QGuiApplication.primaryScreen()
        others = [s for s in others if s is not prim] or others
    return max(others, key=lambda s: s.geometry().width() * s.geometry().height())


def move_to_screen(widget, screen: Optional[QScreen], corner: str = "", inset: int = 12) -> bool:
    """Move a top-level widget onto ``screen``, keeping its relative spot
    (or into a corner: "bottom-right", "top-left", "left", "top", "center", ...),
    ``inset`` px from the edges. Returns True if it moved."""
    if screen is None or widget is None:
        return False
    area = screen.availableGeometry()
    cur = widget.screen() if hasattr(widget, "screen") else None
    geo = widget.frameGeometry()
    w, h = min(geo.width(), area.width()), min(geo.height(), area.height())
    if corner:
        cx, cy = area.left() + (area.width() - w) // 2, area.top() + (area.height() - h) // 2
        x = area.right() + 1 - w - inset if "right" in corner else area.left() + inset if "left" in corner else cx
        y = area.bottom() + 1 - h - inset if "bottom" in corner else area.top() + inset if "top" in corner else cy
    elif cur is not None and cur is not screen:
        src = cur.availableGeometry()
        fx = (geo.left() - src.left()) / max(1, src.width() - geo.width())
        fy = (geo.top() - src.top()) / max(1, src.height() - geo.height())
        x = area.left() + int(max(0.0, min(1.0, fx)) * max(0, area.width() - w))
        y = area.top() + int(max(0.0, min(1.0, fy)) * max(0, area.height() - h))
    else:
        x, y = geo.left(), geo.top()
    r = QRect(x, y, w, h)
    if not area.contains(r):
        x = max(area.left(), min(x, area.right() - w))
        y = max(area.top(), min(y, area.bottom() - h))
    if (x, y) == (geo.left(), geo.top()) and cur is screen:
        return False
    if widget.isMaximized():
        widget.showNormal()
    widget.move(x, y)
    return True


def nudge(widget, dx: int, dy: int) -> bool:
    """Move a window by (dx, dy) px ("right a bit"), kept on the screen it's on."""
    if widget is None or not (dx or dy):
        return False
    geo = widget.frameGeometry()
    screen = QGuiApplication.screenAt(geo.center()) or QGuiApplication.primaryScreen()
    area = screen.availableGeometry()
    x = max(area.left(), min(geo.left() + dx, area.right() + 1 - geo.width()))
    y = max(area.top(), min(geo.top() + dy, area.bottom() + 1 - geo.height()))
    if (x, y) == (geo.left(), geo.top()):
        return False
    widget.move(x, y)
    return True


def screen_number(screen: Optional[QScreen]) -> int:
    all_ = screens()
    return all_.index(screen) + 1 if screen in all_ else 0

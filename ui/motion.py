"""
ui/motion.py

Animation helpers. Every animation in the UI goes through here so the
Appearance > Animations switch turns all of it off in one place.
Opacity effects are removed when an animation finishes — leaving a
QGraphicsEffect on a widget makes Qt re-render it offscreen forever.
"""

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QTimer
from PySide6.QtWidgets import QGraphicsOpacityEffect

from core.config import config

FAST, BASE, SLOW = 120, 200, 320
EASE = QEasingCurve.OutCubic
SNAP = QEasingCurve.OutBack


def enabled() -> bool:
    return bool(config.get("appearance.animations", True))


def animate(target, prop: bytes, start, end, ms: int = BASE, curve=EASE, on_done=None):
    """Start a property animation (kept alive by its target). Jumps to the end if animations are off."""
    if not enabled() or ms <= 0:
        target.setProperty(prop.decode(), end)
        if on_done:
            on_done()
        return None
    anim = QPropertyAnimation(target, prop, target)
    anim.setDuration(ms)
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setEasingCurve(curve)
    if on_done:
        anim.finished.connect(on_done)
    anim.start(QPropertyAnimation.DeleteWhenStopped)
    return anim


def fade_in(widget, ms: int = BASE, delay: int = 0, dy: int = 0):
    """Fade (and optionally rise ``dy`` px) into place."""
    if not enabled():
        widget.show()
        return

    def go():
        eff = QGraphicsOpacityEffect(widget)
        eff.setOpacity(0.0)
        widget.setGraphicsEffect(eff)
        widget.show()
        animate(eff, b"opacity", 0.0, 1.0, ms, on_done=lambda: widget.setGraphicsEffect(None))
        if dy:
            end = widget.pos()
            animate(widget, b"pos", end + QPoint(0, dy), end, ms)
    QTimer.singleShot(delay, go) if delay else go()


def fade_out(widget, ms: int = FAST, on_done=None):
    def finish():
        widget.setGraphicsEffect(None)
        widget.hide()
        if on_done:
            on_done()
    if not enabled():
        finish()
        return
    eff = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(eff)
    animate(eff, b"opacity", 1.0, 0.0, ms, on_done=finish)


def stagger(widgets, ms: int = BASE, step: int = 40, dy: int = 10):
    for i, w in enumerate(widgets):
        fade_in(w, ms, delay=i * step, dy=dy)

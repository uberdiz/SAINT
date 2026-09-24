"""
ui/toast.py

Small notifications that slide in at the bottom-right of the window and
leave on their own.
"""

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from ui import icons, motion
from ui.theme import current_palette
from ui.widgets import IconButton

MAX = 3


class Toast(QFrame):
    def __init__(self, host, title, message, kind="info", icon=None):
        super().__init__(host.parent)
        self.setObjectName("Toast")
        self.host = host
        p = current_palette()
        color = {"ok": p.success, "error": p.danger, "warn": p.warning}.get(kind, p.accent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 11, 8, 11)
        lay.setSpacing(12)
        ic = QLabel()
        ic.setPixmap(icons.pixmap(icon or {"ok": "check", "error": "alert", "warn": "alert"}.get(kind, "sparkles"),
                                  color, 18))
        lay.addWidget(ic, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet("font-weight: 600;")
        col.addWidget(t)
        if message:
            m = QLabel(message)
            m.setObjectName("Muted")
            m.setWordWrap(True)
            col.addWidget(m)
        lay.addLayout(col, 1)
        x = IconButton("x", "Dismiss", 14)
        x.clicked.connect(self.dismiss)
        lay.addWidget(x, 0, Qt.AlignTop)
        self.setFixedWidth(340)
        self.adjustSize()
        QTimer.singleShot(5200 if kind == "error" else 4200, self.dismiss)

    def dismiss(self):
        if self in self.host.toasts:
            self.host.toasts.remove(self)
            motion.fade_out(self, motion.FAST, on_done=self.deleteLater)
            self.host.relayout()


class ToastHost:
    def __init__(self, parent):
        self.parent = parent
        self.toasts = []

    def show(self, title, message="", kind="info", icon=None):
        t = Toast(self, title, message, kind, icon)
        self.toasts.insert(0, t)
        while len(self.toasts) > MAX:
            self.toasts[-1].dismiss()
        self.relayout(new=t)
        t.show()
        t.raise_()
        motion.fade_in(t, motion.BASE)

    def relayout(self, new=None):
        y = self.parent.height() - 20
        for t in self.toasts:
            y -= t.height()
            target = QPoint(self.parent.width() - t.width() - 20, y)
            if t is new or not motion.enabled():
                t.move(target)
            else:
                motion.animate(t, b"pos", t.pos(), target, motion.BASE)
            t.raise_()
            y -= 10

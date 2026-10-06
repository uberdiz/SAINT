"""
ui/components/panel.py

The basic surface of the v0.4 interface: a titled panel with an optional hint and actions in its
header. ``active=True`` gives it the accent edge used while SAINT is working on something.
"""

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ui.design import tokens


class Panel(QFrame):
    def __init__(self, title: str = "", hint: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Panel")
        l, t, r, b = tokens.CARD_PADDING
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(l, t - (2 if tokens.compact() else 0), r, b)
        self.body.setSpacing(tokens.gap(tokens.CARD_GAP))
        self.header = QHBoxLayout()
        self.header.setSpacing(tokens.SPACE_SM)
        self.title = QLabel(title.upper())
        self.title.setObjectName("PanelTitle")
        self.header.addWidget(self.title)
        self.hint = QLabel(hint)
        self.hint.setObjectName("PanelHint")
        self.hint.setVisible(bool(hint))
        self.header.addWidget(self.hint)
        self.header.addStretch()
        if title or hint:
            self.body.addLayout(self.header)

    def event(self, e):
        handled = super().event(e)
        if e.type() == QEvent.LayoutRequest:
            # Our content changed size (rows rebuilt, wrapped text changed). The column holding this
            # panel caches its height-for-width and Qt doesn't refresh that cache by itself: without
            # this, rebuilt rows were squeezed into the panel's old height (a few pixels each).
            self.updateGeometry()
        return handled

    def add_action(self, widget: QWidget):
        self.header.addWidget(widget)
        return widget

    def relayout(self):
        """Content was rebuilt: have the page re-measure this panel now."""
        self.body.invalidate()
        self.updateGeometry()

    def set_hint(self, text: str):
        self.hint.setText(text or "")
        self.hint.setVisible(bool(text))

    def set_active(self, on: bool):
        name = "PanelActive" if on else "Panel"
        if self.objectName() != name:
            self.setObjectName(name)
            self.style().unpolish(self)
            self.style().polish(self)

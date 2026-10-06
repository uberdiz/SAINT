"""
ui/components/qr_view.py

A pairing QR code that is always whole and scannable:

* square, and sized by its layout (``heightForWidth``) instead of a fixed 224 px box — the old
  fixed box was clipped when the Devices page was squeezed on a small or scaled screen;
* the 4-module quiet zone is always inside the white square (scanners need it);
* every module is a whole number of *device* pixels, drawn from a 1-bit image without
  antialiasing, so the code stays sharp at 125 % / 150 % / 175 % Windows scaling;
* never smaller than ``MIN_MODULE_PX`` device pixels per module — the widget asks its layout
  for at least that much room, and a dense code says so instead of shrinking into mush.
"""

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QDialog, QLabel, QSizePolicy, QVBoxLayout, QWidget

QUIET = 4                    # quiet-zone modules on every side (ISO/IEC 18004)
MIN_MODULE_PX = 3            # device pixels per module below which phone cameras struggle
PAD = 10                     # white margin (logical px) outside the quiet zone, for the rounded card


def qr_image(matrix, cell: int) -> QImage:
    """The code (with its quiet zone) as an image, ``cell`` device pixels per module."""
    n = len(matrix)
    side = (n + 2 * QUIET) * cell
    img = QImage(side, side, QImage.Format_RGB32)
    img.fill(QColor("#ffffff"))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, False)
    dark = QColor("#0b0c0e")
    for y, row in enumerate(matrix):
        for x, v in enumerate(row):
            if v:
                p.fillRect((x + QUIET) * cell, (y + QUIET) * cell, cell, cell, dark)
    p.end()
    return img


def module_px(side_device_px: float, modules: int) -> int:
    """Whole device pixels per module that fit ``side`` (quiet zone included)."""
    return max(1, int(math.floor(side_device_px / (modules + 2 * QUIET))))


class QrView(QWidget):
    """Paints a QR matrix (rows of 0/1) as a crisp, square, always-complete code."""

    def __init__(self, preferred: int = 240, parent=None):
        super().__init__(parent)
        self.matrix = None
        self._preferred = preferred
        self._cache = (None, 0, None)          # (matrix id, cell, image)
        sp = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setMinimumSize(self.minimumSizeHint())

    # ---- sizing -------------------------------------------------------------------------
    def modules(self) -> int:
        return len(self.matrix) if self.matrix else 25

    def min_side(self) -> int:
        """Logical px needed for MIN_MODULE_PX device px per module at this screen's scale."""
        dpr = self.devicePixelRatioF() or 1.0
        need = (self.modules() + 2 * QUIET) * MIN_MODULE_PX / dpr + 2 * PAD
        return int(math.ceil(max(160, need)))

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        return w

    def sizeHint(self) -> QSize:
        side = max(self._preferred, self.min_side())
        return QSize(side, side)

    def minimumSizeHint(self) -> QSize:
        side = self.min_side()
        return QSize(side, side)

    def set_matrix(self, matrix):
        self.matrix = matrix
        self._cache = (None, 0, None)
        self.setMinimumSize(self.minimumSizeHint())
        self.updateGeometry()
        self.update()

    # ---- geometry the tests and the page check ------------------------------------------------
    def code_rect(self) -> QRectF:
        """Where the white square (code + quiet zone) is drawn, in widget coordinates."""
        side = min(self.width(), self.height())
        return QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)

    def cell_px(self) -> int:
        dpr = self.devicePixelRatioF() or 1.0
        inner = (min(self.width(), self.height()) - 2 * PAD) * dpr
        return module_px(inner, self.modules())

    def scannable(self) -> bool:
        return bool(self.matrix) and self.cell_px() >= MIN_MODULE_PX

    # ---- painting -------------------------------------------------------------------------------
    def _image(self, cell: int) -> QImage:
        key = id(self.matrix)
        if self._cache[0] != key or self._cache[1] != cell:
            self._cache = (key, cell, qr_image(self.matrix, cell))
        return self._cache[2]

    def paintEvent(self, _):
        g = QPainter(self)
        g.setRenderHint(QPainter.Antialiasing)
        box = self.code_rect()
        g.setPen(Qt.NoPen)
        g.setBrush(QColor("#ffffff"))
        g.drawRoundedRect(box, 12, 12)
        if not self.matrix:
            g.setPen(QColor("#555555"))
            g.drawText(box, Qt.AlignCenter | Qt.TextWordWrap, "QR code unavailable\n(use the code instead)")
            g.end()
            return
        dpr = self.devicePixelRatioF() or 1.0
        img = self._image(self.cell_px())
        img.setDevicePixelRatio(dpr)
        w = img.width() / dpr
        # Snap to whole device pixels so modules don't straddle pixel boundaries.
        x = round((box.center().x() - w / 2) * dpr) / dpr
        y = round((box.center().y() - w / 2) * dpr) / dpr
        g.setRenderHint(QPainter.Antialiasing, False)
        g.setRenderHint(QPainter.SmoothPixmapTransform, False)
        g.drawImage(QPointF(x, y), img)
        g.end()


class QrDialog(QDialog):
    """The pairing code, big, for scanning from across the desk."""

    def __init__(self, matrix, caption: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Scan with SAINT on your phone")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 20)
        lay.setSpacing(12)
        self.qr = QrView(preferred=440)
        self.qr.set_matrix(matrix)
        lay.addWidget(self.qr, 1)
        if caption:
            text = QLabel(caption)
            text.setWordWrap(True)
            text.setAlignment(Qt.AlignCenter)
            text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lay.addWidget(text)
        screen = (parent.screen() if parent is not None else None)
        avail = screen.availableGeometry() if screen is not None else None
        side = 520 if avail is None else max(self.qr.min_side() + 48, min(520, avail.height() - 160))
        self.resize(side, side + (60 if caption else 0))

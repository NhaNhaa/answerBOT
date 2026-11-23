from typing import Tuple, Optional
from PIL import Image
from PyQt6.QtWidgets import QWidget, QApplication
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QRect
from PyQt6.QtGui import QPainter, QColor, QPen, QPixmap, QImage, QScreen

from .capture import get_cursor_phys_pos


def pil_to_qpixmap(pil_img: Image.Image, dpr: float) -> QPixmap:
    if pil_img.mode != "RGBA":
        pil_img = pil_img.convert("RGBA")
    data = pil_img.tobytes("raw", "RGBA")
    qimg = QImage(data, pil_img.width, pil_img.height, pil_img.width * 4, QImage.Format.Format_RGBA8888)
    pix = QPixmap.fromImage(qimg)
    pix.setDevicePixelRatio(dpr)
    return pix


class SnipOverlay(QWidget):
    """
    Stealth 1:1 physical-pixel anchored frozen screen snip overlay.
    - Zero screen-dimming flash (looks identical to live screen).
    - Faint 1px low-contrast outline while dragging.
    - Exact physical-pixel cropping via Win32 GetCursorPos() under PerMonitorV2.
    - Left-click drag crops; tiny (<5 px) tap cancels without stealing focus.
    """
    snip_completed = pyqtSignal(object)  # Emits PIL.Image
    snip_cancelled = pyqtSignal()

    def __init__(
        self,
        frozen_img: Image.Image,
        origin_phys: Tuple[int, int],
        monitor: Optional[QScreen] = None,
    ):
        super().__init__()
        self.frozen_img = frozen_img
        self.origin_phys = origin_phys
        self.monitor = monitor or QApplication.primaryScreen()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        geo = self.monitor.geometry()
        self.setGeometry(geo)
        self.dpr = float(self.monitor.devicePixelRatio() or 1.0)

        # Compute effective scale directly from physical grab vs logical geometry
        eff_dpr = self.frozen_img.width / max(1, geo.width()) if geo.width() > 0 else self.dpr
        self.pixmap = pil_to_qpixmap(frozen_img, eff_dpr)

        self.start_pos: Optional[QPoint] = None
        self.current_pos: Optional[QPoint] = None
        self._press_phys: Optional[Tuple[int, int]] = None
        self._completed_or_cancelled = False
        self.setCursor(Qt.CursorShape.CrossCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start_pos = event.position().toPoint()
            self.current_pos = self.start_pos
            self._press_phys = get_cursor_phys_pos()
            self.update()

    def mouseMoveEvent(self, event):
        if self.start_pos is not None:
            self.current_pos = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.start_pos is not None and self.current_pos is not None:
            rect = QRect(self.start_pos, self.current_pos).normalized()
            release_phys = get_cursor_phys_pos()
            press_phys = self._press_phys

            self.start_pos = None
            self.current_pos = None
            self._press_phys = None

            if rect.width() < 5 or rect.height() < 5:
                self.cancel()
                return

            # Primary path: true Win32 physical cursor delta (immune to DPI / Qt frame offsets)
            if (
                press_phys is not None
                and abs(release_phys[0] - press_phys[0]) >= 5
                and abs(release_phys[1] - press_phys[1]) >= 5
            ):
                p_left = min(press_phys[0], release_phys[0]) - self.origin_phys[0]
                p_top = min(press_phys[1], release_phys[1]) - self.origin_phys[1]
                p_right = max(press_phys[0], release_phys[0]) - self.origin_phys[0]
                p_bottom = max(press_phys[1], release_phys[1]) - self.origin_phys[1]
            else:
                # Fallback for synthetic Qt test events where OS hardware cursor didn't move
                sx = self.frozen_img.width / max(1, self.width())
                sy = self.frozen_img.height / max(1, self.height())
                p_left = int(round(rect.left() * sx))
                p_top = int(round(rect.top() * sy))
                p_right = int(round((rect.left() + rect.width()) * sx))
                p_bottom = int(round((rect.top() + rect.height()) * sy))

            crop_l = max(0, min(self.frozen_img.width, p_left))
            crop_t = max(0, min(self.frozen_img.height, p_top))
            crop_r = max(0, min(self.frozen_img.width, p_right))
            crop_b = max(0, min(self.frozen_img.height, p_bottom))

            if (crop_r - crop_l) >= 5 and (crop_b - crop_t) >= 5:
                self._completed_or_cancelled = True
                cropped = self.frozen_img.crop((crop_l, crop_t, crop_r, crop_b))
                self.hide()
                self.snip_completed.emit(cropped)
                self.deleteLater()
            else:
                self.cancel()

    def cancel(self):
        if self._completed_or_cancelled:
            return
        self._completed_or_cancelled = True
        self.hide()
        self.snip_cancelled.emit()
        self.deleteLater()

    def paintEvent(self, event):
        painter = QPainter(self)
        # Render frozen screen 1:1 with zero dimming flash so snipping attracts zero attention
        if self.pixmap:
            painter.drawPixmap(0, 0, self.pixmap)

        if self.start_pos is not None and self.current_pos is not None:
            rect = QRect(self.start_pos, self.current_pos).normalized()
            painter.fillRect(rect, QColor(255, 255, 255, 10))
            pen = QPen(QColor(160, 165, 175, 135), 1, Qt.PenStyle.SolidLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)

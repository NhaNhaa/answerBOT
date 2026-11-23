import os
import sys
import json
import math
import ctypes
from ctypes import wintypes
from typing import Optional, Callable, Dict, Any, Tuple

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QAbstractScrollArea
)
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QPointF, QTimer
from PyQt6.QtGui import QPainter, QColor, QBrush, QPen, QCursor, QPolygonF, QFont

from .config import (
    DRAG_THRESHOLD_PX, STATE_FILE,
    DEFAULT_CARD_W, DEFAULT_CARD_H
)
from .capture import is_wda_supported, WDA_EXCLUDEFROMCAPTURE

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204
WM_MBUTTONDOWN = 0x0207
WM_MOUSEWHEEL = 0x020A
VK_ESCAPE = 0x1B

WCA_ACCENT_POLICY = 19
ACCENT_ENABLE_BLURBEHIND = 3
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUNDSMALL = 3
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000


class ACCENT_POLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_uint32),
        ("AnimationId", ctypes.c_int),
    ]


class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
    _fields_ = [
        ("Attrib", ctypes.c_int),
        ("pvData", ctypes.c_void_p),
        ("cbData", ctypes.c_size_t),
    ]


def enable_window_blur(hwnd: int) -> bool:
    """
    Applies native Windows DWM Gaussian blur behind a frameless translucent window,
    with small rounded corner clipping on Windows 11 (build >= 22000).
    """
    ok = False
    try:
        accent = ACCENT_POLICY()
        accent.AccentState = ACCENT_ENABLE_BLURBEHIND
        accent.AccentFlags = 0
        accent.GradientColor = 0
        accent.AnimationId = 0

        data = WINDOWCOMPOSITIONATTRIBDATA()
        data.Attrib = WCA_ACCENT_POLICY
        data.pvData = ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p)
        data.cbData = ctypes.sizeof(accent)

        if hasattr(user32, "SetWindowCompositionAttribute"):
            res = user32.SetWindowCompositionAttribute(wintypes.HWND(hwnd), ctypes.byref(data))
            ok = bool(res)
    except Exception:
        pass

    try:
        if sys.getwindowsversion().build >= 22000:
            pref = wintypes.DWORD(DWMWCP_ROUNDSMALL)
            dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd),
                wintypes.DWORD(DWMWA_WINDOW_CORNER_PREFERENCE),
                ctypes.byref(pref),
                ctypes.sizeof(pref),
            )
    except Exception:
        pass

    return ok


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
user32.SetWindowLongW.restype = wintypes.LONG
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


class ScopedEscapeHook:
    """
    Installs WH_KEYBOARD_LL only while overlays are open.
    Allows pressing Escape to close overlays without stealing keyboard focus.
    """
    _hook = None
    _callbacks = []
    _proc = None
    _atexit_bound = False

    @classmethod
    def register_callback(cls, cb: Callable[[], None]):
        if cb not in cls._callbacks:
            cls._callbacks.append(cb)
        cls._ensure_hook()

    @classmethod
    def unregister_callback(cls, cb: Callable[[], None]):
        if cb in cls._callbacks:
            cls._callbacks.remove(cb)
        if not cls._callbacks:
            cls._remove_hook()

    @classmethod
    def _ensure_hook(cls):
        if not cls._atexit_bound:
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(cls._remove_hook)
                cls._atexit_bound = True

        if cls._hook is None:
            def hook_callback(nCode, wParam, lParam):
                if nCode == 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if kb.vkCode == VK_ESCAPE:
                        for cb in list(cls._callbacks):
                            QTimer.singleShot(0, cb)
                return user32.CallNextHookEx(cls._hook, nCode, wParam, lParam)

            cls._proc = HOOKPROC(hook_callback)
            cls._hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL,
                cls._proc,
                kernel32.GetModuleHandleW(None),
                0,
            )

    @classmethod
    def _remove_hook(cls):
        if cls._hook:
            user32.UnhookWindowsHookEx(cls._hook)
            cls._hook = None
            cls._proc = None
        cls._callbacks.clear()


class ScopedOutsideClickHook:
    """
    Installs WH_MOUSE_LL only while PopupPanel is visible.
    - Hides the non-activating PopupPanel on outside left-click (without consuming clicks).
    - Routes WM_MOUSEWHEEL to PopupPanel when hovering over overflowing answer text,
      keeping PopupPanel 100% unclickable while enabling mouse-wheel/touchpad scrolling.
    """
    _hook = None
    _callbacks = []
    _wheel_callbacks = []
    _proc = None
    _atexit_bound = False

    @classmethod
    def register_callback(cls, cb: Callable[[Optional[QPoint]], None]):
        if cb not in cls._callbacks:
            cls._callbacks.append(cb)
        cls._ensure_hook()

    @classmethod
    def unregister_callback(cls, cb: Callable[[Optional[QPoint]], None]):
        if cb in cls._callbacks:
            cls._callbacks.remove(cb)
        if not cls._callbacks and not cls._wheel_callbacks:
            cls._remove_hook()

    @classmethod
    def register_wheel_callback(cls, cb: Callable[[int, int, int], bool]):
        if cb not in cls._wheel_callbacks:
            cls._wheel_callbacks.append(cb)
        cls._ensure_hook()

    @classmethod
    def unregister_wheel_callback(cls, cb: Callable[[int, int, int], bool]):
        if cb in cls._wheel_callbacks:
            cls._wheel_callbacks.remove(cb)
        if not cls._callbacks and not cls._wheel_callbacks:
            cls._remove_hook()

    @classmethod
    def trigger_at(cls, global_pos: Optional[QPoint] = None):
        pos = global_pos if global_pos is not None else QCursor.pos()
        for cb in list(cls._callbacks):
            cb(pos)

    @classmethod
    def trigger_wheel_at(cls, phys_x: int, phys_y: int, delta: int) -> bool:
        handled = False
        for cb in list(cls._wheel_callbacks):
            if cb(phys_x, phys_y, delta):
                handled = True
        return handled

    @classmethod
    def _ensure_hook(cls):
        if not cls._atexit_bound:
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(cls._remove_hook)
                cls._atexit_bound = True

        if cls._hook is None:
            def hook_callback(nCode, wParam, lParam):
                if nCode == 0:
                    if wParam == WM_LBUTTONDOWN:
                        QTimer.singleShot(0, cls.trigger_at)
                    elif wParam == WM_MOUSEWHEEL and cls._wheel_callbacks:
                        ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                        delta = ctypes.c_short((ms.mouseData >> 16) & 0xFFFF).value
                        if delta != 0 and cls.trigger_wheel_at(int(ms.pt.x), int(ms.pt.y), int(delta)):
                            return 1
                return user32.CallNextHookEx(cls._hook, nCode, wParam, lParam)

            cls._proc = HOOKPROC(hook_callback)
            cls._hook = user32.SetWindowsHookExW(
                WH_MOUSE_LL,
                cls._proc,
                kernel32.GetModuleHandleW(None),
                0,
            )

    @classmethod
    def _remove_hook(cls):
        if cls._hook:
            user32.UnhookWindowsHookEx(cls._hook)
            cls._hook = None
            cls._proc = None
        cls._callbacks.clear()
        cls._wheel_callbacks.clear()


class FloatingBubble(QWidget):
    """
    Subtle 18x18 px Non-Activating Micro-Dot.
    Dual-ring 22% gray, 65% hover.
    Drag threshold: 5 px. Left-click only.
    """
    clicked = pyqtSignal()
    moved = pyqtSignal(int, int)
    position_changed = pyqtSignal(int, int)

    def __init__(self, init_pos: Optional[Tuple[int, int]] = None, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.resize(18, 18)

        self._is_hovered = False
        self._drag_start_pos: Optional[QPoint] = None
        self._window_start_pos: Optional[QPoint] = None
        self._is_dragging = False

        if init_pos:
            self.move(init_pos[0], init_pos[1])
        else:
            self._move_to_default_position()

    def showEvent(self, event):
        super().showEvent(event)
        if is_wda_supported():
            hwnd = int(self.winId())
            user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), wintypes.DWORD(WDA_EXCLUDEFROMCAPTURE))

    def _move_to_default_position(self):
        screen = self.screen() or (self.parent().screen() if self.parent() else None)
        if not screen:
            from PyQt6.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.right() - 40, geo.center().y())

    def enterEvent(self, event):
        self._is_hovered = True
        self.update()

    def leaveEvent(self, event):
        self._is_hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.globalPosition().toPoint()
            self._window_start_pos = self.pos()
            self._is_dragging = False

    def mouseMoveEvent(self, event):
        if self._drag_start_pos and self._window_start_pos:
            curr_pos = event.globalPosition().toPoint()
            delta = curr_pos - self._drag_start_pos
            if delta.manhattanLength() >= DRAG_THRESHOLD_PX:
                self._is_dragging = True
                new_pos = self._window_start_pos + delta
                self.move(new_pos)
                self.moved.emit(new_pos.x(), new_pos.y())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._is_dragging:
                self._snap_to_edge()
                self.position_changed.emit(self.x(), self.y())
            else:
                self.clicked.emit()
            self._drag_start_pos = None
            self._window_start_pos = None
            self._is_dragging = False

    def _snap_to_edge(self):
        screen = self.screen()
        if not screen:
            return
        geo = screen.availableGeometry()
        snap_margin = 25
        x = self.x()
        y = self.y()
        if abs(x - geo.left()) < snap_margin:
            x = geo.left() + 5
        elif abs(x - (geo.right() - self.width())) < snap_margin:
            x = geo.right() - self.width() - 5
        self.move(x, y)
        self.moved.emit(x, y)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        alpha = 155 if self._is_hovered else 52
        brush_color = QColor(90, 92, 98, alpha)
        pen_color = QColor(55, 58, 64, alpha + 18)

        painter.setPen(QPen(pen_color, 1))
        painter.setBrush(QBrush(brush_color))
        painter.drawEllipse(1, 1, 16, 16)

        core_alpha = min(255, alpha + 40)
        painter.setBrush(QBrush(QColor(165, 168, 175, core_alpha)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(6, 6, 6, 6)


CARD_BG_COLOR = QColor(255, 255, 255, 135)
CARD_BORDER_COLOR = QColor(215, 222, 232, 135)


class PinToggleButton(QWidget):
    """
    Tiny 18x18 non-activating pin toggle anchored below FloatingBubble.
    Visible only while PopupPanel is open.
    Outlined pin = OFF (outside click hides box); filled pin = ON (box stays locked).
    Left-click only.
    """
    toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.resize(18, 18)

        self._pinned = False
        self._is_hovered = False

    def is_pinned(self) -> bool:
        return self._pinned

    def set_pinned(self, pinned: bool, emit_signal: bool = True) -> None:
        pinned = bool(pinned)
        if self._pinned != pinned:
            self._pinned = pinned
            self.update()
            if emit_signal:
                self.toggled.emit(self._pinned)

    def reposition_near_bubble(self, bubble: QWidget) -> None:
        bx = bubble.x() + (bubble.width() - self.width()) // 2
        by = bubble.y() + bubble.height() + 4
        screen = bubble.screen()
        if not screen:
            from PyQt6.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            if by + self.height() > avail.bottom():
                by = bubble.y() - self.height() - 4
            bx = max(avail.left(), min(bx, avail.right() - self.width()))
            by = max(avail.top(), min(by, avail.bottom() - self.height()))
        self.move(bx, by)

    def showEvent(self, event):
        super().showEvent(event)
        if is_wda_supported():
            hwnd = int(self.winId())
            user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), wintypes.DWORD(WDA_EXCLUDEFROMCAPTURE))

    def enterEvent(self, event):
        self._is_hovered = True
        self.update()

    def leaveEvent(self, event):
        self._is_hovered = False
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.set_pinned(not self._pinned)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bg_alpha = 180 if (self._pinned or self._is_hovered) else 125
        painter.setBrush(QBrush(QColor(255, 255, 255, bg_alpha)))
        painter.setPen(QPen(QColor(190, 198, 210, 145 if self._is_hovered else 115), 1))
        painter.drawEllipse(1, 1, 16, 16)

        if self._pinned:
            color = QColor(45, 50, 58, 240)
            pen = QPen(color, 1.25, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(QBrush(color))
        else:
            alpha = 225 if self._is_hovered else 175
            color = QColor(75, 82, 92, alpha)
            pen = QPen(color, 1.25, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)

        # Minimal upright pushpin: cap + tapered body + flange + needle
        body = QPolygonF([
            QPointF(7.2, 4.6),
            QPointF(10.8, 4.6),
            QPointF(11.4, 9.4),
            QPointF(6.6, 9.4),
        ])
        painter.drawPolygon(body)
        painter.drawLine(QPointF(6.4, 4.6), QPointF(11.6, 4.6))
        painter.drawLine(QPointF(5.6, 9.4), QPointF(12.4, 9.4))
        painter.drawLine(QPointF(9.0, 9.4), QPointF(9.0, 14.0))


class StarToggleButton(QWidget):
    """
    Tiny 18x18 non-activating star toggle anchored above FloatingBubble.
    Visible only while PopupPanel is open.
    Outlined star = OFF (normal dot-life countdown);
    filled star = ON (keeps same answer across unlimited close/re-open cycles).
    Left-click only.
    """
    toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.resize(18, 18)

        self._starred = False
        self._is_hovered = False

    def is_starred(self) -> bool:
        return self._starred

    def set_starred(self, starred: bool, emit_signal: bool = True) -> None:
        starred = bool(starred)
        if self._starred != starred:
            self._starred = starred
            self.update()
            if emit_signal:
                self.toggled.emit(self._starred)

    def reposition_near_bubble(self, bubble: QWidget) -> None:
        bx = bubble.x() + (bubble.width() - self.width()) // 2
        by = bubble.y() - self.height() - 4
        screen = bubble.screen()
        if not screen:
            from PyQt6.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            # If Pin flipped above the dot near bottom edge, stack Star above Pin
            if bubble.y() + bubble.height() + 4 + 18 > avail.bottom():
                by = bubble.y() - 18 - 4 - self.height() - 4
            # If Star would clip above the top edge, place it below Pin instead
            if by < avail.top():
                by = bubble.y() + bubble.height() + 4 + 18 + 4
            bx = max(avail.left(), min(bx, avail.right() - self.width()))
            by = max(avail.top(), min(by, avail.bottom() - self.height()))
        self.move(bx, by)

    def showEvent(self, event):
        super().showEvent(event)
        if is_wda_supported():
            hwnd = int(self.winId())
            user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), wintypes.DWORD(WDA_EXCLUDEFROMCAPTURE))

    def enterEvent(self, event):
        self._is_hovered = True
        self.update()

    def leaveEvent(self, event):
        self._is_hovered = False
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.set_starred(not self._starred)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bg_alpha = 180 if (self._starred or self._is_hovered) else 125
        painter.setBrush(QBrush(QColor(255, 255, 255, bg_alpha)))
        painter.setPen(QPen(QColor(190, 198, 210, 145 if self._is_hovered else 115), 1))
        painter.drawEllipse(1, 1, 16, 16)

        if self._starred:
            color = QColor(45, 50, 58, 240)
            pen = QPen(color, 1.15, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(QBrush(color))
        else:
            alpha = 225 if self._is_hovered else 175
            color = QColor(75, 82, 92, alpha)
            pen = QPen(color, 1.15, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)

        cx, cy = 9.0, 9.2
        r_outer, r_inner = 5.0, 2.15
        pts = []
        for i in range(10):
            angle = math.radians(-90 + i * 36)
            r = r_outer if i % 2 == 0 else r_inner
            pts.append(QPointF(cx + r * math.cos(angle), cy + r * math.sin(angle)))
        painter.drawPolygon(QPolygonF(pts))


class DoneSnipButton(QWidget):
    """
    Small non-activating stealth pill anchored below FloatingBubble while
    collecting multi-snip images (1..4 snips). Displays "Done N/5".
    Left-click only.
    """
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.resize(56, 18)

        self._count = 0
        self._total = 5
        self._label = "Done 0/5"
        self._is_hovered = False

    def label_text(self) -> str:
        return self._label

    def set_count(self, count: int, total: int) -> None:
        self._count = max(0, int(count))
        self._total = max(1, int(total))
        self._label = f"Done {self._count}/{self._total}"
        self.update()

    def reposition_near_bubble(self, bubble: QWidget) -> None:
        bx = bubble.x() + (bubble.width() - self.width()) // 2
        by = bubble.y() + bubble.height() + 4
        screen = bubble.screen()
        if not screen:
            from PyQt6.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            if by + self.height() > avail.bottom():
                by = bubble.y() - self.height() - 4
            bx = max(avail.left() + 2, min(bx, avail.right() - self.width() - 2))
            by = max(avail.top() + 2, min(by, avail.bottom() - self.height() - 2))
        self.move(bx, by)

    def showEvent(self, event):
        super().showEvent(event)
        if is_wda_supported():
            hwnd = int(self.winId())
            user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), wintypes.DWORD(WDA_EXCLUDEFROMCAPTURE))

    def enterEvent(self, event):
        self._is_hovered = True
        self.update()

    def leaveEvent(self, event):
        self._is_hovered = False
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bg_alpha = 185 if self._is_hovered else 135
        border_alpha = 155 if self._is_hovered else 120
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setBrush(QBrush(QColor(255, 255, 255, bg_alpha)))
        painter.setPen(QPen(QColor(190, 198, 210, border_alpha), 1))
        painter.drawRoundedRect(rect, 8, 8)

        font = QFont("Segoe UI")
        font.setPixelSize(10)
        painter.setFont(font)
        text_alpha = 245 if self._is_hovered else 225
        painter.setPen(QColor(45, 50, 58, text_alpha))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._label)


class PopupPanel(QWidget):
    """
    Low-profile click-through see-through light-white answer card (~340x260 px default).
    Top-left shows remaining dot lives (N/2); hides on outside left-click or dot click.
    """

    def __init__(self, backend_widget: QWidget, parent=None, anchor_bubble: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setMinimumSize(190, 115)

        self._anchor_bubble: Optional[QWidget] = anchor_bubble
        self._pin_button: Optional[PinToggleButton] = None
        self._star_button: Optional[StarToggleButton] = None
        self._stay_open: bool = False
        self._wheel_remainder: float = 0.0
        self.last_answer_text: str = ""

        self._state = self._load_state()
        init_w = int(self._state.get("card_w", DEFAULT_CARD_W))
        init_h = int(self._state.get("card_h", DEFAULT_CARD_H))
        if not (190 <= init_w <= 700) or not (115 <= init_h <= 600):
            init_w, init_h = DEFAULT_CARD_W, DEFAULT_CARD_H
        self.resize(init_w, init_h)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 5, 7, 6)
        layout.setSpacing(2)

        # Compact Top Row: N/2 Lives (top-left) + transient status
        self.top_bar = QWidget(self)
        self.top_bar.setFixedHeight(16)
        self.top_bar.setStyleSheet("background: transparent;")
        top_layout = QHBoxLayout(self.top_bar)
        top_layout.setContentsMargins(3, 0, 3, 0)
        top_layout.setSpacing(6)

        self.lives_label = QLabel("")
        self.lives_label.setStyleSheet(
            "font-family: 'Segoe UI', sans-serif; font-size: 10px; "
            "color: rgba(45, 50, 58, 235); background: transparent;"
        )
        self.lives_label.hide()

        self.status_label = QLabel("")
        self.status_label.setStyleSheet(
            "font-family: 'Segoe UI', sans-serif; font-size: 9.5px; "
            "color: rgba(110, 117, 128, 175); background: transparent;"
        )
        self.status_label.hide()

        top_layout.addWidget(self.lives_label)
        top_layout.addWidget(self.status_label)
        top_layout.addStretch()
        layout.addWidget(self.top_bar)

        # Answer Body
        self.backend_container = backend_widget
        layout.addWidget(self.backend_container, 1)

    def set_anchor_bubble(self, bubble: QWidget):
        self._anchor_bubble = bubble

    def set_pin_button(self, pin_button: Optional[PinToggleButton]):
        self._pin_button = pin_button

    def set_star_button(self, star_button: Optional[StarToggleButton]):
        self._star_button = star_button

    def set_stay_open(self, locked: bool):
        self._stay_open = bool(locked)

    def is_stay_open(self) -> bool:
        return self._stay_open

    def set_lives(self, remaining: int, total: int, keep_forever: bool = False):
        if keep_forever:
            self.lives_label.setText("∞")
            self.lives_label.show()
        elif total > 0:
            self.lives_label.setText(f"{max(0, remaining)}/{total}")
            self.lives_label.show()
        else:
            self.lives_label.setText("")
            self.lives_label.hide()

    def set_status(self, text: str):
        cleaned = (text or "").strip()
        if not cleaned or cleaned.lower() in ("done", "ready"):
            self.status_label.setText("")
            self.status_label.hide()
            return
        self.status_label.setText(cleaned)
        self.status_label.show()

    def _on_global_mouse_press(self, global_pos: Optional[QPoint] = None):
        if not self.isVisible() or self._stay_open:
            return
        pt = global_pos if global_pos is not None else QCursor.pos()
        if self.frameGeometry().contains(pt):
            return
        if (
            self._anchor_bubble is not None
            and self._anchor_bubble.isVisible()
            and self._anchor_bubble.frameGeometry().contains(pt)
        ):
            return
        if (
            self._pin_button is not None
            and self._pin_button.isVisible()
            and self._pin_button.frameGeometry().contains(pt)
        ):
            return
        if (
            self._star_button is not None
            and self._star_button.isVisible()
            and self._star_button.frameGeometry().contains(pt)
        ):
            return
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.setBrush(QBrush(CARD_BG_COLOR))
        painter.setPen(QPen(CARD_BORDER_COLOR, 1))
        painter.drawRoundedRect(rect, 6, 6)

    def _find_scroll_area(self) -> Optional[QAbstractScrollArea]:
        if isinstance(self.backend_container, QAbstractScrollArea):
            return self.backend_container
        return self.backend_container.findChild(QAbstractScrollArea)

    def _physical_rect(self) -> Optional[Tuple[int, int, int, int]]:
        if not self.isVisible():
            return None
        rect = wintypes.RECT()
        hwnd = int(self.winId())
        if user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        return None

    def scroll_by_wheel(self, delta: int) -> bool:
        area = self._find_scroll_area()
        if area is None:
            return False
        vbar = area.verticalScrollBar()
        if vbar is None or vbar.maximum() <= 0:
            return False
        if self._wheel_remainder and ((delta > 0) != (self._wheel_remainder > 0)):
            self._wheel_remainder = 0.0
        px_float = (delta / 120.0) * 48.0 + self._wheel_remainder
        px = int(round(px_float))
        self._wheel_remainder = px_float - px
        if px == 0:
            return True
        new_val = max(vbar.minimum(), min(vbar.maximum(), vbar.value() - px))
        vbar.setValue(new_val)
        return True

    def _handle_global_wheel(self, phys_x: int, phys_y: int, delta: int) -> bool:
        if not self.isVisible() or delta == 0:
            return False
        bounds = self._physical_rect()
        if bounds is None:
            return False
        left, top, right, bottom = bounds
        if not (left <= phys_x < right and top <= phys_y < bottom):
            return False
        area = self._find_scroll_area()
        if area is None:
            return False
        vbar = area.verticalScrollBar()
        if vbar is None or vbar.maximum() <= 0:
            return False
        QTimer.singleShot(0, lambda d=delta: self.scroll_by_wheel(d))
        return True

    def showEvent(self, event):
        super().showEvent(event)
        ScopedOutsideClickHook.register_callback(self._on_global_mouse_press)
        ScopedOutsideClickHook.register_wheel_callback(self._handle_global_wheel)
        hwnd = int(self.winId())
        try:
            ex_style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
            user32.SetWindowLongW(
                wintypes.HWND(hwnd),
                GWL_EXSTYLE,
                ex_style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE,
            )
        except Exception:
            pass
        if is_wda_supported():
            user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), wintypes.DWORD(WDA_EXCLUDEFROMCAPTURE))
        if self._pin_button is not None:
            if self._anchor_bubble is not None:
                self._pin_button.reposition_near_bubble(self._anchor_bubble)
            self._pin_button.show()
        if self._star_button is not None:
            if self._anchor_bubble is not None:
                self._star_button.reposition_near_bubble(self._anchor_bubble)
            self._star_button.show()

    def hideEvent(self, event):
        super().hideEvent(event)
        ScopedOutsideClickHook.unregister_callback(self._on_global_mouse_press)
        ScopedOutsideClickHook.unregister_wheel_callback(self._handle_global_wheel)
        self._wheel_remainder = 0.0
        if self._pin_button is not None:
            self._pin_button.hide()
        if self._star_button is not None:
            self._star_button.hide()

    def _load_state(self) -> Dict[str, Any]:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "card_w": DEFAULT_CARD_W,
            "card_h": DEFAULT_CARD_H,
        }

    def _save_state(self):
        tmp_file = STATE_FILE.with_suffix(".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
            os.replace(tmp_file, STATE_FILE)
        except Exception:
            pass

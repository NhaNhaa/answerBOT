import sys
import ctypes
from ctypes import wintypes
from typing import Optional, Tuple, Callable, Any
from PIL import Image
import mss

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

MONITOR_DEFAULTTONEAREST = 2
WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011


class POINT(ctypes.Structure):
    _fields_ = [
        ("x", wintypes.LONG),
        ("y", wintypes.LONG),
    ]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.MonitorFromPoint.argtypes = [POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HMONITOR
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL


def is_wda_supported() -> bool:
    """WDA_EXCLUDEFROMCAPTURE is supported on Windows 10 2004+ (build >= 19041)."""
    return sys.getwindowsversion().build >= 19041


def get_cursor_phys_pos() -> Tuple[int, int]:
    """Returns exact physical screen cursor coordinates under PerMonitorV2."""
    pt = POINT()
    if user32.GetCursorPos(ctypes.byref(pt)):
        return int(pt.x), int(pt.y)
    return 0, 0


def get_monitor_phys_bounds_at_point(px: int, py: int) -> Tuple[int, int, int, int]:
    """Returns (left, top, width, height) in physical pixels for the monitor containing (px, py)."""
    hmon = user32.MonitorFromPoint(POINT(px, py), MONITOR_DEFAULTTONEAREST)
    if hmon:
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            l = int(mi.rcMonitor.left)
            t = int(mi.rcMonitor.top)
            w = int(mi.rcMonitor.right - mi.rcMonitor.left)
            h = int(mi.rcMonitor.bottom - mi.rcMonitor.top)
            if w > 0 and h > 0:
                return l, t, w, h

    with mss.MSS() as sct:
        mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        return int(mon["left"]), int(mon["top"]), int(mon["width"]), int(mon["height"])


def is_image_blank(image: Image.Image) -> bool:
    """Checks if an image is uniform (e.g. solid black or DRM protected content)."""
    colors = image.getcolors(maxcolors=2)
    return colors is not None and len(colors) == 1


def capture_active_monitor(cursor_phys: Optional[Tuple[int, int]] = None) -> Tuple[Image.Image, Tuple[int, int, int, int]]:
    """
    Captures the full monitor containing the cursor in exact physical pixels.
    Returns (PIL.Image, (mon_left_phys, mon_top_phys, mon_width_phys, mon_height_phys)).
    """
    px, py = cursor_phys if cursor_phys is not None else get_cursor_phys_pos()
    left, top, width, height = get_monitor_phys_bounds_at_point(px, py)
    with mss.MSS() as sct:
        raw = sct.grab({"left": left, "top": top, "width": width, "height": height})
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    return img, (left, top, width, height)


def hide_and_grab(windows: list, grab_callback: Callable[[], Any]) -> Any:
    """
    Hides specified windows that are currently visible, flushes composition frames,
    runs the grab callback, and restores visibility only to windows that were previously visible.
    """
    visible_windows = []
    for w in windows:
        if hasattr(w, "isVisible") and w.isVisible():
            visible_windows.append(w)
            w.hide()

    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app:
        app.processEvents()

    dwmapi.DwmFlush()
    dwmapi.DwmFlush()

    try:
        return grab_callback()
    finally:
        for w in visible_windows:
            w.show()

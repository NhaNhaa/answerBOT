"""
QuickRead AI Overlay — Stealth blurred floating question & reading assistant.
"""
import sys
import os
import ctypes
from typing import Optional

# Line 1: PerMonitorV2 DPI awareness BEFORE QApplication or mss
import re
user32 = ctypes.windll.user32
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--no-sandbox --disable-blink-features=AutomationControlled"
)

from PyQt6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu
)
from PyQt6.QtCore import Qt, QLockFile, QTimer
from PyQt6.QtGui import QIcon, QPixmap, QColor, QPainter, QCursor, QScreen
from PIL import Image

from src.config import APP_DATA_DIR, DEFAULT_IMAGE_PROMPT, DOT_SPARE_LIVES, MAX_SNIP_IMAGES
from src.capture import capture_active_monitor, get_cursor_phys_pos, hide_and_grab
from src.ai_backend import create_backend, AIBackend
from src.floating_ui import FloatingBubble, PopupPanel, PinToggleButton, StarToggleButton, DoneSnipButton
from src.snip_overlay import SnipOverlay


def create_tray_icon_pixmap() -> QPixmap:
    pix = QPixmap(32, 32)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(65, 70, 80))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(2, 2, 28, 28)
    painter.setPen(QColor(230, 232, 238))
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "Q")
    painter.end()
    return pix


class QuickReadApp:
    def __init__(self, is_smoke_test: bool = False, backend: Optional[AIBackend] = None):
        self.is_smoke_test = is_smoke_test
        self.backend = backend if backend is not None else create_backend()
        self.card = PopupPanel(self.backend.widget())

        saved_pos = None
        if "dot_x" in self.card._state and "dot_y" in self.card._state:
            saved_pos = (self.card._state["dot_x"], self.card._state["dot_y"])

        self.bubble = FloatingBubble(init_pos=saved_pos)
        self.pin_btn = PinToggleButton()
        self.star_btn = StarToggleButton()
        self.done_btn = DoneSnipButton()
        self.card.set_anchor_bubble(self.bubble)
        self.card.set_pin_button(self.pin_btn)
        self.card.set_star_button(self.star_btn)
        self.pin_btn.toggled.connect(self.card.set_stay_open)
        self.star_btn.toggled.connect(self._on_star_toggled)
        self.done_btn.clicked.connect(self.on_done_clicked)
        self.bubble.moved.connect(lambda _x, _y: self._reposition_bubble_accessories())
        self.bubble.position_changed.connect(self._on_bubble_moved)

        self._active_snip: Optional[SnipOverlay] = None
        self._pending_snips: list[Image.Image] = []
        self._has_captured_once: bool = False
        self._reopen_lives: int = 0
        self._keep_answer_forever: bool = False
        self._last_snip_image: Optional[Image.Image] = None
        self._last_snip_images: list[Image.Image] = []
        self._cached_answer: Optional[str] = None
        self._cleaned_up: bool = False
        # Snip stays locked until the startup login window is closed (web backend only).
        self._login_pending: bool = self.backend.requires_login and not is_smoke_test

        # Connect signals:
        # 1st dot click -> Snip (grants DOT_SPARE_LIVES); dot toggles card while lives > 0,
        # then clears answer and starts a new snip when lives == 0.
        self.bubble.clicked.connect(self.on_dot_clicked)
        self.backend.status_changed.connect(self.card.set_status)
        self.backend.answer_ready.connect(self._on_answer_ready)

        self._setup_tray()

        if not self.is_smoke_test:
            if not self._login_pending:
                self.backend.prewarm()

    def begin_login(self):
        if not self._login_pending:
            return
        self.card.set_status("Please log in to Google")
        self.backend.login_finished.connect(self._on_login_finished)
        self.backend.open_login_helper(modal=False)

    def _on_login_finished(self):
        if not self._login_pending:
            return
        self._login_pending = False
        self.backend.prewarm()
        self.card.set_status("Ready" if self.backend.is_logged_in() else "Not logged in - uploads may fail")

    def _reposition_bubble_accessories(self):
        self.pin_btn.reposition_near_bubble(self.bubble)
        self.star_btn.reposition_near_bubble(self.bubble)
        self.done_btn.reposition_near_bubble(self.bubble)

    def _on_star_toggled(self, keep: bool):
        self._keep_answer_forever = bool(keep)
        if self._has_captured_once:
            self.card.set_lives(self._reopen_lives, DOT_SPARE_LIVES, keep_forever=self._keep_answer_forever)

    def _on_bubble_moved(self, x: int, y: int):
        self.card._state["dot_x"] = x
        self.card._state["dot_y"] = y
        self.card._save_state()
        self._reposition_bubble_accessories()

    def _setup_tray(self):
        app = QApplication.instance()
        self.tray = QSystemTrayIcon(QIcon(create_tray_icon_pixmap()), app)
        self.tray_menu = QMenu()

        locate_act = self.tray_menu.addAction("Show / Locate Dot")
        locate_act.triggered.connect(self._locate_dot)

        login_act = self.tray_menu.addAction("Google Login / Verify...")
        login_act.triggered.connect(lambda: self.backend.open_login_helper())

        self.tray_menu.addSeparator()
        quit_act = self.tray_menu.addAction("Quit")
        quit_act.triggered.connect(self.quit)

        self.tray.setContextMenu(self.tray_menu)
        if not self.is_smoke_test:
            self.tray.show()

    def _locate_dot(self):
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.bubble.move(geo.right() - 40, geo.center().y())
        self.bubble.show()
        self.bubble.raise_()
        self._reposition_bubble_accessories()

    def _position_card_near_bubble(self):
        screen = self.bubble.screen() or QApplication.primaryScreen()
        if not screen:
            return
        avail = screen.availableGeometry()
        bx = self.bubble.x()
        by = self.bubble.y()
        card_w = self.card.width()
        card_h = self.card.height()

        if bx > avail.center().x():
            cx = bx - card_w - 10
        else:
            cx = bx + self.bubble.width() + 10

        cy = max(avail.top() + 8, min(by - 20, avail.bottom() - card_h - 8))
        cx = max(avail.left() + 8, min(cx, avail.right() - card_w - 8))
        self.card.move(cx, cy)

    def show_card(self):
        if not self.card.isVisible():
            self._position_card_near_bubble()
        self.card.show()

    def _clear_answer(self):
        self._cached_answer = None
        self.card.last_answer_text = ""
        self.card.set_lives(0, DOT_SPARE_LIVES)
        self.backend.display_cached("")

    def on_dot_clicked(self):
        if self._pending_snips:
            self.start_manual_snip()
            return

        if self._keep_answer_forever and self._has_captured_once:
            if self.card.isVisible():
                self.card.hide()
            else:
                self.show_card()
            return

        if not self._has_captured_once or self._reopen_lives <= 0:
            self._clear_answer()
            self.start_manual_snip()
            return

        self._reopen_lives -= 1
        self.card.set_lives(self._reopen_lives, DOT_SPARE_LIVES, keep_forever=self._keep_answer_forever)
        if self.card.isVisible():
            self.card.hide()
        else:
            self.show_card()

    def toggle_snip(self):
        self.on_dot_clicked()

    def start_manual_snip(self):
        if self._login_pending:
            self.backend.open_login_helper(modal=False)
            return

        if self._active_snip is not None and self._active_snip.isVisible():
            self._active_snip.cancel()

        if self.card.isVisible():
            self.card.hide()

        if self.done_btn.isVisible():
            self.done_btn.hide()

        cursor_qpos = QCursor.pos()
        screen = QApplication.screenAt(cursor_qpos) or self.bubble.screen() or QApplication.primaryScreen()
        if not screen:
            return

        cursor_phys = get_cursor_phys_pos()

        def grab_active():
            return capture_active_monitor(cursor_phys=cursor_phys)

        full_img, mon_bounds = hide_and_grab([self.bubble, self.pin_btn, self.star_btn, self.done_btn], grab_active)
        origin_phys = (mon_bounds[0], mon_bounds[1])
        self._open_snip_overlay(full_img, origin_phys, screen)

    def _open_snip_overlay(self, image: Image.Image, origin_phys: tuple[int, int], screen: QScreen):
        self._active_snip = SnipOverlay(image, origin_phys, screen)
        self._active_snip.snip_completed.connect(self._on_snip_completed)
        self._active_snip.snip_cancelled.connect(self._on_snip_cancelled)
        self._active_snip.show()

    def _on_snip_cancelled(self):
        self._active_snip = None
        if self._pending_snips:
            self.done_btn.set_count(len(self._pending_snips), MAX_SNIP_IMAGES)
            self.done_btn.reposition_near_bubble(self.bubble)
            self.done_btn.show()
        else:
            self.done_btn.hide()
            self.card.set_status("Snip cancelled")

    def _on_snip_completed(self, cropped_img: Image.Image):
        self._active_snip = None
        self._pending_snips.append(cropped_img)
        count = len(self._pending_snips)
        if count >= MAX_SNIP_IMAGES:
            self.on_done_clicked()
            return
        self.done_btn.set_count(count, MAX_SNIP_IMAGES)
        self.done_btn.reposition_near_bubble(self.bubble)
        self.done_btn.show()

    def on_done_clicked(self):
        if not self._pending_snips:
            return
        if self._active_snip is not None and self._active_snip.isVisible():
            self._active_snip.hide()
            self._active_snip.deleteLater()
            self._active_snip = None

        images = list(self._pending_snips)
        self._pending_snips.clear()
        self.done_btn.hide()

        self._has_captured_once = True
        self._reopen_lives = DOT_SPARE_LIVES
        self._last_snip_images = images
        self._last_snip_image = images[-1]
        self._cached_answer = None
        self.card.set_lives(self._reopen_lives, DOT_SPARE_LIVES, keep_forever=self._keep_answer_forever)
        status = "Sending image..." if len(images) == 1 else f"Sending {len(images)} images..."
        self.card.set_status(status)
        self.backend.submit_images(images, DEFAULT_IMAGE_PROMPT)
        self.show_card()

    def _on_answer_ready(self, answer_text: str):
        if answer_text.strip():
            self._cached_answer = answer_text
            self.card.last_answer_text = answer_text

    def show(self):
        self.bubble.show()

    def cleanup(self):
        if self._cleaned_up:
            return
        self._cleaned_up = True
        if self._active_snip is not None:
            self._active_snip.hide()
            self._active_snip.deleteLater()
            self._active_snip = None
        self._pending_snips.clear()
        self.card.hide()
        self.card.deleteLater()
        self.pin_btn.hide()
        self.pin_btn.deleteLater()
        self.star_btn.hide()
        self.star_btn.deleteLater()
        self.done_btn.hide()
        self.done_btn.deleteLater()
        self.bubble.hide()
        self.bubble.deleteLater()
        self.tray.hide()
        self.tray.setContextMenu(None)
        self.tray_menu.deleteLater()
        self.tray.deleteLater()
        self.backend.cleanup()
        app = QApplication.instance()
        if app is not None:
            app.processEvents()

    def quit(self):
        self.cleanup()
        QApplication.quit()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    try:
        from PyQt6.QtWebEngineCore import QWebEngineProfile
        raw_ua = QWebEngineProfile.defaultProfile().httpUserAgent()
        if "QtWebEngine" in raw_ua:
            clean_ua = re.sub(r"QtWebEngine/\S+\s*", "", raw_ua).strip()
            QWebEngineProfile.defaultProfile().setHttpUserAgent(clean_ua)
    except Exception:
        pass

    if "--login" in sys.argv:
        backend = create_backend("chrome")
        backend.open_login_helper()
        backend.cleanup()
        return 0

    is_smoke = "--smoke-test" in sys.argv

    lock_path = APP_DATA_DIR / "quickread.lock"
    lock_file = QLockFile(str(lock_path))
    if not lock_file.tryLock(100):
        if not is_smoke:
            print("Another instance of QuickRead AI Overlay is already running.")
            return 0

    if is_smoke:
        print("[Smoke Test] Initializing QuickRead AI Overlay...")
        qa_app = QuickReadApp(is_smoke_test=True)
        qa_app.show()
        qa_app.cleanup()
        print("[Smoke Test] Verification completed successfully.")
        lock_file.unlock()
        return 0

    qa_app = QuickReadApp(is_smoke_test=False)
    qa_app.show()
    QTimer.singleShot(0, qa_app.begin_login)
    app.aboutToQuit.connect(qa_app.cleanup)

    try:
        res = app.exec()
    finally:
        qa_app.cleanup()
        lock_file.unlock()
    return res


if __name__ == "__main__":
    sys.exit(main())

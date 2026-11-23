"""
Chrome-driven Google AI Mode backend.

Google rejects sign-in inside embedded browsers, so this backend runs the user's
REAL Chrome (own profile folder, login persisted there) and controls one tab over
the Chrome DevTools Protocol (CDP) on 127.0.0.1. After login, Chrome is restarted
on the same profile with its window hidden via Win32 SW_HIDE (no headless UA, no
automatic pop-up after login).
"""
import ctypes
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Sequence

from PIL import Image
from PyQt6.QtCore import QEventLoop, QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtWebSockets import QWebSocket
from PyQt6.QtWidgets import QVBoxLayout, QWidget

import src.config as config
from .ai_backend import (
    AIBackend, EXTRACT_AI_ANSWER_JS, SESSION_COOKIE_NAMES, _OCRWorkerThread,
    _create_transparent_browser, _set_browser_text_preserving_scroll,
    clean_extracted_ai_text, save_upload_pngs,
)

LOGIN_URL = (
    "https://accounts.google.com/ServiceLogin?hl=en&continue="
    + urllib.parse.quote(config.GOOGLE_AI_MODE_HOME, safe="")
)

FIND_FILE_INPUT_JS = (
    "document.querySelector('input[type=\"file\"][accept*=\"image\"]') "
    "|| document.querySelector('input[type=\"file\"]')"
)

CLICK_UPLOAD_BUTTON_JS = r"""
(function() {
    const sels = 'button[aria-label*="image" i], button[aria-label*="upload" i], '
        + 'button[aria-label*="attach" i], button[aria-label*="lens" i], '
        + 'button[aria-label*="photo" i], button[aria-label*="add" i], '
        + '[role="button"][aria-label*="image" i], [role="button"][aria-label*="upload" i]';
    for (const el of document.querySelectorAll(sels)) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0 && !el.disabled) {
            el.click();
            return true;
        }
    }
    return false;
})()
"""

CLICK_UPLOAD_MENU_ITEM_JS = r"""
(function() {
    const groups = [
        '[role="menuitem"], [role="option"], [role="menu"] button, [role="menu"] [role="button"], [role="listbox"] button, [role="dialog"] [role="button"]',
        '[role="menu"] *, [role="listbox"] *'
    ];
    for (const sel of groups) {
        for (const el of document.querySelectorAll(sel)) {
            const r = el.getBoundingClientRect();
            const t = (el.innerText || el.getAttribute('aria-label') || '').trim();
            if (r.width > 0 && r.height > 0 && t.length < 60 && /upload|from (your )?(computer|device)|photo|image|file/i.test(t)) {
                el.click();
                return true;
            }
        }
    }
    return false;
})()
"""

FOCUS_BOX_JS = r"""
(function() {
    const boxes = document.querySelectorAll('textarea, input[name="q"], [contenteditable="true"], [role="textbox"]');
    for (const b of boxes) {
        const r = b.getBoundingClientRect();
        if (r.width > 0 && r.height > 0 && !b.disabled) {
            b.focus();
            return true;
        }
    }
    return false;
})()
"""

BOX_HAS_TEXT_JS = r"""
(function() {
    const boxes = document.querySelectorAll('textarea, input[name="q"], [contenteditable="true"], [role="textbox"]');
    for (const b of boxes) {
        const r = b.getBoundingClientRect();
        if (r.width > 0 && r.height > 0 && !b.disabled) {
            const val = b.value !== undefined ? b.value : (b.innerText || '');
            return val.trim().length > 0;
        }
    }
    return false;
})()
"""

CLICK_SEND_BUTTON_JS = r"""
(function() {
    const boxes = document.querySelectorAll('textarea, input[name="q"], [contenteditable="true"], [role="textbox"]');
    let box = null;
    for (const b of boxes) {
        const r = b.getBoundingClientRect();
        if (r.width > 0 && r.height > 0 && !b.disabled) {
            box = b;
            break;
        }
    }
    const form = box ? box.closest('form') : null;
    const isRectVisible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const isLayoutVisible = el => el.offsetWidth > 0 && el.offsetHeight > 0 && getComputedStyle(el).visibility !== 'hidden';
    const isEnabled = el => !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const isNoise = el => {
        if (el.closest('nav, [role="navigation"], [aria-hidden="true"], [inert]')) return true;
        const aria = (el.getAttribute('aria-label') || '').trim();
        return /feedback/i.test(aria) || aria.length > 60;
    };
    const candidates = [];
    const exactRe = /^(send|send\s+(message|prompt|query)|submit)$/i;
    const addUnique = el => {
        if (!isNoise(el) && !candidates.includes(el)) candidates.push(el);
    };
    for (const el of document.querySelectorAll('button[aria-label], [role="button"][aria-label]')) {
        if (exactRe.test((el.getAttribute('aria-label') || '').trim())) addUnique(el);
    }
    if (form) {
        for (const el of form.querySelectorAll('button[aria-label*="send" i], [role="button"][aria-label*="send" i], button[aria-label*="submit" i], button[type="submit"]')) {
            addUnique(el);
        }
    }
    for (const el of document.querySelectorAll('button[aria-label*="send" i], [role="button"][aria-label*="send" i], button[aria-label*="submit" i]')) {
        addUnique(el);
    }
    let hasVisible = false;
    for (const check of [isRectVisible, isLayoutVisible]) {
        for (const el of candidates) {
            if (!check(el)) continue;
            hasVisible = true;
            if (isEnabled(el)) {
                el.click();
                return 'clicked';
            }
        }
    }
    return hasVisible ? 'waiting' : 'missing';
})()
"""

CHECK_PREUPLOAD_JS = r"""
(function() {
    const href = String(location.href || '');
    if (href.includes('/sorry/') || document.querySelector('#captcha-form, iframe[src*="recaptcha"]')) {
        return { state: 'captcha' };
    }
    if (href.includes('consent.google.com')) {
        return { state: 'consent' };
    }
    if (!document.body) {
        return { state: 'loading' };
    }
    const raw = (document.body.innerText || '').trim();
    const low = raw.toLowerCase();
    if (raw.length < 500 && (low.includes('sign in to continue') || low.includes('sign in to use ai mode'))) {
        return { state: 'signin_required' };
    }
    if (raw.length < 1500 && low.includes('ai mode is not currently available')) {
        return { state: 'unavailable' };
    }
    return { state: 'ok' };
})()
"""


def build_check_home_ready_js(home_url: str) -> str:
    base = home_url.split("#", 1)[0].split("?", 1)[0]
    base_json = json.dumps(base)
    return f"""
(function() {{
    const href = String(location.href || '');
    if (href.includes('/sorry/') || document.querySelector('#captcha-form, iframe[src*="recaptcha"]')) {{
        return 'blocked:captcha';
    }}
    if (href.includes('consent.google.com')) {{
        return 'blocked:consent';
    }}
    if (!document.body || (document.readyState !== 'complete' && document.readyState !== 'interactive')) {{
        return 'reload';
    }}
    const raw = (document.body.innerText || '').trim();
    const low = raw.toLowerCase();
    if (raw.length < 500 && (low.includes('sign in to continue') || low.includes('sign in to use ai mode'))) {{
        return 'blocked:signin_required';
    }}
    if (raw.length < 1500 && low.includes('ai mode is not currently available')) {{
        return 'blocked:unavailable';
    }}
    const expectedBase = {base_json};
    if (!href.startsWith(expectedBase) || /[?&]q=[^&]+/.test(href) || window.__aiSubmissionState === 'submitted') {{
        return 'reload';
    }}
    if (document.querySelector('input[type="file"]')) {{
        return 'ready';
    }}
    const boxes = document.querySelectorAll('textarea, input[name="q"], [contenteditable="true"], [role="textbox"]');
    for (const b of boxes) {{
        const r = b.getBoundingClientRect();
        if (r.width > 0 && r.height > 0 && !b.disabled) {{
            const val = b.value !== undefined ? b.value : (b.innerText || '');
            if (val.trim().length === 0) return 'ready';
        }}
    }}
    return 'reload';
}})()
"""


def find_chrome() -> Optional[str]:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        Path(pf) / "Google/Chrome/Application/chrome.exe",
        Path(pf86) / "Google/Chrome/Application/chrome.exe",
        Path(local) / "Google/Chrome/Application/chrome.exe",
        Path(pf86) / "Microsoft/Edge/Application/msedge.exe",
        Path(pf) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def build_chrome_args(chrome: str, profile_dir, port: int, headless: bool, url: str,
                      minimized: bool = False) -> list:
    args = [
        chrome,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion",
    ]
    if headless:
        args.append("--headless=new")
    elif minimized:
        args.append("--start-minimized")
    args.append(url)
    return args


BLOCKED_STATES = ("captcha", "consent", "signin_required", "unavailable")


def _hide_chrome_windows(pid: int) -> tuple[int, int]:
    """Hide top-level Chrome_WidgetWin_1 windows owned by `pid`; returns (found, visible)."""
    if os.name != "nt" or not pid:
        return (0, 0)
    user32 = ctypes.windll.user32
    wnd_enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    found = 0
    still_visible = 0

    def enum_cb(hwnd, _):
        nonlocal found, still_visible
        proc_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if proc_id.value == pid:
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value == "Chrome_WidgetWin_1":
                found += 1
                if user32.IsWindowVisible(hwnd):
                    user32.ShowWindow(hwnd, 0)  # SW_HIDE
                if user32.IsWindowVisible(hwnd):
                    still_visible += 1
        return True

    user32.EnumWindows(wnd_enum_proc(enum_cb), 0)
    return (found, still_visible)


def _http_json(port: int, path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=0.4) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


class CDPClient(QObject):
    """Minimal async Chrome DevTools Protocol client over QWebSocket."""
    notification = pyqtSignal(str, dict)
    connected = pyqtSignal()
    closed = pyqtSignal()
    failed = pyqtSignal()

    def __init__(self, ws_url: str, parent=None):
        super().__init__(parent)
        self._id = 0
        self._cbs: dict = {}
        self._ws = QWebSocket()
        self._ws.connected.connect(self.connected)
        self._ws.disconnected.connect(self.closed)
        self._ws.errorOccurred.connect(lambda _e: self.failed.emit())
        self._ws.textMessageReceived.connect(self._on_message)
        self._ws.open(QUrl(ws_url))

    def send(self, method: str, params: Optional[dict] = None, cb: Optional[Callable] = None) -> None:
        self._id += 1
        if cb is not None:
            self._cbs[self._id] = cb
        self._ws.sendTextMessage(json.dumps({"id": self._id, "method": method, "params": params or {}}))

    def _on_message(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if "id" in msg:
            cb = self._cbs.pop(msg["id"], None)
            if cb is not None:
                cb(msg.get("result"), msg.get("error"))
        elif "method" in msg:
            self.notification.emit(msg["method"], msg.get("params") or {})

    def close(self) -> None:
        self._ws.close()


class ChromeBackend(AIBackend):
    requires_login = True

    def __init__(self, parent=None):
        super().__init__(parent)
        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(self._container)
        layout.setContentsMargins(2, 0, 2, 0)
        self.output_view = _create_transparent_browser(self._container)
        layout.addWidget(self.output_view)

        self._proc: Optional[subprocess.Popen] = None
        self._cdp: Optional[CDPClient] = None
        self._cdp_ready = False
        self._launching = False
        self._ready_cbs: list = []
        self._headless_active = False
        self._hide_active = False
        self._hide_deadline = 0.0
        self._hide_timer = QTimer(self)
        self._hide_timer.setInterval(50)
        self._hide_timer.timeout.connect(self._hide_tick)
        self._logged_in = False

        self._login_active = False
        self._finishing = False
        self._login_closing = False
        self._login_timer = QTimer(self)
        self._login_timer.setInterval(500)
        self._login_timer.timeout.connect(self._login_tick)

        self._after_load: Optional[Callable] = None
        self._nav_token: Optional[object] = None
        self._chooser_cb: Optional[Callable] = None
        self._upload_deadline = 0.0
        self._upload_attempts = 0
        self._submit_deadline = 0.0
        self._pending_prompt = ""

        self._home_state = "idle"
        self._home_waiters: list = []
        self._launch_url = ""
        self._op_token = 0
        self._home_prewarm_timer = QTimer(self)
        self._home_prewarm_timer.setSingleShot(True)
        self._home_prewarm_timer.setInterval(150)
        self._home_prewarm_timer.timeout.connect(self._prewarm_home)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(150)
        self._poll_timer.timeout.connect(self._poll_stream)
        self._active_full_query = ""
        self._active_cleaned_input = ""
        self._awaiting_submit = False
        self._upload_failed_flag = False
        self._inject_ts = 0.0
        self._last_clean_answer = ""
        self._stable_ticks = 0
        self._load_done = False
        self._start_ts = 0.0

        self._last_image: Optional[Image.Image] = None
        self._last_images: list[Image.Image] = []
        self._pending_upload_files: list[str] = []
        self._ocr_worker: Optional[_OCRWorkerThread] = None
        self._ocr_fallback_text = ""
        self._ocr_done_flag = False
        self._ocr_started = False
        self._fallback_triggered = False

    # ---- AIBackend API -------------------------------------------------
    def widget(self) -> QWidget:
        return self._container

    def display_cached(self, text: str) -> None:
        self._poll_timer.stop()
        self.output_view.setPlainText(text)

    def is_logged_in(self) -> bool:
        return self._logged_in

    # ---- Chrome process / connection ----------------------------------
    def _page_info(self):
        targets = _http_json(config.CHROME_DEBUG_PORT, "/json")
        for t in targets or []:
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                return t["webSocketDebuggerUrl"], t.get("url", "")
        return None

    def _drop_connection(self) -> None:
        self._cdp_ready = False
        self._home_state = "idle"
        self._home_waiters.clear()
        self._home_prewarm_timer.stop()
        if self._cdp is not None:
            try:
                self._cdp.closed.disconnect()
            except TypeError:
                pass
            self._cdp.close()
            self._cdp.deleteLater()
            self._cdp = None

    def _hide_tick(self) -> None:
        if not self._hide_active or self._proc is None or self._proc.poll() is not None:
            self._hide_timer.stop()
            return
        found, still_visible = _hide_chrome_windows(self._proc.pid)
        if (found > 0 and still_visible == 0) or time.monotonic() > self._hide_deadline:
            self._hide_timer.stop()

    def _launch(self, url: str, headless: bool, on_ready: Callable, minimized: bool = False,
                hide: bool = False) -> None:
        chrome = find_chrome()
        if not chrome:
            self._fail("Chrome was not found. Please install Google Chrome.")
            return
        args = build_chrome_args(chrome, config.CHROME_PROFILE_DIR, config.CHROME_DEBUG_PORT,
                                 headless, url, minimized)
        popen_kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if hide and os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 7  # SW_SHOWMINNOACTIVE
            popen_kwargs["startupinfo"] = si
        self._launch_url = url
        self._home_state = "loading" if (url == config.GOOGLE_AI_MODE_HOME and not self._login_active) else "idle"
        self._proc = subprocess.Popen(args, **popen_kwargs)
        self._headless_active = headless
        self._hide_active = hide and not headless
        self._launching = True
        if self._hide_active:
            self._hide_deadline = time.monotonic() + 5.0
            self._hide_timer.start()
            _hide_chrome_windows(self._proc.pid)
        else:
            self._hide_timer.stop()
        self._connect_attempt(time.monotonic() + 20.0, on_ready)

    def _connect_attempt(self, deadline: float, on_ready: Callable) -> None:
        info = self._page_info()
        if info is None:
            if time.monotonic() > deadline:
                self._launching = False
                self._fail("Could not connect to Chrome.")
            else:
                QTimer.singleShot(200, lambda: self._connect_attempt(deadline, on_ready))
            return
        self._cdp = CDPClient(info[0], self)
        self._cdp.connected.connect(lambda: self._on_connected(on_ready))
        self._cdp.failed.connect(lambda: self._connect_failed())
        self._cdp.closed.connect(self._on_cdp_closed)
        self._cdp.notification.connect(self._on_event)

    def _on_connected(self, on_ready: Callable) -> None:
        self._cdp_ready = True
        self._launching = False
        if self._hide_active and self._proc is not None:
            _hide_chrome_windows(self._proc.pid)

        def after_page_enable(_r, _e):
            if not self._cdp_ready:
                return
            if not self._login_active:
                self._cdp.send("Page.setInterceptFileChooserDialog", {"enabled": True})
                if self._home_state == "loading" and self._launch_url == config.GOOGLE_AI_MODE_HOME:
                    self._wait_initial_home_ready()
            on_ready()

        self._cdp.send("Page.enable", {}, after_page_enable)

    def _connect_failed(self) -> None:
        if not self._cdp_ready:
            self._launching = False
            self._fail("Could not connect to Chrome.")

    def _on_cdp_closed(self) -> None:
        self._cdp_ready = False
        self._home_state = "idle"
        self._home_waiters.clear()
        self._home_prewarm_timer.stop()

    def _fail(self, msg: str) -> None:
        self._launching = False
        self._hide_timer.stop()
        self._home_prewarm_timer.stop()
        self._home_state = "idle"
        self._home_waiters.clear()
        self._ready_cbs.clear()
        self._poll_timer.stop()
        self.output_view.setPlainText(msg)
        self.status_changed.emit("Chrome error")
        if self._login_active and not self._finishing:
            self._finish_login()

    def _stop_chrome(self, then: Callable) -> None:
        """Gracefully close Chrome (flushes cookies to disk), then call `then`."""
        self._hide_timer.stop()
        self._hide_active = False
        self._drop_connection()
        proc = self._proc
        version = _http_json(config.CHROME_DEBUG_PORT, "/json/version")
        if (proc is None or proc.poll() is not None) and version is None:
            self._proc = None
            QTimer.singleShot(0, then)
            return
        if version and version.get("webSocketDebuggerUrl"):
            tmp = CDPClient(version["webSocketDebuggerUrl"], self)
            tmp.connected.connect(lambda: tmp.send("Browser.close"))
        deadline = time.monotonic() + 10.0

        def tick():
            exited = proc is None or proc.poll() is not None
            port_free = _http_json(config.CHROME_DEBUG_PORT, "/json/version") is None
            if exited and port_free:
                self._proc = None
                QTimer.singleShot(300, then)
            elif time.monotonic() > deadline:
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                self._proc = None
                QTimer.singleShot(1000, then)
            else:
                QTimer.singleShot(150, tick)

        QTimer.singleShot(150, tick)

    # ---- Login ---------------------------------------------------------
    def open_login_helper(self, modal: bool = True) -> None:
        if self._login_active:
            if self._cdp_ready:
                self._cdp.send("Page.bringToFront")
            return
        self._login_active = True
        self._finishing = False
        self._login_closing = False
        self.status_changed.emit("Log in to Google in Chrome")

        def start():
            self._launch(LOGIN_URL, headless=False, on_ready=lambda: None)
            self._login_timer.start()

        self._stop_chrome(start)
        if modal:
            loop = QEventLoop()
            self.login_finished.connect(loop.quit)
            loop.exec()
            self.login_finished.disconnect(loop.quit)

    def _login_tick(self) -> None:
        if self._finishing:
            return
        if self._proc is not None and self._proc.poll() is not None and not self._launching:
            self._finish_login()
            return
        if self._cdp_ready and not self._login_closing:
            self._cdp.send("Network.getAllCookies", {}, self._on_login_cookies)

    def _on_login_cookies(self, result, error) -> None:
        cookies = (result or {}).get("cookies", [])
        self._logged_in = any(
            c.get("name") in SESSION_COOKIE_NAMES and c.get("domain", "").lstrip(".").endswith("google.com")
            for c in cookies
        )
        if self._logged_in and not self._login_closing:
            info = self._page_info()
            url = info[1] if info else ""
            if "accounts.google.com" not in url:
                self._login_closing = True
                QTimer.singleShot(config.LOGIN_AUTOCLOSE_MS, self._finish_login)

    def _finish_login(self) -> None:
        if not self._login_active or self._finishing:
            return
        self._finishing = True
        self._login_timer.stop()
        self._stop_chrome(self._login_done)

    def _login_done(self) -> None:
        self._login_active = False
        self._finishing = False
        self._login_closing = False
        self.login_finished.emit()

    # ---- Background engine ---------------------------------------------
    def prewarm(self) -> None:
        self._home_prewarm_timer.stop()
        self._ensure_bg(self._prewarm_home, initial_url=config.GOOGLE_AI_MODE_HOME)

    def _prewarm_home(self) -> None:
        if not self._cdp_ready or self._awaiting_submit or self._poll_timer.isActive() or self._login_active:
            return
        if self._home_state in ("loading", "ready"):
            return
        self._home_state = "loading"
        self._navigate(config.GOOGLE_AI_MODE_HOME, self._on_home_loaded)

    def _wait_initial_home_ready(self) -> None:
        if not self._cdp_ready:
            return
        token = object()
        self._nav_token = token
        self._after_load = self._on_home_loaded
        self._fallback_load_check(token, retry_deadline=time.monotonic() + 12.0, require_non_blank=True)

    def _on_home_loaded(self) -> None:
        if self._home_state != "loading":
            return
        self._home_state = "ready"
        waiters, self._home_waiters = self._home_waiters, []
        for waiter in waiters:
            waiter()

    def _ensure_bg(self, cb: Callable, initial_url: Optional[str] = None) -> None:
        if self._cdp_ready and self._proc is not None and self._proc.poll() is None:
            cb()
            return
        self._ready_cbs.append(cb)
        if self._launching:
            return
        self._launching = True
        start_url = initial_url or config.GOOGLE_AI_MODE_HOME

        def launch():
            headless = config.CHROME_HEADLESS
            hide = (not headless) and config.CHROME_HIDE_WINDOW
            self._launch(start_url, headless, self._run_ready_cbs,
                         minimized=not headless, hide=hide)

        self._stop_chrome(launch)

    def _run_ready_cbs(self) -> None:
        cbs, self._ready_cbs = self._ready_cbs, []
        for cb in cbs:
            cb()

    def _on_event(self, method: str, params: dict) -> None:
        if method == "Page.loadEventFired" and self._after_load is not None:
            self._fallback_load_check(self._nav_token, require_non_blank=(self._home_state == "loading"))
        elif method == "Page.fileChooserOpened" and self._chooser_cb is not None:
            cb, self._chooser_cb = self._chooser_cb, None
            cb(params)

    def _navigate(self, url: str, after_load: Callable) -> None:
        if not self._cdp_ready:
            return
        self._after_load = None
        token = object()
        self._nav_token = token
        non_blank = (url != "about:blank")

        def after_mark(_val):
            if self._nav_token is not token or not self._cdp_ready:
                return
            self._after_load = after_load

            def on_nav_reply(result, _error):
                if self._nav_token is token and self._after_load is not None and result and not result.get("errorText"):
                    QTimer.singleShot(
                        150,
                        lambda: self._fallback_load_check(
                            token, retry_deadline=time.monotonic() + 12.0, require_non_blank=non_blank
                        ),
                    )

            self._cdp.send("Page.navigate", {"url": url}, on_nav_reply)

        self._eval("window.__aiNavMark = 1; true", after_mark)

    def _fallback_load_check(self, token: object, retry_deadline: float = 0.0, require_non_blank: bool = False) -> None:
        if self._nav_token is not token or self._after_load is None or not self._cdp_ready:
            return

        def on_done(ready):
            if self._nav_token is not token or self._after_load is None:
                return
            if ready:
                cb, self._after_load = self._after_load, None
                cb()
            elif retry_deadline and time.monotonic() < retry_deadline:
                QTimer.singleShot(
                    100,
                    lambda: self._fallback_load_check(
                        token, retry_deadline=retry_deadline, require_non_blank=require_non_blank
                    ),
                )

        if require_non_blank:
            expr = (
                "(() => {"
                "if (window.__aiNavMark) return false;"
                "const h = String(location.href || '');"
                "if (!h || h === 'about:blank') return false;"
                "if (document.readyState === 'complete') return true;"
                "if (document.readyState !== 'interactive') return false;"
                "const b = document.querySelector('textarea, input[name=\"q\"], [contenteditable=\"true\"], [role=\"textbox\"]');"
                "const u = document.querySelector('input[type=\"file\"], button[aria-label*=\"add\" i], button[aria-label*=\"upload\" i], button[aria-label*=\"image\" i]');"
                "return !!(b && u && b.getBoundingClientRect().width > 0);"
                "})()"
            )
        else:
            expr = "!window.__aiNavMark && document.readyState === 'complete'"
        self._eval(expr, on_done)

    def _eval(self, expr: str, cb: Callable, by_value: bool = True, user_gesture: bool = False) -> None:
        if not self._cdp_ready:
            return

        def done(result, _error):
            res = (result or {}).get("result") or {}
            cb(res.get("value") if by_value else res)

        params = {"expression": expr, "returnByValue": by_value}
        if user_gesture:
            params["userGesture"] = True
        self._cdp.send("Runtime.evaluate", params, done)

    def _reset_answer_state(self, full_query: str, cleaned_input: str, keep_home_load: bool = False) -> None:
        self._op_token += 1
        self._poll_timer.stop()
        self._home_prewarm_timer.stop()
        self._chooser_cb = None
        self._home_waiters.clear()
        if not keep_home_load:
            self._after_load = None
            if self._home_state in ("loading", "ready"):
                self._home_state = "used"
        self._active_full_query = full_query
        self._active_cleaned_input = cleaned_input
        self._last_clean_answer = ""
        self._stable_ticks = 0
        self._load_done = False
        self._inject_ts = 0.0
        self._upload_failed_flag = False
        self._start_ts = time.monotonic()

    # ---- Submit --------------------------------------------------------
    def submit_images(self, images: Sequence[Image.Image], prompt: str = config.DEFAULT_IMAGE_PROMPT) -> None:
        if not images:
            return
        self._reset_answer_state(prompt, "", keep_home_load=True)
        self._last_images = list(images)
        self._last_image = self._last_images[0]
        self._ocr_fallback_text = ""
        self._ocr_done_flag = False
        self._ocr_started = False
        self._fallback_triggered = False

        paths = save_upload_pngs(
            self._last_images,
            config.LAST_SNIP_FILE,
            getattr(config, "MAX_UPLOAD_IMAGE_DIM", 1600),
        )
        self._pending_upload_files = [str(p) for p in paths]
        self._pending_prompt = prompt
        self._awaiting_submit = True
        status = "Sending image..." if len(self._last_images) == 1 else f"Sending {len(self._last_images)} images..."
        self.output_view.setPlainText(status)
        self.status_changed.emit(status)
        self._poll_timer.start()
        self._ensure_bg(self._begin_upload, initial_url=config.GOOGLE_AI_MODE_HOME)

    def submit_image(self, image: Image.Image, prompt: str = config.DEFAULT_IMAGE_PROMPT) -> None:
        self.submit_images([image], prompt)

    def submit(self, full_query: str, cleaned_input: str = "") -> None:
        self._reset_answer_state(full_query, cleaned_input, keep_home_load=False)
        self._last_image = None
        self._last_images = []
        self._pending_upload_files = []
        self._awaiting_submit = False
        self.output_view.setPlainText("Thinking...")
        self.status_changed.emit("Thinking...")
        self._poll_timer.start()
        url = f"{config.GOOGLE_AI_MODE_HOME}&q={urllib.parse.quote_plus(full_query)}"

        def go():
            self._home_state = "used"
            self._navigate(url, lambda: setattr(self, "_load_done", True))

        self._ensure_bg(go, initial_url="about:blank")

    def _begin_upload(self) -> None:
        if not self._awaiting_submit or not self._cdp_ready:
            return
        op = self._op_token
        self._upload_deadline = time.monotonic() + 20.0
        self._upload_attempts = 0
        self._cdp.send("Page.setInterceptFileChooserDialog", {"enabled": True})

        if self._home_state == "ready":
            self._home_state = "used"

            def on_checked(status):
                if op != self._op_token or not self._awaiting_submit or not self._cdp_ready:
                    return
                if status == "ready":
                    self._load_done = True
                    if self._hide_active and self._proc is not None:
                        _hide_chrome_windows(self._proc.pid)
                    self._upload_attempt()
                elif isinstance(status, str) and status.startswith("blocked:"):
                    self._awaiting_submit = False
                    self._on_blocked(status.split(":", 1)[1])
                else:
                    self._navigate(config.GOOGLE_AI_MODE_HOME, self._on_page_loaded)

            self._eval(build_check_home_ready_js(config.GOOGLE_AI_MODE_HOME), on_checked)
            return

        if self._home_state == "loading":
            def on_home_ready():
                if op != self._op_token or not self._awaiting_submit or not self._cdp_ready:
                    return
                self._home_state = "used"
                self._on_page_loaded()

            self._home_waiters.append(on_home_ready)
            if self._nav_token is not None and self._after_load is not None:
                self._fallback_load_check(self._nav_token, retry_deadline=time.monotonic() + 10.0, require_non_blank=True)
            return

        self._home_state = "used"
        self._navigate(config.GOOGLE_AI_MODE_HOME, self._on_page_loaded)

    def _on_page_loaded(self) -> None:
        if not self._awaiting_submit or not self._cdp_ready:
            return
        self._load_done = True
        if self._hide_active and self._proc is not None:
            _hide_chrome_windows(self._proc.pid)
        self._eval(CHECK_PREUPLOAD_JS, lambda r: self._handle_preupload_state(r, self._upload_attempt))

    def _handle_preupload_state(self, res, on_ok: Callable) -> None:
        if not self._awaiting_submit:
            return
        if isinstance(res, dict) and res.get("state") in BLOCKED_STATES:
            self._awaiting_submit = False
            self._on_blocked(res["state"])
            return
        on_ok()

    def _upload_attempt(self) -> None:
        if not self._awaiting_submit or not self._cdp_ready:
            return
        if time.monotonic() > self._upload_deadline:
            self._upload_failed()
            return
        self._eval(FIND_FILE_INPUT_JS, self._on_file_input, by_value=False)

    def _retry_upload(self) -> None:
        op = self._op_token
        QTimer.singleShot(200, lambda: self._upload_attempt() if op == self._op_token else None)

    def _upload_file_paths(self) -> list[str]:
        if self._pending_upload_files:
            return list(self._pending_upload_files)
        return [str(config.LAST_SNIP_FILE)]

    def _on_file_input(self, res) -> None:
        if not self._awaiting_submit or not self._cdp_ready:
            return
        oid = (res or {}).get("objectId")
        if oid:
            self._chooser_cb = None
            self._cdp.send("DOM.setFileInputFiles",
                           {"files": self._upload_file_paths(), "objectId": oid}, self._after_attach)
            return
        self._try_click_chooser(CLICK_UPLOAD_MENU_ITEM_JS, self._try_click_upload_button, fast_poll_menu=False)

    def _try_click_upload_button(self) -> None:
        def check_state():
            self._eval(CHECK_PREUPLOAD_JS, lambda r: self._handle_preupload_state(r, self._retry_upload))

        if self._upload_attempts >= 4:
            check_state()
            return
        self._upload_attempts += 1
        self._try_click_chooser(CLICK_UPLOAD_BUTTON_JS, check_state, fast_poll_menu=True)

    def _try_click_chooser(self, js_expr: str, on_miss: Callable, fast_poll_menu: bool = False) -> None:
        op = self._op_token
        self._chooser_cb = self._on_chooser

        def after(clicked):
            if op != self._op_token or not self._awaiting_submit:
                self._chooser_cb = None
                return
            if clicked:
                if fast_poll_menu:
                    QTimer.singleShot(60, lambda: self._poll_after_upload_click(op, 0))
                else:
                    QTimer.singleShot(80, lambda: self._poll_after_menu_click(op, 0))
            else:
                self._chooser_cb = None
                on_miss()

        self._eval(js_expr, after, user_gesture=True)

    def _poll_after_upload_click(self, op: int, tries: int) -> None:
        if op != self._op_token or not self._awaiting_submit or not self._cdp_ready or self._chooser_cb is None:
            return

        def on_menu_clicked(clicked):
            if op != self._op_token or not self._awaiting_submit or self._chooser_cb is None:
                return
            if clicked:
                QTimer.singleShot(80, lambda: self._poll_after_menu_click(op, 0))
            elif tries < 12:
                QTimer.singleShot(60, lambda: self._poll_after_upload_click(op, tries + 1))
            else:
                self._chooser_cb = None
                self._retry_upload()

        def on_input(res):
            if op != self._op_token or not self._awaiting_submit or self._chooser_cb is None:
                return
            oid = (res or {}).get("objectId")
            if oid:
                self._chooser_cb = None
                self._cdp.send("DOM.setFileInputFiles",
                               {"files": self._upload_file_paths(), "objectId": oid}, self._after_attach)
                return
            self._eval(CLICK_UPLOAD_MENU_ITEM_JS, on_menu_clicked, user_gesture=True)

        self._eval(FIND_FILE_INPUT_JS, on_input, by_value=False)

    def _poll_after_menu_click(self, op: int, tries: int) -> None:
        if op != self._op_token or not self._awaiting_submit or not self._cdp_ready or self._chooser_cb is None:
            return

        def on_input(res):
            if op != self._op_token or not self._awaiting_submit or self._chooser_cb is None:
                return
            oid = (res or {}).get("objectId")
            if oid:
                self._chooser_cb = None
                self._cdp.send("DOM.setFileInputFiles",
                               {"files": self._upload_file_paths(), "objectId": oid}, self._after_attach)
                return
            if tries < 7:
                QTimer.singleShot(80, lambda: self._poll_after_menu_click(op, tries + 1))
            else:
                self._chooser_cb = None
                self._retry_upload()

        self._eval(FIND_FILE_INPUT_JS, on_input, by_value=False)

    def _on_chooser(self, params: dict) -> None:
        args = {"files": self._upload_file_paths()}
        if "backendNodeId" in params:
            args["backendNodeId"] = params["backendNodeId"]
        self._cdp.send("DOM.setFileInputFiles", args, self._after_attach)

    def _after_attach(self, _result, error) -> None:
        if error:
            self._retry_upload()
            return
        op = self._op_token
        # Dismiss any open upload popup menu (e.g. '+' dropdown) before typing/sending.
        for t in ("rawKeyDown", "keyUp"):
            self._cdp.send("Input.dispatchKeyEvent", {
                "type": t, "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27,
            })
        self.status_changed.emit("Thinking...")
        self._submit_deadline = time.monotonic() + 25.0
        QTimer.singleShot(80, lambda: self._type_prompt() if op == self._op_token else None)

    def _type_prompt(self) -> None:
        if not self._cdp_ready or not self._awaiting_submit:
            return
        if not self._pending_prompt.strip():
            self._submit_when_ready()
            return
        if time.monotonic() > self._submit_deadline:
            self._upload_failed()
            return
        op = self._op_token

        def checked(has_text):
            if op != self._op_token or not self._cdp_ready or not self._awaiting_submit:
                return
            if has_text:
                self._submit_when_ready()
            else:
                QTimer.singleShot(100, lambda: self._type_prompt() if op == self._op_token else None)

        def focused(ok):
            if op != self._op_token or not self._cdp_ready or not self._awaiting_submit:
                return
            if not ok:
                QTimer.singleShot(100, lambda: self._type_prompt() if op == self._op_token else None)
                return
            self._cdp.send("Input.insertText", {"text": self._pending_prompt},
                           lambda _r, _e: self._eval(BOX_HAS_TEXT_JS, checked))

        self._eval(FOCUS_BOX_JS, focused)

    def _submit_when_ready(self) -> None:
        if not self._cdp_ready or not self._awaiting_submit:
            return
        op = self._op_token

        def got(status):
            if op != self._op_token or not self._cdp_ready or not self._awaiting_submit:
                return
            if status == "clicked":
                self._mark_submitted()
            elif time.monotonic() > self._submit_deadline:
                if status == "missing":
                    for t in ("rawKeyDown", "keyUp"):
                        self._cdp.send("Input.dispatchKeyEvent", {
                            "type": t, "key": "Enter", "code": "Enter",
                            "windowsVirtualKeyCode": 13, "text": "\r" if t == "rawKeyDown" else "",
                        })
                    self._mark_submitted()
                else:
                    self._upload_failed()
            else:
                QTimer.singleShot(100, lambda: self._submit_when_ready() if op == self._op_token else None)

        self._eval(CLICK_SEND_BUTTON_JS, got, user_gesture=True)

    def _mark_submitted(self) -> None:
        self._eval("window.__aiSubmissionState = 'submitted'; true", lambda _v: None)
        self._awaiting_submit = False
        self._inject_ts = time.monotonic()

    # ---- OCR fallback (lazy: runs only if image upload actually fails) --
    def _start_ocr_fallback(self) -> None:
        if self._ocr_started:
            return
        self._ocr_started = True
        if not self._last_images and self._last_image is None:
            self._ocr_done_flag = True
            return
        try:
            if self._ocr_worker is not None:
                try:
                    self._ocr_worker.ocr_done.disconnect(self._on_ocr_done)
                except TypeError:
                    pass
            if len(self._last_images) > 1:
                ocr_target = list(self._last_images)
            else:
                ocr_target = self._last_images[0] if self._last_images else self._last_image
            worker = _OCRWorkerThread(ocr_target, parent=self)
            worker.ocr_done.connect(self._on_ocr_done)

            def _clear_worker(w=worker):
                if self._ocr_worker is w:
                    self._ocr_worker = None
                w.deleteLater()

            worker.finished.connect(_clear_worker)
            self._ocr_worker = worker
            worker.start()
        except Exception:
            self._ocr_done_flag = True

    def _on_ocr_done(self, text: str) -> None:
        self._ocr_fallback_text = text.strip()
        self._ocr_done_flag = True

    def _upload_failed(self) -> None:
        self._awaiting_submit = False
        self._upload_failed_flag = True
        self._start_ocr_fallback()

    def _trigger_ocr_fallback_if_needed(self) -> bool:
        if self._fallback_triggered:
            return False
        if not self._ocr_started:
            self._start_ocr_fallback()
        if self._ocr_fallback_text:
            self._fallback_triggered = True
            prompt = self._active_full_query or config.DEFAULT_IMAGE_PROMPT
            self.status_changed.emit("Fallback: Searching...")
            self.submit(f"{prompt}\n\n{self._ocr_fallback_text}", cleaned_input=self._ocr_fallback_text)
            return True
        if self._ocr_done_flag:
            self._poll_timer.stop()
            self.output_view.setPlainText("Could not upload the image to Google AI Mode in Chrome.")
            self.status_changed.emit("Upload blocked")
            return True
        return False

    # ---- Answer polling (same state machine as the embedded backend) ----
    def _poll_stream(self) -> None:
        if time.monotonic() - self._start_ts > 55.0:
            self._poll_timer.stop()
            if self._last_clean_answer:
                self._home_state = "used"
                self.status_changed.emit("Done")
                self.answer_ready.emit(self._last_clean_answer)
                self._home_prewarm_timer.start()
            else:
                self.output_view.setPlainText(
                    "Timed out waiting for Google AI Mode.\n"
                    "If Chrome needs a sign-in or verification, use the tray 'Google Login / Verify...'."
                )
                self.status_changed.emit("Timed out")
            return
        if self._upload_failed_flag:
            self._trigger_ocr_fallback_if_needed()
            return
        if self._awaiting_submit or not self._cdp_ready:
            return
        self._eval(EXTRACT_AI_ANSWER_JS, self._on_dom_extracted)

    def _on_dom_extracted(self, res) -> None:
        if not isinstance(res, dict):
            return
        state = res.get("state", "")
        if state in ("idle", "submitting", "loading"):
            return
        if state == "upload_failed":
            self._upload_failed()
            return
        if state in BLOCKED_STATES:
            self._on_blocked(state)
            return

        cleaned = clean_extracted_ai_text(
            res.get("text", ""),
            full_query=self._active_full_query,
            cleaned_input=self._active_cleaned_input,
        )
        if not cleaned:
            return
        if cleaned != self._last_clean_answer:
            had_prev = bool(self._last_clean_answer)
            self._last_clean_answer = cleaned
            self._stable_ticks = 0
            _set_browser_text_preserving_scroll(self.output_view, cleaned, preserve=had_prev)
            self.status_changed.emit("Streaming...")
        elif self._load_done:
            if res.get("generating"):
                self._stable_ticks = 0
                return
            if res.get("doneSignals"):
                self._stable_ticks += 1
                if self._stable_ticks >= 2:
                    self._finish_answer(cleaned)
                return
            if self._inject_ts > 0.0 and (time.monotonic() - self._inject_ts) < 2.0:
                return
            self._stable_ticks += 1
            if self._stable_ticks >= 10:
                self._finish_answer(cleaned)

    def _finish_answer(self, cleaned: str) -> None:
        self._poll_timer.stop()
        self._home_state = "used"
        self.status_changed.emit("Done")
        self.answer_ready.emit(cleaned)
        self._home_prewarm_timer.start()

    def _on_blocked(self, state: str) -> None:
        self._poll_timer.stop()
        self._home_prewarm_timer.stop()
        self._home_state = "used"
        self._awaiting_submit = False
        self._upload_failed_flag = False
        # Never pop Chrome back up automatically after login; user can use Tray if needed.
        if state == "unavailable":
            self.output_view.setPlainText(
                "Google AI Mode is unavailable on this session.\n"
                "Right-click Tray icon -> 'Google Login / Verify...' if needed."
            )
            self.status_changed.emit("Blocked / unavailable")
            return
        self.output_view.setPlainText(
            "Google needs verification or sign-in.\n"
            "Right-click Tray icon -> 'Google Login / Verify...' if needed."
        )
        self.status_changed.emit("Verify in Tray")

    # ---- Cleanup -------------------------------------------------------
    def cleanup(self) -> None:
        self._poll_timer.stop()
        self._login_timer.stop()
        self._hide_timer.stop()
        self._home_prewarm_timer.stop()
        self._hide_active = False
        self._last_image = None
        self._last_images = []
        self._pending_upload_files = []
        self._drop_connection()
        if self._ocr_worker is not None and self._ocr_worker.isRunning():
            self._ocr_worker.wait(1000)
            self._ocr_worker = None
        proc = self._proc
        if proc is not None and proc.poll() is None:
            version = _http_json(config.CHROME_DEBUG_PORT, "/json/version")
            if version and version.get("webSocketDebuggerUrl"):
                tmp = CDPClient(version["webSocketDebuggerUrl"])
                tmp.connected.connect(lambda: tmp.send("Browser.close"))
                end = time.monotonic() + 4.0
                from PyQt6.QtWidgets import QApplication
                while proc.poll() is None and time.monotonic() < end:
                    QApplication.processEvents()
                    time.sleep(0.05)
                tmp.close()
            if proc.poll() is None:
                proc.terminate()
        self._proc = None

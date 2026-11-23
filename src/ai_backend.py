import os
import io
import re
import time
import json
import base64
import urllib.parse
import urllib.request
from abc import abstractmethod
from pathlib import Path
from typing import Optional, Sequence, Union
from PIL import Image

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextBrowser, QDialog, QPushButton,
    QLabel, QInputDialog, QMessageBox
)
from PyQt6.QtCore import Qt, QUrl, QTimer, QThread, pyqtSignal, QObject
from PyQt6.QtNetwork import QNetworkCookie
try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage, QWebEngineScript
except ImportError:
    QWebEngineView = None
    QWebEngineProfile = None
    QWebEnginePage = object
    QWebEngineScript = None

import src.config as config
from .config import (
    PROFILE_DIR, GOOGLE_AI_MODE_HOME, DEFAULT_IMAGE_PROMPT,
    LAST_SNIP_FILE, GEMINI_API_KEY_ENV, GEMINI_MODEL_ENV,
    DEFAULT_GEMINI_MODEL, LOGIN_AUTOCLOSE_MS
)

SESSION_COOKIE_NAMES = {"SID", "__Secure-1PSID"}

CHROME_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

FIREFOX_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) "
    "Gecko/20100101 Firefox/133.0"
)


def import_cookie_string(profile: QWebEngineProfile, raw_str: str) -> int:
    """
    Parses a raw cookie string (name=val; name2=val2 or JSON list) and injects
    it into the QWebEngineProfile's cookieStore for .google.com.
    """
    store = profile.cookieStore()
    count = 0
    raw_str = raw_str.strip()
    if raw_str.startswith("[") and raw_str.endswith("]"):
        try:
            items = json.loads(raw_str)
            for item in items:
                name = str(item.get("name", "")).encode("utf-8")
                val = str(item.get("value", "")).encode("utf-8")
                domain = str(item.get("domain", ".google.com"))
                path = str(item.get("path", "/"))
                if name and val:
                    c = QNetworkCookie(name, val)
                    c.setDomain(domain)
                    c.setPath(path)
                    if item.get("secure", True):
                        c.setSecure(True)
                    if item.get("httpOnly", False):
                        c.setHttpOnly(True)
                    store.setCookie(c, QUrl("https://www.google.com"))
                    count += 1
            return count
        except Exception:
            pass

    for part in raw_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k_b = k.strip().encode("utf-8")
        v_b = v.strip().encode("utf-8")
        if k_b:
            c = QNetworkCookie(k_b, v_b)
            c.setDomain(".google.com")
            c.setPath("/")
            c.setSecure(True)
            store.setCookie(c, QUrl("https://www.google.com"))
            count += 1
    return count

TRANSPARENT_TEXT_QSS = """
QTextBrowser {
    background: transparent;
    color: rgba(45, 50, 58, 235);
    border: none;
    font-family: 'Segoe UI', sans-serif;
    font-size: 11.5px;
    selection-background-color: rgba(0, 0, 0, 28);
    selection-color: rgba(20, 24, 30, 250);
}
QScrollBar:vertical {
    background: transparent;
    width: 4px;
    margin: 0px;
}
QScrollBar::handle:vertical {
    background: rgba(0, 0, 0, 34);
    min-height: 18px;
    border-radius: 2px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
"""

# Reads innerText from the LIVE connected DOM (temporarily hiding chrome/modals)
# so Blink preserves block line breaks AND excludes all display:none share/clipboard dialogs!
EXTRACT_AI_ANSWER_JS = r"""
(function() {
    const href = String(location.href || '');
    if (href.includes('/sorry/') || document.querySelector('#captcha-form, iframe[src*="recaptcha"]')) {
        return { state: 'captcha', text: '' };
    }
    if (href.includes('consent.google.com')) {
        return { state: 'consent', text: '' };
    }
    if (!document.body) {
        return { state: 'loading', text: '' };
    }

    const subState = window.__aiSubmissionState || '';
    if (subState === 'idle') {
        return { state: 'idle', text: '', url: href };
    }
    if (subState === 'upload_failed') {
        return { state: 'upload_failed', text: '', url: href };
    }
    if (subState === 'attaching' || subState === 'uploading') {
        return { state: 'submitting', text: '', url: href };
    }

    const bodyRaw = (document.body.innerText || '').trim();
    const bodyLower = bodyRaw.toLowerCase();
    if (
        bodyRaw.length < 500 &&
        (bodyLower.includes('sign in to continue') || bodyLower.includes('sign in to use ai mode'))
    ) {
        return { state: 'signin_required', text: '' };
    }
    if (bodyRaw.length < 1500 && bodyLower.includes('ai mode is not currently available')) {
        return { state: 'unavailable', text: '' };
    }

    const hasUrlQuery = /[?&]q=[^&]+/.test(href);
    if (!hasUrlQuery && subState !== 'submitted') {
        return { state: 'idle', text: '', url: href };
    }

    const IGNORE_SEL = [
        'header', 'nav', 'footer', 'script', 'style', 'noscript', 'svg',
        'button', 'input', 'textarea', 'form', 'dialog',
        '[role="navigation"]', '[role="banner"]', '[role="search"]', '[role="contentinfo"]',
        '[role="dialog"]', '[role="alert"]', '[role="status"]', '[role="menu"]',
        '#searchform', '#gb', '#top_nav', '#hdtb', '#botstuff', '[aria-hidden="true"]',
        '#rso', '#rcnt'
    ].join(',');

    function readLiveVisibleText(root) {
        if (!root) return '';
        const ignored = Array.from(root.querySelectorAll(IGNORE_SEL));
        const saved = ignored.map(el => [
            el.style.getPropertyValue('display'),
            el.style.getPropertyPriority('display')
        ]);
        try {
            for (const el of ignored) {
                el.style.setProperty('display', 'none', 'important');
            }
            return (root.innerText || '').trim();
        } finally {
            for (let i = 0; i < ignored.length; i++) {
                const [val, prio] = saved[i];
                if (val) {
                    ignored[i].style.setProperty('display', val, prio);
                } else {
                    ignored[i].style.removeProperty('display');
                }
            }
        }
    }

    const roots = [
        document.querySelector('[data-container-id="main-col"]'),
        document.querySelector('[data-subtree*="aim"]'),
        document.querySelector('[data-attrid*="ai"]'),
        document.querySelector('main'),
        document.querySelector('[role="main"]'),
        document.body
    ];

    let bestText = '';
    let bestRoot = null;
    for (const r of roots) {
        if (!r) continue;
        const txt = readLiveVisibleText(r);
        if (txt && txt.length >= 2) {
            bestText = txt;
            bestRoot = r;
            break;
        }
    }

    let generating = false;
    let doneSignals = false;
    try {
        for (const btn of document.querySelectorAll(
            'button[aria-label="Stop"], button[aria-label*="Stop generating" i], button[aria-label*="Stop responding" i], [role="button"][aria-label="Stop"]'
        )) {
            if (btn.disabled || btn.closest('[aria-hidden="true"], [inert]')) continue;
            const r = btn.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) {
                generating = true;
                break;
            }
        }
        if (!generating && bestRoot) {
            const doneRe = /^(good response|bad response|thumbs up|thumbs down|helpful|not helpful|copy|copy text|copy response|share)$/i;
            for (const btn of bestRoot.querySelectorAll('button[aria-label], [role="button"][aria-label]')) {
                if (btn.closest('nav, header, footer, form, [role="navigation"], [aria-hidden="true"], [inert]')) continue;
                const aria = (btn.getAttribute('aria-label') || '').trim();
                if (!doneRe.test(aria)) continue;
                const r = btn.getBoundingClientRect();
                const cs = getComputedStyle(btn);
                const vis = (r.width > 0 && r.height > 0) || (btn.offsetWidth > 0 && btn.offsetHeight > 0 && cs.visibility !== 'hidden' && cs.opacity !== '0');
                if (vis) {
                    doneSignals = true;
                    break;
                }
            }
        }
    } catch (e) {}

    return { state: 'ok', text: bestText, url: href, generating: generating, doneSignals: doneSignals };
})()
"""

NOISE_EXACT_LINES = {
    "accessibility links",
    "skip to main content",
    "skip to previous prompt",
    "skip to next prompt",
    "accessibility help",
    "accessibility feedback",
    "ai mode",
    "ai overview",
    "all",
    "images",
    "videos",
    "news",
    "maps",
    "books",
    "shopping",
    "finance",
    "search modes",
    "filters and topics",
    "sign in",
    "search results",
    "main results",
    "thinking",
    "thinking...",
    "searching...",
    "generating...",
    "copy",
    "copy code",
    "markdown",
    "share",
    "helpful",
    "not helpful",
    "show more",
    "learn more",
    "sites",
    "sources",
    "snip.png",
    "last_snip.png",
    "analyzing...",
    "analyzing image...",
    "looking at image...",
    "uploading...",
    "uploading image...",
    "meet ai mode",
    "ask anything",
    "ask anything...",
    "try asking",
    "what is on your mind",
    "what is on your mind?",
    "what's on your mind",
    "what's on your mind?",
    "what’s on your mind",
    "what’s on your mind?",
    "how can i help you today",
    "how can i help you today?",
    "what can i help with",
    "what can i help with?",
    "ask follow-up",
    "ask a follow-up",
    "use code with caution",
    "use code with caution.",
    "here are top web results for exploring this topic:",
    "here are top web results for exploring this topic",
}

PERSONALIZED_GREETING_RE = re.compile(
    r"^hi\s+[^,\n]{1,60},?\s*"
    r"(what('s|’s|\s+is)\s+on\s+your\s+mind|how\s+can\s+i\s+help(\s+you)?(\s+today)?|what\s+can\s+i\s+help\s+with)\??$",
    re.IGNORECASE,
)

# Substring/line cutoffs for Google modals, feedback toasts, and AI disclaimers
HARD_CUTOFF_SUBSTRINGS = (
    "copied to clipboard",
    "failed to copy to clipboard",
    "share public link",
    "this public link is valid",
    "can't copy the link right now",
    "can’t copy the link right now",
    "a copy of this chat",
    "thanks for letting us know",
    "google may use account and system data",
    "ai responses may include mistakes",
    "ai can make mistakes",
    "generative ai is experimental",
    "your feedback helps google improve",
    "thank you for your feedback",
    "public link created",
    "would you like to see ",
    "would you like to explore",
    "would you like me to ",
    "if you have more questions",
    "if you have any more questions",
    "if you would like to ",
    "if you would like,",
    "if you'd like to ",
    "if you'd like,",
    "if you’d like to ",
    "if you’d like,",
    "let me know if you",
    "let me know:",
    "here are top web results for exploring this topic",
)


def pil_to_png_bytes_and_b64(image: Image.Image) -> tuple[bytes, str]:
    """
    Encodes a PIL Image to PNG bytes in memory, writes it to LAST_SNIP_FILE for
    headless chooseFiles fallback, and returns (png_bytes, base64_str).
    """
    buf = io.BytesIO()
    rgb_img = image.convert("RGB") if image.mode not in ("RGB", "RGBA") else image
    rgb_img.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    try:
        config.LAST_SNIP_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.LAST_SNIP_FILE.write_bytes(png_bytes)
    except Exception:
        pass
    b64_str = base64.b64encode(png_bytes).decode("ascii")
    return png_bytes, b64_str


def save_upload_png(image: Image.Image, path=None, max_dim: Optional[int] = None) -> None:
    """
    Saves a crisp PNG for Chrome file upload without base64 overhead,
    downscaling only if the snip exceeds `max_dim` on either side.
    """
    target = path if path is not None else config.LAST_SNIP_FILE
    limit = max_dim if max_dim is not None else getattr(config, "MAX_UPLOAD_IMAGE_DIM", 1600)
    rgb_img = image.convert("RGB") if image.mode not in ("RGB", "RGBA") else image
    if limit and max(rgb_img.width, rgb_img.height) > limit:
        rgb_img = rgb_img.copy()
        rgb_img.thumbnail((limit, limit), Image.Resampling.LANCZOS)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        rgb_img.save(target, format="PNG", compress_level=3)
    except Exception:
        pass


def save_upload_pngs(
    images: Sequence[Image.Image],
    base_path=None,
    max_dim: Optional[int] = None,
) -> list[Path]:
    """
    Saves one or more snipped PIL images as PNGs for multi-file upload.
    A single image uses `base_path` (default `LAST_SNIP_FILE`) directly;
    multiple images write `<stem>_1.png` .. `<stem>_N.png` beside it.
    """
    target = Path(base_path) if base_path is not None else Path(config.LAST_SNIP_FILE)
    if not images:
        return []
    if len(images) == 1:
        save_upload_png(images[0], target, max_dim=max_dim)
        return [target]
    paths: list[Path] = []
    for idx, img in enumerate(images, start=1):
        p = target.with_name(f"{target.stem}_{idx}{target.suffix}")
        save_upload_png(img, p, max_dim=max_dim)
        paths.append(p)
    return paths


def build_attach_and_submit_image_js(b64_png: Union[str, Sequence[str]], prompt: str) -> str:
    """
    Builds JavaScript to attach one or more PNG images and submit the unified prompt inside
    Google AI Mode (udm=50) without ever opening an OS file dialog.
    """
    b64_list = [b64_png] if isinstance(b64_png, str) else list(b64_png)
    b64_json = json.dumps(b64_list)
    prompt_json = json.dumps(prompt)
    return f"""
    (function() {{
        const b64List = {b64_json};
        const promptText = {prompt_json};

        const dt = new DataTransfer();
        for (let idx = 0; idx < b64List.length; idx++) {{
            const binStr = atob(b64List[idx]);
            const bytes = new Uint8Array(binStr.length);
            for (let i = 0; i < binStr.length; i++) {{
                bytes[i] = binStr.charCodeAt(i);
            }}
            const blob = new Blob([bytes], {{ type: 'image/png' }});
            const fname = b64List.length === 1 ? 'snip.png' : ('snip_' + (idx + 1) + '.png');
            const file = new File([blob], fname, {{ type: 'image/png', lastModified: Date.now() }});
            dt.items.add(file);
        }}

        let attached = false;
        let method = 'none';

        function setBoxText(targetBox, text) {{
            if (!targetBox) return false;
            targetBox.focus();
            if ('value' in targetBox) {{
                const proto = targetBox.tagName === 'TEXTAREA'
                    ? window.HTMLTextAreaElement.prototype
                    : window.HTMLInputElement.prototype;
                const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (nativeSetter) {{
                    nativeSetter.call(targetBox, text);
                }} else {{
                    targetBox.value = text;
                }}
            }} else {{
                targetBox.textContent = text;
            }}
            targetBox.dispatchEvent(new Event('input', {{ bubbles: true }}));
            targetBox.dispatchEvent(new Event('change', {{ bubbles: true }}));
            return true;
        }}

        function tryAttachFileInput() {{
            const fileInput = document.querySelector('input[type="file"]');
            if (fileInput) {{
                try {{
                    fileInput.files = dt.files;
                    fileInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    fileInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    if (fileInput.files && fileInput.files.length > 0) {{
                        attached = true;
                        method = 'file_input';
                        return true;
                    }}
                }} catch (e) {{}}
            }}
            return false;
        }}

        try {{
            const origInputClick = HTMLInputElement.prototype.click;
            HTMLInputElement.prototype.click = function() {{
                if (this.type === 'file') {{
                    try {{
                        this.files = dt.files;
                        this.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        this.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        attached = true;
                        if (method === 'none') method = 'file_input_hook';
                    }} catch(e) {{}}
                    return;
                }}
                return origInputClick.apply(this, arguments);
            }};
        }} catch(e) {{}}

        if (!tryAttachFileInput()) {{
            const uploadTriggers = document.querySelectorAll(
                'button[aria-label*="image" i], button[aria-label*="upload" i], ' +
                'button[aria-label*="attach" i], button[aria-label*="lens" i], ' +
                'button[aria-label*="photo" i], [role="button"][aria-label*="image" i], ' +
                '[role="button"][aria-label*="upload" i]'
            );
            for (const btn of uploadTriggers) {{
                try {{
                    btn.click();
                    if (attached || tryAttachFileInput()) {{
                        if (method === 'none') method = 'upload_trigger';
                        break;
                    }}
                }} catch (e) {{}}
            }}
        }}

        const box = document.querySelector(
            'textarea, input[name="q"], [contenteditable="true"], [role="textbox"]'
        );

        if (!attached) {{
            const pasteTarget = box || document.body || document;
            try {{
                const pasteEvt = new ClipboardEvent('paste', {{
                    bubbles: true,
                    cancelable: true,
                    clipboardData: dt
                }});
                pasteTarget.dispatchEvent(pasteEvt);
                attached = true;
                method = 'clipboard_paste';
            }} catch (e) {{}}
        }}

        function triggerSubmit() {{
            const submitBtns = document.querySelectorAll(
                'button[type="submit"], button[aria-label*="Send" i], ' +
                'button[aria-label*="Submit" i], button[aria-label*="Search" i], ' +
                'button[aria-label*="Ask" i], [role="button"][aria-label*="Send" i]'
            );
            for (const btn of submitBtns) {{
                const isAriaDisabled = btn.getAttribute('aria-disabled') === 'true';
                if (!btn.disabled && !isAriaDisabled) {{
                    try {{ btn.click(); return 'button_click'; }} catch (e) {{}}
                }}
            }}
            if (box) {{
                const form = box.closest('form');
                if (form) {{
                    try {{
                        if (typeof form.requestSubmit === 'function') {{
                            form.requestSubmit();
                        }} else {{
                            form.submit();
                        }}
                        return 'form_submit';
                    }} catch (e) {{}}
                }}
                box.dispatchEvent(new KeyboardEvent('keydown', {{
                    key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true
                }}));
                return 'enter_key';
            }}
            return 'none';
        }}

        let promptSet = false;
        let submitResult = 'none';

        if (attached) {{
            window.__aiSubmissionState = 'uploading';
            if (box) {{
                promptSet = setBoxText(box, promptText);
            }}
            submitResult = triggerSubmit();
            if (submitResult !== 'none') {{
                window.__aiSubmissionState = 'submitted';
            }} else {{
                let attempts = 0;
                const timerId = setInterval(function() {{
                    attempts++;
                    const currBox = document.querySelector('textarea, input[name="q"], [contenteditable="true"], [role="textbox"]');
                    if (!promptSet && currBox) {{
                        promptSet = setBoxText(currBox, promptText);
                    }}
                    const res = triggerSubmit();
                    if (res !== 'none') {{
                        clearInterval(timerId);
                        window.__aiSubmissionState = 'submitted';
                    }} else if (attempts >= 12) {{
                        clearInterval(timerId);
                        window.__aiSubmissionState = 'upload_failed';
                    }}
                }}, 250);
            }}
        }} else {{
            window.__aiSubmissionState = 'upload_failed';
        }}

        return {{
            attached: attached,
            method: method,
            promptSet: promptSet,
            submitted: submitResult !== 'none',
            submitMethod: submitResult
        }};
    }})()
    """


def clean_extracted_ai_text(
    raw_text: str,
    full_query: str = "",
    cleaned_input: str = "",
) -> str:
    """
    Strips echoed prompt text, Google Search nav chrome, and hidden clipboard/share
    modals while preserving 100% of Google AI's explanation, paragraphs, and context.
    """
    if not raw_text:
        return ""

    text = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()

    # Remove screen-reader arrow descriptions like "←left arrow"
    text = re.sub(r"←\s*left arrow\u2060?", "←", text)

    # Cut off inline modal/clipboard/disclaimer tails even if glued onto the end of a sentence
    lower_full = text.lower()
    earliest_cut = None
    for marker in HARD_CUTOFF_SUBSTRINGS:
        pos = lower_full.find(marker)
        if pos != -1 and (earliest_cut is None or pos < earliest_cut):
            earliest_cut = pos
    if earliest_cut is not None:
        text = text[:earliest_cut].strip()

    # Remove echoed full_query or cleaned_input if Google echoed the user prompt turn at the top
    for candidate in (full_query.strip(), cleaned_input.strip(), DEFAULT_IMAGE_PROMPT):
        if candidate and len(candidate) >= 6:
            idx = text.find(candidate)
            if 0 <= idx <= 200:
                text = text[idx + len(candidate):].strip()
                break
            first_line = candidate.split("\n", 1)[0].strip()
            if len(first_line) >= 15 and text.startswith(first_line):
                for q_line in candidate.split("\n"):
                    q_s = q_line.strip()
                    if q_s and text.startswith(q_s):
                        text = text[len(q_s):].lstrip()

    prompt_lower = DEFAULT_IMAGE_PROMPT.lower()
    query_lower = full_query.strip().lower()

    out_lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        lower = stripped.lower()

        if not stripped:
            if out_lines and out_lines[-1] != "":
                out_lines.append("")
            continue

        if (
            lower in NOISE_EXACT_LINES
            or lower.startswith("use code with caution")
            or lower == prompt_lower
            or (query_lower and lower == query_lower)
            or PERSONALIZED_GREETING_RE.match(stripped)
        ):
            continue

        if re.match(r"^(\+\d+|\d+\s+sites?|(last_)?snip(_\d+)?\.png)$", lower):
            continue

        if lower == "answer:":
            prev_non_empty = next((l.strip().lower() for l in reversed(out_lines) if l), "")
            if prev_non_empty == "answer:":
                continue

        out_lines.append(line.rstrip())

    while out_lines and out_lines[-1] == "":
        out_lines.pop()

    return _promote_answer_section("\n".join(out_lines).strip())


def _promote_answer_section(text: str) -> str:
    """Move a trailing top-level Answer section above Explanation so it stays visible first."""
    if not text:
        return ""
    lines = text.split("\n")
    if re.match(r"^answer\s*:", lines[0].strip().lower()):
        return text

    exp_idx = None
    ans_idx = None
    for idx, line in enumerate(lines):
        if line != line.lstrip():
            continue
        low = line.strip().lower()
        if exp_idx is None and re.match(r"^explanation\s*:", low):
            exp_idx = idx
        elif re.match(r"^answer\s*:", low):
            ans_idx = idx

    if ans_idx is None or ans_idx == 0:
        return text
    if exp_idx is not None and exp_idx > ans_idx:
        return text
    if exp_idx is None and (len(lines) - ans_idx) > 8:
        return text

    answer_block = "\n".join(lines[ans_idx:]).strip()
    rest_block = "\n".join(lines[:ans_idx]).strip()
    return f"{answer_block}\n\n{rest_block}" if rest_block else answer_block


def _create_transparent_browser(parent: Optional[QWidget] = None) -> QTextBrowser:
    view = QTextBrowser(parent)
    view.setOpenExternalLinks(False)
    view.setOpenLinks(False)
    view.setFrameShape(QTextBrowser.Shape.NoFrame)
    view.setStyleSheet(TRANSPARENT_TEXT_QSS)
    view.viewport().setAutoFillBackground(False)
    return view


def _set_browser_text_preserving_scroll(view: QTextBrowser, text: str, preserve: bool = False) -> None:
    """Updates QTextBrowser text while keeping current scroll offset during streaming."""
    vbar = view.verticalScrollBar()
    saved = vbar.value() if (preserve and vbar is not None) else 0
    view.setPlainText(text)
    if vbar is not None:
        if preserve and saved > 0:
            vbar.setValue(min(saved, vbar.maximum()))
        else:
            vbar.setValue(0)


class AIBackend(QObject):
    status_changed = pyqtSignal(str)
    answer_ready = pyqtSignal(str)
    login_finished = pyqtSignal()
    requires_login = False

    def __init__(self, parent=None):
        super().__init__(parent)

    @abstractmethod
    def widget(self) -> QWidget:
        pass

    @abstractmethod
    def prewarm(self) -> None:
        pass

    @abstractmethod
    def submit_image(self, image: Image.Image, prompt: str = DEFAULT_IMAGE_PROMPT) -> None:
        pass

    def submit_images(self, images: Sequence[Image.Image], prompt: str = DEFAULT_IMAGE_PROMPT) -> None:
        if images:
            self.submit_image(images[0], prompt)

    def submit(self, full_query: str, cleaned_input: str = "") -> None:
        pass

    def display_cached(self, text: str) -> None:
        pass

    def open_login_helper(self, modal: bool = True) -> None:
        pass

    def is_logged_in(self) -> bool:
        return True

    def cleanup(self) -> None:
        pass


class _GeminiRequestThread(QThread):
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        prompt: str,
        api_key: str,
        b64_png: Optional[str] = None,
        b64_pngs: Optional[Sequence[str]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.prompt = prompt
        self.api_key = api_key
        if b64_pngs is not None:
            self.b64_pngs = list(b64_pngs)
        elif b64_png:
            self.b64_pngs = [b64_png]
        else:
            self.b64_pngs = []
        self.b64_png = self.b64_pngs[0] if self.b64_pngs else None

    def run(self):
        model = os.environ.get(GEMINI_MODEL_ENV, DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={self.api_key}"
        )
        parts = []
        for b64 in self.b64_pngs:
            parts.append({
                "inline_data": {
                    "mime_type": "image/png",
                    "data": b64,
                }
            })
        parts.append({"text": self.prompt})
        payload = {"contents": [{"parts": parts}]}
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                candidates = data.get("candidates", [])
                if candidates:
                    res_parts = candidates[0].get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in res_parts).strip()
                    if text:
                        self.succeeded.emit(text)
                        return
                self.failed.emit("No answer generated.")
        except Exception as e:
            self.failed.emit(f"API error: {e}")


class GeminiAPIBackend(AIBackend):
    """Optional API backend when GEMINI_API_KEY is explicitly configured."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(self._container)
        layout.setContentsMargins(2, 0, 2, 0)

        self.output_view = _create_transparent_browser(self._container)
        layout.addWidget(self.output_view)
        self._worker: Optional[_GeminiRequestThread] = None
        self._active_full_query = ""

    def widget(self) -> QWidget:
        return self._container

    def prewarm(self) -> None:
        pass

    def display_cached(self, text: str) -> None:
        self.output_view.setPlainText(text)

    def _disconnect_worker(self):
        if self._worker is not None:
            try:
                self._worker.succeeded.disconnect(self._on_ok)
                self._worker.failed.disconnect(self._on_err)
            except Exception:
                pass
            if self._worker.isRunning():
                self._worker.wait(2000)
            self._worker.deleteLater()
            self._worker = None

    def cleanup(self) -> None:
        self._disconnect_worker()

    def submit_images(self, images: Sequence[Image.Image], prompt: str = DEFAULT_IMAGE_PROMPT) -> None:
        if not images:
            return
        self._active_full_query = prompt
        api_key = os.environ.get(GEMINI_API_KEY_ENV, "").strip()
        if not api_key:
            self.output_view.setPlainText("GEMINI_API_KEY is not set.")
            self.status_changed.emit("Missing API key")
            return

        self._disconnect_worker()
        b64_pngs = [pil_to_png_bytes_and_b64(img)[1] for img in images]
        status = "Sending image..." if len(images) == 1 else f"Sending {len(images)} images..."
        self.status_changed.emit(status)
        self.output_view.setPlainText(status)
        self._worker = _GeminiRequestThread(prompt, api_key, b64_pngs=b64_pngs, parent=self)
        self._worker.succeeded.connect(self._on_ok)
        self._worker.failed.connect(self._on_err)
        self._worker.start()

    def submit_image(self, image: Image.Image, prompt: str = DEFAULT_IMAGE_PROMPT) -> None:
        self.submit_images([image], prompt)

    def submit(self, full_query: str, cleaned_input: str = "") -> None:
        self._active_full_query = full_query
        api_key = os.environ.get(GEMINI_API_KEY_ENV, "").strip()
        if not api_key:
            self.output_view.setPlainText("GEMINI_API_KEY is not set.")
            self.status_changed.emit("Missing API key")
            return

        self._disconnect_worker()
        self.status_changed.emit("Thinking...")
        self.output_view.setPlainText("...")
        self._worker = _GeminiRequestThread(full_query, api_key, b64_png=None, parent=self)
        self._worker.succeeded.connect(self._on_ok)
        self._worker.failed.connect(self._on_err)
        self._worker.start()

    def _on_ok(self, text: str):
        cleaned = clean_extracted_ai_text(text, full_query=self._active_full_query)
        final_text = cleaned or text.strip()
        self.output_view.setPlainText(final_text)
        self.status_changed.emit("Done")
        self.answer_ready.emit(final_text)

    def _on_err(self, err: str):
        self.output_view.setPlainText(err)
        self.status_changed.emit("Error")


_SHARED_WEB_PROFILE: Optional[QWebEngineProfile] = None


def _get_shared_web_profile() -> QWebEngineProfile:
    global _SHARED_WEB_PROFILE
    if _SHARED_WEB_PROFILE is None:
        from PyQt6.QtWidgets import QApplication
        cache_dir = PROFILE_DIR / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        prof = QWebEngineProfile("web_profile", QApplication.instance())
        prof.setPersistentStoragePath(str(PROFILE_DIR))
        prof.setCachePath(str(cache_dir))
        prof.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)
        prof.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)

        raw_ua = prof.httpUserAgent()
        clean_ua = re.sub(r"QtWebEngine/\S+\s*", "", raw_ua).strip()
        prof.setHttpUserAgent(clean_ua or CHROME_DESKTOP_UA)

        vis_script = QWebEngineScript()
        vis_script.setName("force_visible_state")
        vis_script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        vis_script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        vis_script.setRunsOnSubFrames(True)
        vis_script.setSourceCode(
            "try { Object.defineProperty(document, 'visibilityState', {get: () => 'visible'}); } catch(e){}"
            "try { Object.defineProperty(document, 'hidden', {get: () => false}); } catch(e){}"
        )
        prof.scripts().insert(vis_script)
        _SHARED_WEB_PROFILE = prof
    return _SHARED_WEB_PROFILE


class _AIModeWebPage(QWebEnginePage):
    """
    Headless QWebEnginePage that intercepts file chooser requests and silently
    returns queued snip PNG paths so an OS file dialog never opens.
    """
    def __init__(self, profile, parent=None):
        super().__init__(profile, parent)
        self._upload_files: Optional[list[str]] = None

    def chooseFiles(self, mode, oldFiles, acceptedMimeTypes):
        if self._upload_files:
            return list(self._upload_files)
        return [str(config.LAST_SNIP_FILE)]


class _OCRWorkerThread(QThread):
    ocr_done = pyqtSignal(str)

    def __init__(self, image: Union[Image.Image, Sequence[Image.Image]], parent=None):
        super().__init__(parent)
        self.images = list(image) if isinstance(image, (list, tuple)) else [image]
        self.image = self.images[0] if self.images else None

    def run(self):
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)
            try:
                import winocr
                from PIL import ImageOps
                chunks = []
                for img in self.images:
                    gray = img.convert("L")
                    lo, hi = gray.getextrema()
                    if (lo + hi) / 2 < 128:
                        gray = ImageOps.invert(gray)
                    gray = ImageOps.autocontrast(gray)
                    if gray.width < 800 or gray.height < 400:
                        scale = 2.0
                        gray = gray.resize((int(gray.width * scale), int(gray.height * scale)), Image.Resampling.LANCZOS)
                    res = winocr.recognize_pil_sync(gray, "en-US")
                    lines = [line.get("text", "") for line in res.get("lines", [])]
                    block = "\n".join(l for l in lines if l.strip()).strip()
                    if block:
                        chunks.append(block)
                self.ocr_done.emit("\n\n".join(chunks).strip())
            finally:
                ctypes.windll.ole32.CoUninitialize()
        except Exception:
            self.ocr_done.emit("")


class GoogleAIModeWebBackend(AIBackend):
    """
    Free in-card Google Search AI Mode (udm=50) backend.
    Drives an invisible _AIModeWebPage in the background with a desktop Chrome UA,
    attaches the snipped image + unified prompt, and streams full-context answer
    text into the blurred PopupPanel. Never opens an external browser.
    """

    requires_login = True

    def __init__(self, parent=None):
        super().__init__(parent)
        if QWebEngineView is None or QWebEngineProfile is None:
            raise RuntimeError(
                "PyQt6-WebEngine is required for the embedded web backend. "
                "Install it with: python -m pip install PyQt6-WebEngine"
            )
        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(self._container)
        layout.setContentsMargins(2, 0, 2, 0)

        self.output_view = _create_transparent_browser(self._container)
        layout.addWidget(self.output_view)

        self.profile = _get_shared_web_profile()
        self.page = _AIModeWebPage(self.profile, self._container)
        self.page.setLifecycleState(QWebEnginePage.LifecycleState.Active)
        self.page.setVisible(True)

        self._session_cookies: set[str] = set()
        self._login_dlg: Optional[QDialog] = None
        store = self.profile.cookieStore()
        store.cookieAdded.connect(self._on_cookie_added)
        store.cookieRemoved.connect(self._on_cookie_removed)
        store.loadAllCookies()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(250)
        self._poll_timer.timeout.connect(self._poll_stream)

        self._active_full_query = ""
        self._active_cleaned_input = ""
        self._pending_image_b64: Optional[str] = None
        self._pending_image_prompt: Optional[str] = None
        self._awaiting_image_inject: bool = False
        self._inject_ts: float = 0.0
        self._last_clean_answer = ""
        self._stable_ticks = 0
        self._load_done = False
        self._start_ts = 0.0
        self._is_prewarming = False

        self._ocr_worker: Optional[_OCRWorkerThread] = None
        self._ocr_fallback_text: str = ""
        self._ocr_done_flag: bool = False
        self._fallback_triggered: bool = False

        self.page.loadFinished.connect(self._on_load_finished)

    def widget(self) -> QWidget:
        return self._container

    def display_cached(self, text: str) -> None:
        self._poll_timer.stop()
        self.output_view.setPlainText(text)

    def prewarm(self) -> None:
        self._is_prewarming = True
        self.page.setUrl(QUrl(GOOGLE_AI_MODE_HOME))

    def submit_images(self, images: Sequence[Image.Image], prompt: str = DEFAULT_IMAGE_PROMPT) -> None:
        if not images:
            return
        self._is_prewarming = False
        self._poll_timer.stop()
        self.page.triggerAction(QWebEnginePage.WebAction.Stop)

        self._ocr_fallback_text = ""
        self._ocr_done_flag = False
        self._fallback_triggered = False
        try:
            if self._ocr_worker is not None and self._ocr_worker.isRunning():
                self._ocr_worker.wait(500)
            ocr_target = images[0] if len(images) == 1 else list(images)
            self._ocr_worker = _OCRWorkerThread(ocr_target, parent=self)
            self._ocr_worker.ocr_done.connect(self._on_ocr_done)
            self._ocr_worker.start()
        except Exception:
            self._ocr_done_flag = True

        saved_paths = save_upload_pngs(images, config.LAST_SNIP_FILE)
        self.page._upload_files = [str(p) for p in saved_paths]
        b64_pngs = [pil_to_png_bytes_and_b64(img)[1] for img in images]
        self._pending_image_b64 = b64_pngs[0] if len(b64_pngs) == 1 else b64_pngs
        self._pending_image_prompt = prompt
        self._awaiting_image_inject = True
        self._inject_ts = 0.0
        self._active_full_query = prompt
        self._active_cleaned_input = ""
        self._last_clean_answer = ""
        self._stable_ticks = 0
        self._load_done = False
        self._start_ts = time.monotonic()

        status = "Sending image..." if len(images) == 1 else f"Sending {len(images)} images..."
        self.output_view.setPlainText(status)
        self.status_changed.emit(status)

        self.page.setUrl(QUrl(GOOGLE_AI_MODE_HOME))
        self._poll_timer.start()

    def submit_image(self, image: Image.Image, prompt: str = DEFAULT_IMAGE_PROMPT) -> None:
        self.submit_images([image], prompt)

    def _on_ocr_done(self, text: str):
        self._ocr_fallback_text = text.strip()
        self._ocr_done_flag = True

    def _trigger_ocr_fallback_if_needed(self) -> bool:
        if self._fallback_triggered:
            return False
        if self._ocr_fallback_text:
            self._fallback_triggered = True
            prompt = self._active_full_query or DEFAULT_IMAGE_PROMPT
            full_q = f"{prompt}\n\n{self._ocr_fallback_text}"
            self.status_changed.emit("Fallback: Searching...")
            self.submit(full_q, cleaned_input=self._ocr_fallback_text)
            return True
        elif self._ocr_done_flag and not self._ocr_fallback_text:
            self._poll_timer.stop()
            self.output_view.setPlainText(
                "Could not upload image directly to Google AI Mode.\n"
                "Right-click Tray icon -> 'Google Login / Verify...' once to enable image upload."
            )
            self.status_changed.emit("Upload blocked")
            return True
        return False

    def submit(self, full_query: str, cleaned_input: str = "") -> None:
        self._is_prewarming = False
        self._poll_timer.stop()
        self.page.triggerAction(QWebEnginePage.WebAction.Stop)

        self._pending_image_b64 = None
        self._pending_image_prompt = None
        self._awaiting_image_inject = False
        self._inject_ts = 0.0
        self._active_full_query = full_query
        self._active_cleaned_input = cleaned_input
        self._last_clean_answer = ""
        self._stable_ticks = 0
        self._load_done = False
        self._start_ts = time.monotonic()

        self.output_view.setPlainText("Thinking...")
        self.status_changed.emit("Thinking...")

        encoded = urllib.parse.quote_plus(full_query)
        target_url = f"{GOOGLE_AI_MODE_HOME}&q={encoded}"
        self.page.setUrl(QUrl(target_url))
        self._poll_timer.start()

    def _on_load_finished(self, ok: bool):
        if self._is_prewarming:
            self._is_prewarming = False
            return

        self._load_done = True
        if not ok and not self._last_clean_answer:
            self._awaiting_image_inject = False
            self._poll_timer.stop()
            self.output_view.setPlainText("Network error loading Google AI Mode.")
            self.status_changed.emit("Offline / failed")
            return

        if self._pending_image_b64 is not None:
            b64_png = self._pending_image_b64
            prompt = self._pending_image_prompt or DEFAULT_IMAGE_PROMPT
            self._pending_image_b64 = None
            self._pending_image_prompt = None
            self._awaiting_image_inject = False
            self._inject_ts = time.monotonic()
            inject_js = build_attach_and_submit_image_js(b64_png, prompt)
            self.status_changed.emit("Thinking...")
            self.page.runJavaScript(inject_js)

    def _poll_stream(self):
        elapsed = time.monotonic() - self._start_ts
        if elapsed > 45.0:
            self._poll_timer.stop()
            if self._last_clean_answer:
                self.status_changed.emit("Done")
                self.answer_ready.emit(self._last_clean_answer)
            else:
                self.output_view.setPlainText(
                    "Timed out waiting for Google AI Mode.\n"
                    "If Google needs one-time sign-in or consent, right-click the Tray icon -> 'Google Login / Verify...'."
                )
                self.status_changed.emit("Timed out")
            return

        if self._awaiting_image_inject:
            return

        self.page.runJavaScript(EXTRACT_AI_ANSWER_JS, self._on_dom_extracted)

    def _on_dom_extracted(self, res):
        if not isinstance(res, dict):
            return

        state = res.get("state", "")
        if state in ("idle", "submitting", "loading"):
            return

        if state == "upload_failed":
            if self._trigger_ocr_fallback_if_needed():
                return
            return

        if state == "captcha":
            self._poll_timer.stop()
            self.output_view.setPlainText(
                "Google requested human verification.\n"
                "Right-click Tray icon -> 'Google Login / Verify...' once to clear it."
            )
            self.status_changed.emit("Verify in Tray")
            return

        if state in ("consent", "signin_required", "unavailable"):
            self._poll_timer.stop()
            self.output_view.setPlainText(
                "Google requires one-time cookie consent or sign-in.\n"
                "Right-click Tray icon -> 'Google Login / Verify...' once to unlock."
            )
            self.status_changed.emit("Setup in Tray")
            return

        raw_text = res.get("text", "")
        cleaned = clean_extracted_ai_text(
            raw_text,
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
        elif self._load_done and len(cleaned) >= 1:
            if self._inject_ts > 0.0 and (time.monotonic() - self._inject_ts) < 2.0:
                return
            self._stable_ticks += 1
            if self._stable_ticks >= 6:
                self._poll_timer.stop()
                self.status_changed.emit("Done")
                self.answer_ready.emit(cleaned)

    def _on_cookie_added(self, cookie: QNetworkCookie) -> None:
        name = bytes(cookie.name()).decode("utf-8", "ignore")
        if name in SESSION_COOKIE_NAMES and cookie.domain().lstrip(".").endswith("google.com"):
            self._session_cookies.add(name)

    def _on_cookie_removed(self, cookie: QNetworkCookie) -> None:
        if cookie.domain().lstrip(".").endswith("google.com"):
            self._session_cookies.discard(bytes(cookie.name()).decode("utf-8", "ignore"))

    def is_logged_in(self) -> bool:
        return bool(self._session_cookies)

    def open_login_helper(self, modal: bool = True) -> None:
        if self._login_dlg is not None:
            self._login_dlg.show()
            self._login_dlg.raise_()
            self._login_dlg.activateWindow()
            return
        dlg = QDialog()
        dlg.setWindowTitle("Google AI Mode — Login & Verification Helper")
        dlg.resize(580, 700)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        banner = QLabel()
        layout.addWidget(banner)
        closing = [False]

        def refresh_banner():
            if self.is_logged_in():
                banner.setText("✓ Logged in — closing...")
                banner.setStyleSheet("background:#1e7e34; color:white; padding:6px; font-weight:bold;")
                if not closing[0]:
                    closing[0] = True
                    QTimer.singleShot(LOGIN_AUTOCLOSE_MS, dlg.accept)
            else:
                banner.setText("Not logged in — please sign in to Google below")
                banner.setStyleSheet("background:#b8860b; color:white; padding:6px; font-weight:bold;")

        refresh_banner()
        banner_timer = QTimer(dlg)
        banner_timer.setInterval(500)
        banner_timer.timeout.connect(refresh_banner)
        banner_timer.start()

        # Top toolbar
        top_bar = QHBoxLayout()
        btn_yt = QPushButton("Safe Sign In (YouTube Route)")
        btn_yt.setToolTip("Sign in via YouTube endpoint. Sets full .google.com account session while bypassing embedded WebView security blocks.")
        btn_google = QPushButton("Direct Google Sign In")
        btn_ua = QPushButton("Switch to Firefox UA")
        btn_aimode = QPushButton("Test AI Mode")
        btn_cookie = QPushButton("Paste Cookie")

        top_bar.addWidget(btn_yt)
        top_bar.addWidget(btn_google)
        top_bar.addWidget(btn_ua)
        top_bar.addWidget(btn_aimode)
        top_bar.addWidget(btn_cookie)
        layout.addLayout(top_bar)

        helper_page = QWebEnginePage(self.profile, dlg)
        helper_page.createWindow = lambda win_type: helper_page

        helper_view = QWebEngineView(dlg)
        helper_view.setPage(helper_page)

        # Toolbar wiring
        btn_yt.clicked.connect(lambda: helper_view.setUrl(
            QUrl("https://accounts.google.com/ServiceLogin?service=youtube&hl=en&continue=https://www.google.com/search%3Fudm%3D50%26hl%3Den")
        ))
        btn_google.clicked.connect(lambda: helper_view.setUrl(
            QUrl("https://accounts.google.com/ServiceLogin?hl=en&continue=https://www.google.com/search%3Fudm%3D50%26hl%3Den")
        ))
        btn_aimode.clicked.connect(lambda: helper_view.setUrl(
            QUrl(GOOGLE_AI_MODE_HOME)
        ))

        is_firefox_ua = [False]

        def toggle_ua():
            is_firefox_ua[0] = not is_firefox_ua[0]
            if is_firefox_ua[0]:
                self.profile.setHttpUserAgent(FIREFOX_DESKTOP_UA)
                btn_ua.setText("Switch to Chrome UA")
            else:
                self.profile.setHttpUserAgent(CHROME_DESKTOP_UA)
                btn_ua.setText("Switch to Firefox UA")
            helper_view.reload()

        btn_ua.clicked.connect(toggle_ua)

        def on_paste_cookie():
            text, ok = QInputDialog.getMultiLineText(
                dlg,
                "Import Google Cookies",
                "Paste your Google cookies string (e.g. from Chrome DevTools / Cookie Editor):\n(Format: SID=...; HSID=...; SSID=...; or JSON array)",
            )
            if ok and text.strip():
                imported = import_cookie_string(self.profile, text.strip())
                QMessageBox.information(
                    dlg,
                    "Cookies Imported",
                    f"Successfully imported {imported} cookies.\nReloading Google AI Mode...",
                )
                helper_view.setUrl(QUrl(GOOGLE_AI_MODE_HOME))

        btn_cookie.clicked.connect(on_paste_cookie)

        # Default start on YouTube safe route which avoids 'This browser or app may not be secure'
        helper_view.setUrl(QUrl("https://accounts.google.com/ServiceLogin?service=youtube&hl=en&continue=https://www.google.com/search%3Fudm%3D50%26hl%3Den"))

        layout.addWidget(helper_view, 1)

        bottom_bar = QHBoxLayout()
        tip = QLabel("Tip: Click 'Safe Sign In' to log in without security blocks, or 'Test AI Mode' when done.")
        tip.setStyleSheet("color: #777; font-size: 11px;")
        done_btn = QPushButton("Done (Save & Close)")
        done_btn.clicked.connect(dlg.accept)
        bottom_bar.addWidget(tip, 1)
        bottom_bar.addWidget(done_btn)
        layout.addLayout(bottom_bar)

        if modal:
            dlg.exec()
            return

        self._login_dlg = dlg

        def on_finished(_result):
            self._login_dlg = None
            dlg.deleteLater()
            self.login_finished.emit()

        dlg.finished.connect(on_finished)
        dlg.show()

    def cleanup(self) -> None:
        self._poll_timer.stop()
        if self._ocr_worker is not None and self._ocr_worker.isRunning():
            self._ocr_worker.wait(1000)
            self._ocr_worker = None
        if self.page is not None:
            from PyQt6 import sip
            if not sip.isdeleted(self.page):
                sip.delete(self.page)
            self.page = None


def create_backend(backend_type: Optional[str] = None) -> AIBackend:
    if backend_type == "gemini":
        return GeminiAPIBackend()
    if backend_type == "web":
        return GoogleAIModeWebBackend()
    if backend_type != "chrome" and os.environ.get(GEMINI_API_KEY_ENV, "").strip():
        return GeminiAPIBackend()
    from .chrome_backend import ChromeBackend
    return ChromeBackend()

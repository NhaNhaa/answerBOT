import os
from pathlib import Path

# Persistent storage outside repository (with fallback if sandboxed)
APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "QuickReadAIOverlay"
PROFILE_DIR = APP_DATA_DIR / "web_profile"
CHROME_PROFILE_DIR = APP_DATA_DIR / "chrome_profile"
STATE_FILE = APP_DATA_DIR / "state.json"
LAST_SNIP_FILE = APP_DATA_DIR / "last_snip.png"

try:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
except (PermissionError, OSError):
    APP_DATA_DIR = Path(os.getcwd()) / ".appdata"
    PROFILE_DIR = APP_DATA_DIR / "web_profile"
    CHROME_PROFILE_DIR = APP_DATA_DIR / "chrome_profile"
    STATE_FILE = APP_DATA_DIR / "state.json"
    LAST_SNIP_FILE = APP_DATA_DIR / "last_snip.png"
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

GOOGLE_AI_MODE_HOME = "https://www.google.com/search?udm=50&hl=en"
DEFAULT_IMAGE_PROMPT = (
    "No bold or # headings outside fenced code blocks. "
    "First write 'Explanation:' with brief bullets or complete indented code. "
    "Last write 'Answer:' with only the choice letter, exact fill text for each [n] blank (omit outside @ or **), "
    "or for written Q&A put the full ready-to-type Markdown and $math$ answer inside one fenced code block."
)
DEFAULT_CARD_W = 340
DEFAULT_CARD_H = 260
DRAG_THRESHOLD_PX = 5       # Logical px for bubble drag vs click
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
CHROME_DEBUG_PORT = 9223
CHROME_HEADLESS = False      # real Chrome avoids Google AI Mode headless blocks
CHROME_HIDE_WINDOW = True    # hide background Chrome window/taskbar icon via SW_HIDE
LOGIN_AUTOCLOSE_MS = 1500   # close login window this long after a session is detected
MAX_UPLOAD_IMAGE_DIM = 1600  # cap longest edge (px) before uploading HiDPI snips
DOT_SPARE_LIVES = 2          # spare dot clicks before next click starts a new snip
MAX_SNIP_IMAGES = 5          # max queued snips per question before auto-Done


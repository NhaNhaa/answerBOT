# QuickRead AI Overlay

A lightweight, stealth floating screen-snip assistant for Windows powered by Google AI Mode via a hidden Chrome session.

## Requirements

- **Windows 10 / 11**
- **Python 3.10+**
- **Git** *(or download the repository as a ZIP)*
- **Google Chrome** *(or Microsoft Edge)*

## Quick Start

```powershell
git clone https://github.com/NhaNhaa/answerBOT.git
cd answerBOT
python -m pip install -r requirements.txt
python main.py
```

*(Optional: create and activate a virtual environment first with `python -m venv .venv` and `.\.venv\Scripts\Activate.ps1`.)*

### Startup & Google Sign-In

1. The floating dot and system tray icon appear immediately on launch (clicking the dot while sign-in is pending brings Chrome to the front).
2. A Chrome window opens to Google Sign-In. Sign in once and wait ~1.5 seconds — Chrome closes automatically and restarts silently hidden in the background.
3. Your Google session is saved locally in `%LOCALAPPDATA%\QuickReadAIOverlay\chrome_profile`. On later launches, Chrome briefly opens to verify your saved session, auto-closes after ~1.5 seconds, and switches to hidden background mode without asking for your password again.

## Controls

| Control | Action |
|---|---|
| **Dot (`●`)** | Left-click to snip (`1–5` images). Drag to move (snaps to screen edges). |
| **Cancel Snip** | Quick left-click tap (`<5 px`) without dragging cancels the current snip without losing already-queued snips. |
| **`Done N/5` pill** *(below dot)* | Appears after Snip #1 (`1–4` snips). Move/scroll freely and click the dot to add more snips, click `Done N/5` to submit, or finish a 5th snip to auto-submit. |
| **Answer Box** | Translucent light-white & **100% click-through** (clicks pass straight to apps underneath). Hover over long answers and use your **scroll wheel or two-finger touchpad** to scroll. |
| **Lives (`2/2`)** *(top-left)* | Outside left-click hides the box for free. Clicking the dot toggles the same answer up to 2 times (`2/2 → 1/2 → 0/2`); the next dot click clears it and starts a new snip. |
| **Star (`☆` / `★`)** *(above dot)* | Shown while the answer box is open. Turn ON (`★`, shows `∞`) to keep the same answer across unlimited close/re-open cycles without spending lives. |
| **Pin (`📌`)** *(below dot)* | Shown while the answer box is open. Turn ON (filled) to keep the box open when clicking outside; clicking the dot still hides/re-opens it. |
| **System Tray Icon** | Right-click the tray icon to **Show / Locate Dot**, re-open **Google Login / Verify...**, or **Quit** cleanly. |

## Optional: Gemini API Mode

To use the official Gemini API instead of Chrome, set `GEMINI_API_KEY` before launching (defaults to `gemini-3.5-flash`; override with `GEMINI_MODEL` if needed):

```powershell
$env:GEMINI_API_KEY = "your-api-key"
python main.py
```

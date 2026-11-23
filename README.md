# QuickRead AI Overlay

A lightweight, stealth floating screen-snip assistant for Windows powered by Google AI Mode via a hidden Chrome session.

## Requirements

- **Windows 10 / 11**
- **Python 3.10+**
- **Google Chrome** (or Microsoft Edge)

## Quick Start

Install dependencies and launch:

```powershell
pip install -r requirements.txt
python main.py
```

**First run (automatic):**
1. A Chrome window opens automatically to Google Sign-In.
2. Sign in to your Google account once and wait ~1.5 seconds.
3. The login window closes on its own, Chrome restarts silently in the background, and the floating dot appears on screen — ready to use.

Your login session is stored locally in `%LOCALAPPDATA%\QuickReadAIOverlay\chrome_profile`, so future launches start straight away.

## Controls

| Control | Action |
|---|---|
| **Dot (`●`)** | Left-click to snip (`1–5` images). Drag to move (snaps to screen edges). |
| **`Done N/5` pill** *(below dot)* | Appears after Snip #1 (`1–4` snips). Move/scroll freely and click the dot to add more snips, click `Done N/5` to submit, or finish a 5th snip to auto-submit. |
| **Answer Box** | Translucent light-white & **100% click-through** (clicks pass straight to apps underneath). Hover over long answers and use your **scroll wheel or two-finger touchpad** to scroll. |
| **Lives (`2/2`)** *(top-left)* | Outside left-click hides the box for free. Clicking the dot toggles the same answer up to 2 times (`2/2 → 1/2 → 0/2`); the next dot click clears it and starts a new snip. |
| **Star (`☆` / `★`)** *(above dot)* | Shown while the answer box is open. Turn ON (`★`, shows `∞`) to keep the same answer across unlimited close/re-open cycles without spending lives. |
| **Pin (`📌`)** *(below dot)* | Shown while the answer box is open. Turn ON (filled) to keep the box open when clicking outside; clicking the dot still hides/re-opens it. |
| **System Tray Icon** | Right-click the tray icon to **Show / Locate Dot**, re-open **Google Login / Verify...**, or **Quit** cleanly. |

## Optional: Gemini API Mode

If you prefer using the official Gemini API instead of Chrome, set `GEMINI_API_KEY` in your environment before running `python main.py`.

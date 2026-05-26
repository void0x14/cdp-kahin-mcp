"""Browser binary auto-detection."""

import shutil


def find_chrome() -> str:
    """Auto-detect Chrome/Chromium binary path."""
    candidates = ["chromium-browser", "chromium", "google-chrome", "google-chrome-stable", "chrome"]
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    msg = "No Chrome/Chromium binary found. Install chromium or set PATH."
    raise RuntimeError(msg)

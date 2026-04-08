"""Manage the XDG autostart .desktop file for FlareReminder.

When "Launch at startup" is enabled in settings we write
``~/.config/autostart/flarereminder.desktop``. When disabled, we delete
it. The Exec= field uses ``sys.executable`` followed by the entry-point
script when running from source, or the PyInstaller binary path when
running frozen.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from . import __app_name__

log = logging.getLogger(__name__)

AUTOSTART_DIR = Path(os.path.expanduser("~/.config/autostart"))
DESKTOP_FILE = AUTOSTART_DIR / "flarereminder.desktop"


def _exec_command() -> str:
    """Return the command to put into the .desktop Exec= field."""
    if getattr(sys, "frozen", False):
        # PyInstaller bundle.
        exe = sys.executable
        return f'"{exe}"'
    # Running from source: invoke the same interpreter on flarereminder.main
    return f'"{sys.executable}" -m flarereminder.main'


def enabled() -> bool:
    return DESKTOP_FILE.exists()


def enable() -> bool:
    """Write the autostart .desktop file. Returns True on success."""
    try:
        AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
        contents = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={__app_name__}\n"
            "Comment=Lens-flare visual reminders for KDE Plasma\n"
            f"Exec={_exec_command()}\n"
            "Terminal=false\n"
            "Categories=Utility;\n"
            "X-GNOME-Autostart-enabled=true\n"
            "X-KDE-autostart-after=panel\n"
        )
        DESKTOP_FILE.write_text(contents, encoding="utf-8")
        log.info("Wrote autostart file %s", DESKTOP_FILE)
        return True
    except OSError as exc:
        log.warning("Could not write autostart file: %s", exc)
        return False


def disable() -> bool:
    """Remove the autostart .desktop file. Returns True if deleted or absent."""
    try:
        if DESKTOP_FILE.exists():
            DESKTOP_FILE.unlink()
            log.info("Removed autostart file %s", DESKTOP_FILE)
        return True
    except OSError as exc:
        log.warning("Could not remove autostart file: %s", exc)
        return False


def set_enabled(enable_flag: bool) -> bool:
    return enable() if enable_flag else disable()

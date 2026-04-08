"""Logging configuration for FlareReminder.

Logs go to both the console (stderr) and a rotating file at
``~/.local/share/flarereminder/flarereminder.log``.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

LOG_DIR = Path(os.path.expanduser("~/.local/share/flarereminder"))
LOG_FILE = LOG_DIR / "flarereminder.log"

_FMT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(debug: bool = False) -> logging.Logger:
    """Configure root logging and return the FlareReminder logger.

    Safe to call more than once; idempotent.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    # Don't double-add handlers if called twice.
    if getattr(root, "_flarereminder_configured", False):
        root.setLevel(logging.DEBUG if debug else logging.INFO)
        return logging.getLogger("flarereminder")

    root.setLevel(logging.DEBUG if debug else logging.INFO)
    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    root.addHandler(console)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover - filesystem edge case
        root.warning("Could not open log file %s: %s", LOG_FILE, exc)

    root._flarereminder_configured = True  # type: ignore[attr-defined]
    return logging.getLogger("flarereminder")

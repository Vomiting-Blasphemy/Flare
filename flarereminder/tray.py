"""System tray icon, context menu, and resource loading.

Uses ``QSystemTrayIcon`` which on KDE Plasma is implemented natively via
the StatusNotifierItem D-Bus protocol. The icon is loaded from a small
hand-authored SVG embedded as a Qt resource (the SVG lives under
``flarereminder/resources/`` and is bundled at PyInstaller time via
``--add-data``).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QIcon
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from . import __app_name__

log = logging.getLogger(__name__)


def _resource_dir() -> Path:
    """Locate the resources directory whether running from source or PyInstaller."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "flarereminder" / "resources"  # type: ignore[attr-defined]
    return Path(__file__).parent / "resources"


def load_tray_icon(suspended: bool = False) -> QIcon:
    name = "tray_icon_suspended.svg" if suspended else "tray_icon.svg"
    path = _resource_dir() / name
    if path.exists():
        return QIcon(str(path))
    log.warning("Tray icon resource %s missing; using empty icon", path)
    return QIcon()


class TrayIcon(QObject):
    """Wrapper around QSystemTrayIcon with named signals for menu actions."""

    open_settings = pyqtSignal()
    acknowledge_all = pyqtSignal()
    suspend_for = pyqtSignal(int)         # seconds; 0 means until restart
    resume_now = pyqtSignal()
    show_session_stats = pyqtSignal()
    export_stats = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tray = QSystemTrayIcon(load_tray_icon(False))
        self._tray.setToolTip(__app_name__)
        self._tray.activated.connect(self._on_activated)
        self._suspended = False
        self._build_menu()
        self._tray.show()

    # ---- public ----------------------------------------------------------

    def set_suspended(self, suspended: bool) -> None:
        if suspended == self._suspended:
            return
        self._suspended = suspended
        self._tray.setIcon(load_tray_icon(suspended))
        self._resume_action.setEnabled(suspended)
        self._tray.setToolTip(
            f"{__app_name__} (suspended)" if suspended else __app_name__
        )

    def show_message(self, title: str, body: str) -> None:
        try:
            self._tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 4000)
        except Exception as exc:  # noqa: BLE001
            log.debug("showMessage failed: %s", exc)

    # ---- internals -------------------------------------------------------

    def _build_menu(self) -> None:
        menu = QMenu()

        a_open = QAction("Open Settings", menu)
        a_open.triggered.connect(self.open_settings)
        menu.addAction(a_open)

        a_ack = QAction("Acknowledge All Flares", menu)
        a_ack.triggered.connect(self.acknowledge_all)
        menu.addAction(a_ack)

        menu.addSeparator()

        # Suspend submenu.
        suspend_menu = QMenu("Suspend Flares", menu)
        for label, seconds in (
            ("Suspend for 1 hour", 3600),
            ("Suspend for 2 hours", 7200),
            ("Suspend for 3 hours", 10800),
            ("Suspend until restart", 0),
        ):
            act = QAction(label, suspend_menu)
            act.triggered.connect(lambda _checked=False, s=seconds: self.suspend_for.emit(s))
            suspend_menu.addAction(act)
        suspend_menu.addSeparator()
        self._resume_action = QAction("Resume now", suspend_menu)
        self._resume_action.triggered.connect(self.resume_now)
        self._resume_action.setEnabled(False)
        suspend_menu.addAction(self._resume_action)
        menu.addMenu(suspend_menu)

        menu.addSeparator()

        a_session = QAction("View This Session Stats", menu)
        a_session.triggered.connect(self.show_session_stats)
        menu.addAction(a_session)

        a_export = QAction("Export Stats to CSV…", menu)
        a_export.triggered.connect(self.export_stats)
        menu.addAction(a_export)

        menu.addSeparator()

        a_quit = QAction("Quit", menu)
        a_quit.triggered.connect(self.quit_requested)
        menu.addAction(a_quit)

        self._menu = menu
        self._tray.setContextMenu(menu)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Left-click → open settings.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_settings.emit()

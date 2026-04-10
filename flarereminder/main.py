"""FlareReminder entry point — wires together all the modules.

Run from source::

    python -m flarereminder.main [--debug] [--test-flare]

When packaged via PyInstaller, the binary ``flarereminder`` runs the same
``main()`` function below.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from typing import Optional

from .logging_setup import setup_logging
from .layer_shell import maybe_enable_layer_shell_integration

# IMPORTANT: try the layer-shell-qt integration BEFORE QApplication exists.
maybe_enable_layer_shell_integration()

from PyQt6.QtCore import QCoreApplication, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from . import __app_id__, __app_name__, __version__
from .config import AppConfig, Reminder, load_config, save_config
from .hotkey import HotkeyManager
from .overlay import OverlayWindow
from .scheduler import (
    Scheduler,
    install_dbus_listeners,
    install_idle_watcher,
)
from .settings_window import SettingsWindow
from .stats import StatsDB
from .tray import TrayIcon

log = logging.getLogger("flarereminder.main")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog=__app_id__, description=__app_name__)
    p.add_argument("--debug", action="store_true", help="enable verbose logging")
    p.add_argument(
        "--test-flare",
        action="store_true",
        help="render one flare frame to /tmp/flarereminder-test.png and exit",
    )
    p.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    return p.parse_args(argv)


def _run_test_flare() -> int:
    """Render a single combined flare frame to a PNG and exit (no event loop)."""
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QColor, QImage, QPainter

    from .config import AppConfig
    from .flare_renderer import FlareParams, blend_colors, render_flare

    cfg = AppConfig.defaults()
    img = QImage(1920, 1080, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor(20, 20, 30, 255))
    p = QPainter(img)
    blended = blend_colors([r.color for r in cfg.reminders if r.enabled])
    render_flare(
        p,
        QRectF(0, 0, img.width(), img.height()),
        FlareParams(color=blended, intensity=1.0),
    )
    p.end()
    out = "/tmp/flarereminder-test.png"
    img.save(out)
    print(f"wrote {out}")
    return 0


class App:
    """Bundles all the long-lived objects and signal wiring."""

    def __init__(self, qapp: QApplication) -> None:
        self.qapp = qapp
        self.config: AppConfig = load_config()
        self.stats = StatsDB()
        self.scheduler = Scheduler(self.config)
        self.overlay = OverlayWindow(self.config.settings)
        self.hotkey = HotkeyManager()
        self.tray: Optional[TrayIcon] = None
        self.settings_window: Optional[SettingsWindow] = None
        self._dbus_handles: list[object] = []
        self._idle_handle = None

        self._init_tray_and_window()
        self._wire_signals()
        self._register_hotkey()
        self._setup_dbus_listeners()
        # Restore suspend badge if config says we're suspended.
        if self.config.suspend.suspended and self.tray is not None:
            self.tray.set_suspended(True)

    # ---- setup ----------------------------------------------------------

    def _init_tray_and_window(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.warning(
                "No system tray available. The settings window will still open "
                "but the tray icon won't appear."
            )
        self.tray = TrayIcon()
        self.settings_window = SettingsWindow(
            self.config,
            self.stats,
            countdown_provider=self._get_countdown_ms,
            in_flight_provider=self._is_in_flight,
        )

    def _wire_signals(self) -> None:
        sched = self.scheduler
        overlay = self.overlay
        tray = self.tray
        sw = self.settings_window
        assert tray is not None
        assert sw is not None

        # Scheduler -> overlay + stats
        sched.fire.connect(self._on_reminder_fired)

        # Hotkey -> ack
        self.hotkey.triggered.connect(self._on_ack_hotkey)

        # Overlay -> stats (record ack delay)
        overlay.acknowledged.connect(self._on_overlay_acknowledged)

        # Tray menu
        tray.open_settings.connect(self._show_settings)
        tray.toggle_settings.connect(self._toggle_settings)
        tray.acknowledge_all.connect(self._on_ack_hotkey)
        tray.suspend_for.connect(self._on_suspend_for)
        tray.resume_now.connect(self._on_resume_now)
        tray.show_session_stats.connect(self._show_session_stats)
        tray.export_stats.connect(self._on_export_stats_from_tray)
        tray.quit_requested.connect(self._on_quit)

        # Settings -> config
        sw.config_changed.connect(self._on_config_changed)
        sw.test_flare_requested.connect(self._on_test_flare)
        sw.acknowledge_requested.connect(self._on_ack_single)
        sw.acknowledge_all_requested.connect(self._on_ack_hotkey)
        sw.hotkey_changed.connect(self._on_hotkey_changed)
        sw.quit_requested.connect(self._on_quit)

    def _register_hotkey(self) -> None:
        strategy = self.hotkey.register(self.config.settings.hotkey)
        if strategy == "none":
            log.warning(
                "Global hotkey registration failed. You can still acknowledge "
                "via the tray icon menu."
            )

    def _setup_dbus_listeners(self) -> None:
        self._dbus_handles = install_dbus_listeners(self.scheduler)
        self._idle_handle = install_idle_watcher(
            self.scheduler, self.config.settings.idle_timeout_seconds
        )

    # ---- signal handlers -----------------------------------------------

    def _on_reminder_fired(self, name: str) -> None:
        reminder = self.config.find_reminder(name)
        if reminder is None:
            return
        event_id = self.stats.record_fire(name)
        self.overlay.add_reminder(reminder, event_id=event_id)

    def _on_ack_hotkey(self) -> None:
        if not self.overlay.is_showing():
            log.debug("Hotkey pressed but no flare visible")
            return
        acked = self.overlay.acknowledge_all()
        for name, event_id in acked:
            if event_id is not None:
                self.stats.record_ack(event_id)
            self.scheduler.acknowledge(name)

    def _on_ack_single(self, name: str) -> None:
        """Acknowledge a single reminder from the settings window."""
        result = self.overlay.acknowledge(name)
        if result is not None:
            _, event_id = result
            if event_id is not None:
                self.stats.record_ack(event_id)
        self.scheduler.acknowledge(name)

    def _get_countdown_ms(self, name: str) -> int:
        """Provider callback for the settings countdown column."""
        return self.scheduler.remaining_ms(name)

    def _is_in_flight(self, name: str) -> bool:
        """Provider callback: True if this reminder has fired but not been acked."""
        return name in self.overlay._active

    def _on_overlay_acknowledged(self, names: list[str]) -> None:
        # Already handled in _on_ack_hotkey when triggered by hotkey;
        # but if the overlay was acked some other way (e.g. test) make sure
        # the scheduler still gets notified for those names.
        for name in names:
            self.scheduler.acknowledge(name)

    def _on_test_flare(self, name: str) -> None:
        """Tab "Test Flare" / "Test Combined" — does NOT touch stats or scheduler."""
        if name == "":
            for r in self.config.reminders:
                if r.enabled:
                    self.overlay.add_reminder(r)
        else:
            r = self.config.find_reminder(name)
            if r:
                self.overlay.add_reminder(r)
        QTimer.singleShot(4000, self.overlay.acknowledge_all)

    def _on_config_changed(self) -> None:
        save_config(self.config)
        self.scheduler.reload_from_config(self.config)
        self.stats.save_reminders_backup(json.dumps(self.config.to_dict()))

    def _on_hotkey_changed(self, combo: str) -> None:
        self.config.settings.hotkey = combo
        save_config(self.config)
        self.hotkey.register(combo)

    def _on_suspend_for(self, seconds: int) -> None:
        if seconds == 0:
            self.scheduler.suspend_until_restart()
        else:
            self.scheduler.suspend_for(seconds)
        self.config.suspend.suspended = True
        self.config.suspend.until_unix = self.scheduler.suspend_info().until_unix
        save_config(self.config)
        if self.tray:
            self.tray.set_suspended(True)

    def _on_resume_now(self) -> None:
        self.scheduler.resume_now()
        self.config.suspend.suspended = False
        self.config.suspend.until_unix = None
        save_config(self.config)
        if self.tray:
            self.tray.set_suspended(False)

    def _show_settings(self) -> None:
        if self.settings_window is None:
            return
        self.settings_window.reload(self.config)
        self.settings_window.refresh_stats()
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    def _toggle_settings(self) -> None:
        if self.settings_window is None:
            return
        if self.settings_window.isVisible():
            self.settings_window.hide()
        else:
            self._show_settings()

    def _show_session_stats(self) -> None:
        if self.settings_window is None:
            return
        self.settings_window.refresh_stats()
        self.settings_window._tabs.setCurrentIndex(2)
        self._show_settings()

    def _on_export_stats_from_tray(self) -> None:
        # Delegate to the settings window's export dialog so we get a file picker.
        self._show_session_stats()
        if self.settings_window is not None:
            self.settings_window._export_csv()

    def _on_quit(self) -> None:
        log.info("Quit requested")
        save_config(self.config)
        self.qapp.quit()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    setup_logging(debug=args.debug)
    log.info("%s %s starting", __app_name__, __version__)

    if args.test_flare:
        # Need a QApplication for QImage/QPainter regardless.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _ = QApplication(sys.argv)
        return _run_test_flare()

    qapp = QApplication(sys.argv)
    qapp.setApplicationName(__app_name__)
    qapp.setApplicationDisplayName(__app_name__)
    qapp.setDesktopFileName(__app_id__)
    qapp.setQuitOnLastWindowClosed(False)

    # Allow Ctrl+C to terminate cleanly when launched from a terminal.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    # Tickle the event loop so signals are processed.
    sigtimer = QTimer()
    sigtimer.start(500)
    sigtimer.timeout.connect(lambda: None)

    try:
        _ = App(qapp)
    except Exception:  # noqa: BLE001
        log.exception("Fatal error during startup")
        return 1

    return qapp.exec()


if __name__ == "__main__":
    sys.exit(main())

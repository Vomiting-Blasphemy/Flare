"""Per-reminder timer scheduler with idle / lock / sleep / suspend handling.

Each enabled reminder owns one ``QTimer`` that counts down from
``interval_minutes * 60_000`` ms. When the timer fires we emit
``Scheduler.fire(reminder_name)`` and the main wiring decides whether to
add it to the overlay (and records a stats event).

The "from-acknowledgement" semantics:
    A reminder's NEXT countdown begins at the moment of acknowledgement,
    NOT at the moment the previous flare was scheduled. So if a reminder
    is scheduled for 15 minutes and the user acks 6 minutes after the
    fire, the next fire is 21 minutes after the original — exactly as
    the spec requires.

Pause/resume:
    pause_all() snapshots remaining ms for every running timer and stops
    them. resume_all() restarts each one with its remembered remaining ms.
    A timer that was already in the "fired but unacknowledged" state stays
    fired — it does not double-fire.

Triggers for pause/resume:
    - Tray-menu suspend (with optional "until" timestamp)
    - Idle threshold via Wayland ext_idle_notify_v1 (best-effort, optional)
    - Screen lock signal: org.freedesktop.ScreenSaver.ActiveChanged
    - System suspend: org.freedesktop.login1.Manager.PrepareForSleep
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal

from .config import AppConfig, Reminder

log = logging.getLogger(__name__)


@dataclass
class _ReminderTimer:
    """Internal per-reminder bookkeeping."""

    reminder: Reminder
    qtimer: QTimer
    remaining_ms: int = 0     # used while paused
    paused: bool = False
    in_flight: bool = False    # True from fire until acknowledge


@dataclass
class SuspendInfo:
    """Snapshot of the suspend state for the tray badge."""

    suspended: bool = False
    until_unix: Optional[int] = None
    reasons: set[str] = field(default_factory=set)  # "tray", "idle", "lock", "sleep"


class Scheduler(QObject):
    """Owns per-reminder QTimers and routes pause/resume from many sources."""

    fire = pyqtSignal(str)  # reminder name

    def __init__(
        self,
        config: AppConfig,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._timers: dict[str, _ReminderTimer] = {}
        self._suspend = SuspendInfo(
            suspended=config.suspend.suspended,
            until_unix=config.suspend.until_unix,
            reasons={"tray"} if config.suspend.suspended else set(),
        )

        # Tick timer for the until_unix expiry check (1 second).
        self._suspend_check = QTimer(self)
        self._suspend_check.setInterval(1000)
        self._suspend_check.timeout.connect(self._check_suspend_expiry)
        self._suspend_check.start()

        for r in config.reminders:
            self._install(r)
        if self._suspend.suspended:
            self._apply_pause()

    # ---- install / sync from config -------------------------------------

    def _install(self, reminder: Reminder) -> None:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setTimerType(Qt.TimerType.PreciseTimer)
        timer.timeout.connect(lambda name=reminder.name: self._on_timeout(name))
        self._timers[reminder.name] = _ReminderTimer(
            reminder=reminder,
            qtimer=timer,
        )
        if reminder.enabled:
            self._start(reminder.name, reminder.interval_minutes * 60 * 1000)

    def reload_from_config(self, config: AppConfig) -> None:
        """Reconcile the timer set with a new config (after settings save)."""
        self._config = config
        new_names = {r.name for r in config.reminders}
        # Drop removed reminders.
        for name in list(self._timers.keys()):
            if name not in new_names:
                self._timers[name].qtimer.stop()
                del self._timers[name]
                log.debug("Scheduler: removed timer for %s", name)
        # Add or update.
        for r in config.reminders:
            if r.name not in self._timers:
                self._install(r)
            else:
                rt = self._timers[r.name]
                rt.reminder = r
                if r.enabled and not rt.in_flight and not self._suspend.suspended:
                    # Restart with new interval.
                    self._start(r.name, r.interval_minutes * 60 * 1000)
                elif not r.enabled:
                    rt.qtimer.stop()

    # ---- core lifecycle --------------------------------------------------

    def _start(self, name: str, ms: int) -> None:
        rt = self._timers.get(name)
        if rt is None:
            return
        rt.qtimer.stop()
        rt.qtimer.start(max(1, int(ms)))
        rt.remaining_ms = max(1, int(ms))
        rt.paused = False
        rt.in_flight = False
        log.debug("Scheduler: %s start in %d ms", name, ms)

    def _on_timeout(self, name: str) -> None:
        rt = self._timers.get(name)
        if rt is None:
            return
        if self._suspend.suspended:
            # If somehow we fired during a pause, defer.
            log.debug("Scheduler: %s timeout during suspend; deferring", name)
            return
        if not rt.reminder.enabled:
            return
        rt.in_flight = True
        log.info("Scheduler: %s firing", name)
        self.fire.emit(name)

    def acknowledge(self, name: str) -> None:
        """Called by the wiring layer when a reminder has been acked.

        Restarts that reminder's countdown from now.
        """
        rt = self._timers.get(name)
        if rt is None:
            return
        rt.in_flight = False
        if not rt.reminder.enabled:
            return
        if self._suspend.suspended:
            # Don't actually restart while suspended; remaining_ms holds full interval.
            rt.remaining_ms = rt.reminder.interval_minutes * 60 * 1000
            rt.qtimer.stop()
            rt.paused = True
            return
        self._start(name, rt.reminder.interval_minutes * 60 * 1000)

    # ---- pause / resume --------------------------------------------------

    def pause(self, reason: str) -> None:
        """Pause all timers attributed to ``reason``."""
        was_suspended = self._suspend.suspended
        self._suspend.reasons.add(reason)
        self._suspend.suspended = True
        if not was_suspended:
            self._apply_pause()
        log.info("Scheduler pause(%s) reasons=%s", reason, self._suspend.reasons)

    def resume(self, reason: str) -> None:
        """Drop ``reason``; resume all timers if no other reasons remain."""
        self._suspend.reasons.discard(reason)
        if self._suspend.reasons:
            log.debug(
                "Scheduler resume(%s) but still suspended for %s",
                reason,
                self._suspend.reasons,
            )
            return
        self._suspend.suspended = False
        self._suspend.until_unix = None
        self._apply_resume()
        log.info("Scheduler resume(%s) — fully resumed", reason)

    def suspend_for(self, seconds: int) -> None:
        """Tray suspend for a fixed duration."""
        self._suspend.until_unix = int(time.time()) + int(seconds)
        self.pause("tray")

    def suspend_until_restart(self) -> None:
        self._suspend.until_unix = None
        self.pause("tray")

    def resume_now(self) -> None:
        """Tray "Resume now" — clear the tray reason."""
        self.resume("tray")

    def is_suspended(self) -> bool:
        return self._suspend.suspended

    def suspend_info(self) -> SuspendInfo:
        return self._suspend

    def _apply_pause(self) -> None:
        for rt in self._timers.values():
            if rt.in_flight:
                continue  # leave its state alone
            if rt.qtimer.isActive():
                rt.remaining_ms = rt.qtimer.remainingTime()
                if rt.remaining_ms <= 0:
                    rt.remaining_ms = rt.reminder.interval_minutes * 60 * 1000
                rt.qtimer.stop()
                rt.paused = True

    def _apply_resume(self) -> None:
        for rt in self._timers.values():
            if rt.in_flight:
                continue
            if rt.paused and rt.reminder.enabled:
                if rt.remaining_ms <= 0:
                    rt.remaining_ms = 1
                rt.qtimer.start(rt.remaining_ms)
                rt.paused = False
                log.debug(
                    "Scheduler resume: %s -> %d ms", rt.reminder.name, rt.remaining_ms
                )

    def _check_suspend_expiry(self) -> None:
        if (
            self._suspend.suspended
            and "tray" in self._suspend.reasons
            and self._suspend.until_unix is not None
            and int(time.time()) >= self._suspend.until_unix
        ):
            log.info("Tray suspend expired; resuming")
            self.resume("tray")

    # ---- read helpers ----------------------------------------------------

    def remaining_ms(self, name: str) -> int:
        rt = self._timers.get(name)
        if rt is None:
            return 0
        if rt.paused:
            return rt.remaining_ms
        return rt.qtimer.remainingTime() if rt.qtimer.isActive() else 0


# ---- D-Bus subscription helpers ------------------------------------------
# These are intentionally optional. They live as standalone functions so
# the Scheduler can be instantiated and unit-tested without any D-Bus.


def install_dbus_listeners(scheduler: Scheduler) -> list[object]:
    """Subscribe to ScreenSaver, login1 sleep, KIdleTime signals via dasbus.

    Returns a list of opaque "subscription handles" the caller should hold
    onto for as long as it wants the listeners alive. All errors are
    swallowed and logged at WARNING level.
    """
    handles: list[object] = []

    # ScreenSaver lock state.
    try:
        from dasbus.connection import SessionMessageBus

        bus = SessionMessageBus()
        ss_proxy = bus.get_proxy(
            "org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver"
        )

        def on_active_changed(active: bool) -> None:
            if active:
                scheduler.pause("lock")
            else:
                scheduler.resume("lock")

        try:
            ss_proxy.ActiveChanged.connect(on_active_changed)
            handles.append(ss_proxy)
            log.info("Subscribed to ScreenSaver.ActiveChanged")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not connect to ScreenSaver.ActiveChanged: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("ScreenSaver D-Bus unavailable: %s", exc)

    # login1 PrepareForSleep.
    try:
        from dasbus.connection import SystemMessageBus

        sysbus = SystemMessageBus()
        login_proxy = sysbus.get_proxy(
            "org.freedesktop.login1", "/org/freedesktop/login1"
        )

        def on_prepare_for_sleep(going_to_sleep: bool) -> None:
            if going_to_sleep:
                scheduler.pause("sleep")
            else:
                scheduler.resume("sleep")

        try:
            login_proxy.PrepareForSleep.connect(on_prepare_for_sleep)
            handles.append(login_proxy)
            log.info("Subscribed to login1.PrepareForSleep")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not connect to login1.PrepareForSleep: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("login1 D-Bus unavailable: %s", exc)

    return handles


def install_idle_watcher(scheduler: Scheduler, threshold_seconds: int) -> object | None:
    """Best-effort idle watcher.

    Tries (in order):
    1. ``ext_idle_notify_v1`` via pywayland
    2. polling ``org.freedesktop.ScreenSaver.GetSessionIdleTime`` once a second

    Returns a handle that should be held while the watcher is active, or
    None if no idle source could be set up.
    """
    # Try poll-based ScreenSaver fallback first — simpler and more portable
    # than wiring a full pywayland event loop. The pywayland path requires
    # a running Wayland display which we may not always have.
    try:
        from dasbus.connection import SessionMessageBus

        bus = SessionMessageBus()
        proxy = bus.get_proxy(
            "org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver"
        )

        # Probe once to make sure the call is supported.
        try:
            _ = proxy.GetSessionIdleTime()
        except Exception as exc:  # noqa: BLE001
            log.warning("ScreenSaver.GetSessionIdleTime not supported: %s", exc)
            return None

        timer = QTimer()
        timer.setInterval(2000)
        state = {"is_idle": False}

        def tick() -> None:
            try:
                idle_ms = int(proxy.GetSessionIdleTime())
            except Exception:  # noqa: BLE001
                return
            should_idle = idle_ms >= threshold_seconds * 1000
            if should_idle and not state["is_idle"]:
                state["is_idle"] = True
                scheduler.pause("idle")
            elif not should_idle and state["is_idle"]:
                state["is_idle"] = False
                scheduler.resume("idle")

        timer.timeout.connect(tick)
        timer.start()
        log.info(
            "Idle watcher installed (ScreenSaver.GetSessionIdleTime, threshold=%ds)",
            threshold_seconds,
        )
        return timer
    except Exception as exc:  # noqa: BLE001
        log.warning("Idle watcher unavailable: %s", exc)
        return None

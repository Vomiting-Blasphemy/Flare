"""Fullscreen transparent overlay window that displays the lens flare.

The window is:
    - Frameless, translucent, no-system-background
    - Transparent to input (Qt::WindowTransparentForInput / WA_TransparentForMouseEvents)
    - Stays on top
    - Tool window (no taskbar entry)
    - Sized to the primary screen geometry

It owns a 60 FPS QTimer that drives the intensity animation:
    1. ease-in to 1.0 over ~3 seconds
    2. sustained pulse (sinusoidal ±10% opacity)
    3. on acknowledge(), 0.4-second linear fade-out, then hide

Multiple reminders may be active simultaneously; their colors are
additively blended (see ``flare_renderer.blend_colors``) and the overlay
stays visible until ALL active reminders have been acknowledged.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Callable

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QPainter, QPaintEvent
from PyQt6.QtWidgets import QWidget

from .config import GlobalSettings, Reminder
from .flare_renderer import FlareParams, blend_colors, render_flare
from .layer_shell import OVERLAY_WINDOW_CLASS, apply_overlay_layer, reapply_kwin_keep_above

log = logging.getLogger(__name__)

FRAME_INTERVAL_MS = 16  # ~60 FPS
EASE_IN_SECONDS = 3.0
FADE_OUT_SECONDS = 0.4
PULSE_FREQ_HZ = 0.5  # one full breath every 2 seconds


@dataclass
class _ActiveReminder:
    """A reminder that's currently lighting up the overlay."""

    name: str
    color: tuple[int, int, int, int]
    intensity_override: float  # multiplier; 1.0 if no override
    transparency: float        # 0.0=invisible, 1.0=opaque
    fired_at: float  # time.monotonic()
    event_id: int | None  # stats event id; set by main wiring


class OverlayWindow(QWidget):
    """Frameless transparent overlay that paints the lens flare."""

    acknowledged = pyqtSignal(list)  # emits list of acked reminder names
    finished = pyqtSignal()          # emits after fade-out completes

    def __init__(self, settings: GlobalSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._active: dict[str, _ActiveReminder] = {}
        self._phase: str = "idle"  # "idle" | "ease_in" | "sustain" | "fade_out"
        self._phase_started: float = 0.0
        self._intensity: float = 0.0
        self._strategy: str | None = None

        self._configure_window_flags()
        self._size_to_screen()

        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(FRAME_INTERVAL_MS)
        self._frame_timer.timeout.connect(self._on_frame)

    # ---- window configuration --------------------------------------------

    def _configure_window_flags(self) -> None:
        # Do NOT use BypassWindowManagerHint — it's an X11 concept and on
        # Wayland it prevents the compositor from managing stacking at all,
        # making the window go behind others when focus changes.
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysStackOnTop, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Window class — used by KWin scripting fallback to find the window.
        self.setObjectName(OVERLAY_WINDOW_CLASS)
        try:
            QGuiApplication.setDesktopFileName(OVERLAY_WINDOW_CLASS)
        except Exception:  # noqa: BLE001
            pass

        # Periodic raise timer: re-assert stacking order while visible.
        self._raise_timer = QTimer(self)
        self._raise_timer.setInterval(2000)  # every 2 s
        self._raise_timer.timeout.connect(self._periodic_raise)

    def _size_to_screen(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            log.warning("No primary screen; defaulting overlay to 1920x1080")
            self.setGeometry(0, 0, 1920, 1080)
            return
        geo = screen.geometry()
        self.setGeometry(geo)

    # ---- public API ------------------------------------------------------

    def add_reminder(self, reminder: Reminder, event_id: int | None = None) -> None:
        """Add or refresh an active reminder, and ensure the overlay is shown."""
        intensity_mul = (
            reminder.flare_intensity_override
            if reminder.flare_intensity_override is not None
            else self._settings.flare_intensity
        )
        transparency = (
            reminder.flare_transparency
            if reminder.flare_transparency is not None
            else self._settings.flare_transparency
        )
        self._active[reminder.name] = _ActiveReminder(
            name=reminder.name,
            color=reminder.color,
            intensity_override=intensity_mul,
            transparency=transparency,
            fired_at=time.monotonic(),
            event_id=event_id,
        )
        log.debug(
            "Overlay add_reminder %s color=%s active=%d",
            reminder.name,
            reminder.color,
            len(self._active),
        )
        self._ensure_visible()

    def acknowledge_all(self) -> list[tuple[str, int | None]]:
        """Ack every active reminder, start fade-out, return list of acked items."""
        if not self._active:
            return []
        acked = [(r.name, r.event_id) for r in self._active.values()]
        log.info("Overlay acknowledge_all: %d reminder(s)", len(acked))
        self._active.clear()
        self._begin_fade_out()
        self.acknowledged.emit([name for name, _ in acked])
        return acked

    def acknowledge(self, name: str) -> tuple[str, int | None] | None:
        """Ack a single reminder. Triggers fade-out only if it was the last."""
        ar = self._active.pop(name, None)
        if ar is None:
            return None
        if not self._active:
            self._begin_fade_out()
        self.acknowledged.emit([name])
        return (ar.name, ar.event_id)

    def is_showing(self) -> bool:
        return self._phase != "idle"

    def active_event_ids(self) -> list[int]:
        return [r.event_id for r in self._active.values() if r.event_id is not None]

    def strategy(self) -> str | None:
        return self._strategy

    # ---- phase management ------------------------------------------------

    def _ensure_visible(self) -> None:
        if self._phase in ("idle", "fade_out"):
            self._phase = "ease_in"
            self._phase_started = time.monotonic()
            self._size_to_screen()
            if not self.isVisible():
                self.show()
                self.raise_()
            # Apply layer-shell / KWin keep-above every time we become visible
            # so re-shown windows get the stacking treatment again.
            try:
                handle = self.windowHandle()
                if handle is not None:
                    self._strategy = apply_overlay_layer(handle)
            except Exception as exc:  # noqa: BLE001
                log.warning("apply_overlay_layer failed: %s", exc)
            self._frame_timer.start()
            self._raise_timer.start()
        # If already animating, keep going — new reminder just joins the blend.
        self.update()

    def _begin_fade_out(self) -> None:
        self._phase = "fade_out"
        self._phase_started = time.monotonic()
        self._fade_out_start_intensity = max(self._intensity, 0.001)

    def _on_frame(self) -> None:
        speed = self._settings.animation_speed_multiplier()
        now = time.monotonic()
        elapsed = (now - self._phase_started) * speed

        if self._phase == "ease_in":
            t = min(1.0, elapsed / EASE_IN_SECONDS)
            # Smoothstep ease.
            self._intensity = t * t * (3.0 - 2.0 * t)
            if t >= 1.0:
                self._phase = "sustain"
                self._phase_started = now
        elif self._phase == "sustain":
            # Pulse around 1.0 with ±pulse_intensity. The flare_renderer also
            # applies a pulse via pulse_phase, but we apply ours to intensity
            # too so the very darkest moments dim a bit.
            self._intensity = 1.0
        elif self._phase == "fade_out":
            t = min(1.0, elapsed / FADE_OUT_SECONDS)
            self._intensity = self._fade_out_start_intensity * (1.0 - t)
            if t >= 1.0:
                self._intensity = 0.0
                self._phase = "idle"
                self._frame_timer.stop()
                self._raise_timer.stop()
                self.hide()
                self.finished.emit()
                return

        self.update()

    def _periodic_raise(self) -> None:
        """Re-assert stacking while visible so the overlay stays on top."""
        if self.isVisible():
            self.raise_()
            reapply_kwin_keep_above()

    # ---- painting --------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        if self._phase == "idle" or not self._active and self._phase != "fade_out":
            return
        rect = QRectF(self.rect())
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            blended_color = blend_colors([r.color for r in self._active.values()]) if self._active else (255, 255, 255, 255)
            # Average overrides across active reminders.
            if self._active:
                override = sum(r.intensity_override for r in self._active.values()) / len(self._active)
                transparency = sum(r.transparency for r in self._active.values()) / len(self._active)
            else:
                override = self._settings.flare_intensity
                transparency = self._settings.flare_transparency
            pulse_phase = (time.monotonic() * 2.0 * math.pi * PULSE_FREQ_HZ) % (2 * math.pi)
            params = FlareParams(
                color=blended_color,
                intensity=self._intensity,
                glow_radius=self._settings.glow_radius,
                streak_length=self._settings.streak_length,
                streak_count=self._settings.streak_count,
                secondary_count=self._settings.secondary_count,
                pulse_phase=pulse_phase,
                pulse_intensity=self._settings.pulse_intensity,
                intensity_multiplier=override,
                transparency=transparency,
            )
            render_flare(painter, rect, params)
        finally:
            painter.end()

    # ---- testing helper --------------------------------------------------

    def force_frame_advance(self, dt_seconds: float) -> None:
        """Test-only: pretend ``dt_seconds`` of wall time has elapsed.

        Used by tests that don't want to spin a real event loop.
        """
        self._phase_started -= dt_seconds
        self._on_frame()

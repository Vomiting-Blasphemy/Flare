"""Pure-rendering helpers for the lens-flare effect.

This module is intentionally free of any QWindow / QApplication state — it
takes a ``QPainter`` and a ``QRect`` plus a parameter object, and draws the
flare. That makes it cheap to unit-test by rendering into an offscreen
``QImage`` (see ``tests/test_flare_render_offscreen.py``).

The flare originates from the upper-right corner of the rect and points
diagonally toward the centre of the screen. Layers, in painting order:

1. Wide chromatic bloom (a soft halo, slightly larger than the glow)
2. Central radial glow (white→transparent radial gradient)
3. 4–6 thin streak lines radiating outward (alpha-feathered)
4. 2–3 secondary lens artifact circles along the flare axis

All layers respect ``intensity`` (0..1, multiplied through alpha) and the
combined RGBA ``color``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)


@dataclass
class FlareParams:
    """All knobs that drive a single flare frame."""

    color: tuple[int, int, int, int] = (255, 255, 255, 255)
    intensity: float = 1.0           # 0..1 — overall opacity multiplier
    glow_radius: int = 360            # px
    streak_length: int = 1100         # px
    streak_count: int = 5             # 4..6
    secondary_count: int = 3          # 2..3
    pulse_phase: float = 0.0          # radians, used for pulse animation
    pulse_intensity: float = 0.10     # 10% sinusoidal swing
    intensity_multiplier: float = 1.0  # global×reminder override
    transparency: float = 1.0          # 0.0=invisible, 1.0=fully opaque
    # Origin offset from the upper-right corner; default is exactly the
    # corner. Negative x moves left, positive y moves down.
    origin_offset: tuple[int, int] = field(default=(0, 0))


def _scaled_color(rgba: tuple[int, int, int, int], alpha_mul: float) -> QColor:
    r, g, b, a = rgba
    a_out = max(0, min(255, int(a * max(0.0, alpha_mul))))
    return QColor(r, g, b, a_out)


def render_flare(painter: QPainter, rect: QRectF, params: FlareParams) -> None:
    """Render one flare frame into ``rect`` using ``painter``.

    The painter is left with the same render hints / composition mode that
    were active before the call (we save/restore).
    """
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)

        # Effective intensity, with sinusoidal pulse at full brightness.
        pulse = 1.0 + params.pulse_intensity * math.sin(params.pulse_phase)
        eff_intensity = (
            max(0.0, min(1.0, params.intensity))
            * max(0.0, params.intensity_multiplier)
            * max(0.0, min(1.0, params.transparency))
            * pulse
        )
        if eff_intensity <= 0.0:
            return

        # Origin = upper-right corner + offset.
        origin = QPointF(
            rect.right() + params.origin_offset[0],
            rect.top() + params.origin_offset[1],
        )
        # Direction = toward the rect centre, normalised.
        centre = QPointF(rect.center().x(), rect.center().y())
        dx = centre.x() - origin.x()
        dy = centre.y() - origin.y()
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length

        _draw_chromatic_bloom(painter, origin, params, eff_intensity)
        _draw_central_glow(painter, origin, params, eff_intensity)
        _draw_streaks(painter, origin, params, eff_intensity, ux, uy)
        _draw_secondary_artifacts(painter, origin, params, eff_intensity, ux, uy)
    finally:
        painter.restore()


# ---- individual layers ---------------------------------------------------


def _draw_chromatic_bloom(
    painter: QPainter,
    origin: QPointF,
    params: FlareParams,
    intensity: float,
) -> None:
    radius = params.glow_radius * 1.7
    grad = QRadialGradient(origin, radius)
    # Two halos: warm outer + the actual color inside.
    inner = _scaled_color(params.color, 0.30 * intensity)
    edge_warm = _scaled_color(
        (
            min(255, params.color[0] + 30),
            min(255, params.color[1] + 10),
            params.color[2],
            params.color[3],
        ),
        0.10 * intensity,
    )
    transparent = _scaled_color(params.color, 0.0)
    grad.setColorAt(0.0, inner)
    grad.setColorAt(0.55, edge_warm)
    grad.setColorAt(1.0, transparent)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(grad))
    painter.drawEllipse(origin, radius, radius)


def _draw_central_glow(
    painter: QPainter,
    origin: QPointF,
    params: FlareParams,
    intensity: float,
) -> None:
    radius = float(params.glow_radius)
    grad = QRadialGradient(origin, radius)
    grad.setColorAt(0.0, _scaled_color(params.color, 1.0 * intensity))
    grad.setColorAt(0.18, _scaled_color(params.color, 0.85 * intensity))
    grad.setColorAt(0.5, _scaled_color(params.color, 0.35 * intensity))
    grad.setColorAt(1.0, _scaled_color(params.color, 0.0))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(grad))
    painter.drawEllipse(origin, radius, radius)


def _draw_streaks(
    painter: QPainter,
    origin: QPointF,
    params: FlareParams,
    intensity: float,
    ux: float,
    uy: float,
) -> None:
    """Draw N streaks symmetrically fanning around the centre→corner axis."""
    n = max(4, min(6, params.streak_count))
    length = float(params.streak_length)
    base_angle = math.atan2(uy, ux)
    # Spread streaks across ~80° fan.
    spread = math.radians(80.0)
    for i in range(n):
        if n == 1:
            t = 0.0
        else:
            t = i / (n - 1) - 0.5  # -0.5..0.5
        ang = base_angle + spread * t
        end = QPointF(
            origin.x() + math.cos(ang) * length,
            origin.y() + math.sin(ang) * length,
        )
        # Per-streak alpha: middle streaks are slightly brighter.
        weight = 1.0 - abs(t) * 1.4
        weight = max(0.25, weight)
        # Build a feathered linear gradient along the streak.
        grad = QLinearGradient(origin, end)
        grad.setColorAt(0.0, _scaled_color(params.color, 0.85 * intensity * weight))
        grad.setColorAt(0.15, _scaled_color(params.color, 0.55 * intensity * weight))
        grad.setColorAt(0.6, _scaled_color(params.color, 0.18 * intensity * weight))
        grad.setColorAt(1.0, _scaled_color(params.color, 0.0))
        pen = QPen(QBrush(grad), 4.0 + 2.0 * weight)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(origin, end)


def _draw_secondary_artifacts(
    painter: QPainter,
    origin: QPointF,
    params: FlareParams,
    intensity: float,
    ux: float,
    uy: float,
) -> None:
    """Draw small lens 'ghost' circles along the flare axis."""
    n = max(2, min(3, params.secondary_count))
    # Distances along the axis as fractions of the streak length.
    distances = [0.30, 0.55, 0.80][:n]
    sizes = [params.glow_radius * 0.45, params.glow_radius * 0.30, params.glow_radius * 0.22][:n]
    alphas = [0.22, 0.16, 0.12][:n]
    for d, size, a in zip(distances, sizes, alphas):
        cx = origin.x() + ux * params.streak_length * d
        cy = origin.y() + uy * params.streak_length * d
        c = QPointF(cx, cy)
        grad = QRadialGradient(c, size)
        grad.setColorAt(0.0, _scaled_color(params.color, a * intensity))
        grad.setColorAt(0.6, _scaled_color(params.color, a * 0.4 * intensity))
        grad.setColorAt(1.0, _scaled_color(params.color, 0.0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(grad))
        painter.drawEllipse(c, size, size)


# ---- additive color blending --------------------------------------------


def blend_colors(colors: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    """Blend a list of RGBA colors using weighted averaging for visible mixing.

    Uses a brightness-weighted average so that combining white + cornflower
    blue produces a visible blue-tinted result rather than pure white.
    Alpha is taken as the maximum across all inputs.

    Empty list returns transparent black.
    """
    if not colors:
        return (0, 0, 0, 0)
    if len(colors) == 1:
        return colors[0]
    n = len(colors)
    r_sum = g_sum = b_sum = 0
    a_max = 0
    for cr, cg, cb, ca in colors:
        r_sum += cr
        g_sum += cg
        b_sum += cb
        a_max = max(a_max, ca)
    # Average the RGB channels so that distinct hues remain visible.
    return (
        min(255, r_sum // n),
        min(255, g_sum // n),
        min(255, b_sum // n),
        min(255, a_max),
    )

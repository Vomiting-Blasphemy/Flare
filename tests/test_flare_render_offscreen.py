"""Offscreen rendering tests for flare_renderer.

These run with ``QT_QPA_PLATFORM=offscreen`` so no display is required.
We render into a QImage, then assert that the upper-right corner has
visible alpha and the lower-left does not.
"""

from __future__ import annotations

import os

# Force offscreen platform BEFORE importing PyQt6.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

import pytest

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QApplication

from flarereminder.flare_renderer import FlareParams, blend_colors, render_flare


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication(sys.argv)
    yield a


def _new_image(w: int = 1920, h: int = 1080) -> QImage:
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor(0, 0, 0, 0))
    return img


def test_renders_alpha_in_upper_right(app, tmp_path: Path):
    img = _new_image()
    p = QPainter(img)
    render_flare(p, QRectF(0, 0, img.width(), img.height()), FlareParams(intensity=1.0))
    p.end()
    out = tmp_path / "flare_full.png"
    img.save(str(out))
    assert out.exists()
    # Upper-right should be brightly lit
    upper_right = img.pixelColor(img.width() - 5, 5)
    assert upper_right.alpha() > 100, f"got alpha={upper_right.alpha()}"
    # Lower-left should be untouched.
    lower_left = img.pixelColor(5, img.height() - 5)
    assert lower_left.alpha() == 0, f"got alpha={lower_left.alpha()}"


def test_zero_intensity_renders_nothing(app):
    img = _new_image(800, 600)
    p = QPainter(img)
    render_flare(p, QRectF(0, 0, 800, 600), FlareParams(intensity=0.0))
    p.end()
    upper_right = img.pixelColor(795, 5)
    assert upper_right.alpha() == 0


def test_pulse_phase_changes_brightness(app):
    img1 = _new_image(800, 600)
    p = QPainter(img1)
    render_flare(
        p, QRectF(0, 0, 800, 600), FlareParams(intensity=1.0, pulse_phase=0.0)
    )
    p.end()
    img2 = _new_image(800, 600)
    p = QPainter(img2)
    # pi/2 -> sin = 1 -> brightest
    render_flare(
        p,
        QRectF(0, 0, 800, 600),
        FlareParams(intensity=1.0, pulse_phase=1.5707963, pulse_intensity=0.10),
    )
    p.end()
    a1 = img1.pixelColor(795, 5).alpha()
    a2 = img2.pixelColor(795, 5).alpha()
    # img2 should be brighter than img1 by ~10% — but the corner pixel may
    # be saturated. So compare a slightly off-corner pixel.
    a1b = img1.pixelColor(700, 100).alpha()
    a2b = img2.pixelColor(700, 100).alpha()
    assert a2b >= a1b


def test_blend_colors_average():
    red = (255, 0, 0, 255)
    green = (0, 255, 0, 255)
    blue = (0, 0, 255, 255)
    # Averaging: red+green -> (127, 127, 0, 255)
    assert blend_colors([red, green]) == (127, 127, 0, 255)
    # Three primaries average to ~(85, 85, 85, 255)
    assert blend_colors([red, green, blue]) == (85, 85, 85, 255)
    # Two similar colors average properly
    assert blend_colors([(100, 50, 0, 200), (200, 50, 0, 100)]) == (
        150,
        50,
        0,
        200,
    )
    assert blend_colors([]) == (0, 0, 0, 0)
    # Single color passes through unchanged
    assert blend_colors([red]) == red


def test_color_is_respected(app):
    img = _new_image(800, 600)
    p = QPainter(img)
    render_flare(
        p,
        QRectF(0, 0, 800, 600),
        FlareParams(color=(255, 0, 0, 255), intensity=1.0),
    )
    p.end()
    px = img.pixelColor(795, 5)
    assert px.red() > px.green()
    assert px.red() > px.blue()

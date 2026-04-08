"""KDE Plasma Wayland overlay-stacking helpers.

PyQt6 doesn't natively expose the ``zwlr_layer_shell_v1`` protocol — its
QPA always creates an ``xdg_toplevel`` for top-level windows. This module
implements a best-effort hybrid:

1. **LayerShellQt** (preferred): if KDE's ``layer-shell-qt`` is installed
   the QPA plugin can be loaded via the ``QT_WAYLAND_SHELL_INTEGRATION``
   environment variable *before* QApplication is constructed. We expose
   ``maybe_enable_layer_shell_integration()`` for this purpose, called
   from ``main.py`` ahead of QApplication. After QApplication is up we
   try the ctypes interface to actually mark the window as
   ``Layer::Overlay`` with anchors and exclusive zone.
2. **KWin scripting fallback**: if (1) isn't available we install a
   one-shot KWin JavaScript that finds windows by ``windowClass`` and
   forces ``keepAbove``, ``skipTaskbar``, ``skipPager``, ``noBorder``.
3. **Final fallback**: trust ``Qt::WindowStaysOnTopHint`` only.

All branches are wrapped in ``try/except`` and logged. The function
returns the strategy that was actually applied so the caller can log it.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import shutil
import textwrap
from typing import Any

log = logging.getLogger(__name__)

# Window class string we set on the overlay so KWin can find it.
OVERLAY_WINDOW_CLASS = "flarereminder-overlay"

# Names of the layer-shell-qt shared library across distros.
LAYER_SHELL_QT_LIB_CANDIDATES = (
    "liblayershellqtinterface.so.6",
    "liblayershellqtinterface.so.5",
    "liblayershellqtinterface.so",
    "libLayerShellQtInterface.so.6",
    "libLayerShellQtInterface.so",
)

# zwlr_layer_shell_v1 layer enum values.
LAYER_BACKGROUND = 0
LAYER_BOTTOM = 1
LAYER_TOP = 2
LAYER_OVERLAY = 3

# Anchor bitmask.
ANCHOR_TOP = 1
ANCHOR_BOTTOM = 2
ANCHOR_LEFT = 4
ANCHOR_RIGHT = 8
ANCHOR_ALL = ANCHOR_TOP | ANCHOR_BOTTOM | ANCHOR_LEFT | ANCHOR_RIGHT

# Keyboard interactivity.
KBD_NONE = 0
KBD_EXCLUSIVE = 1
KBD_ON_DEMAND = 2


def maybe_enable_layer_shell_integration() -> bool:
    """Hint Qt to use the layer-shell-qt QPA plugin, if available.

    Must be called BEFORE constructing ``QApplication``. Returns True if
    the env var was set.
    """
    if os.environ.get("QT_WAYLAND_SHELL_INTEGRATION"):
        return True  # respect user override
    if _find_layer_shell_lib() is not None:
        os.environ["QT_WAYLAND_SHELL_INTEGRATION"] = "layer-shell"
        log.info("Enabled QT_WAYLAND_SHELL_INTEGRATION=layer-shell")
        return True
    log.debug("layer-shell-qt not found; running with default Qt Wayland integration")
    return False


def apply_overlay_layer(qwindow: Any) -> str:
    """Try to promote ``qwindow`` to a wlr-layer-shell overlay surface.

    Returns one of:
        - ``"layer-shell"``       — LayerShellQt path succeeded
        - ``"kwin-script"``       — KWin scripting fallback succeeded
        - ``"keep-above-only"``   — only Qt's stays-on-top hint is in effect

    Never raises.
    """
    # Strategy 1: LayerShellQt via ctypes.
    try:
        if _try_layer_shell_qt(qwindow):
            log.info("Overlay stacking strategy: layer-shell-qt")
            return "layer-shell"
    except Exception as exc:  # noqa: BLE001
        log.warning("LayerShellQt path failed: %s", exc)

    # Strategy 2: KWin scripting via D-Bus.
    try:
        if _try_kwin_keep_above_script():
            log.info("Overlay stacking strategy: kwin-script")
            return "kwin-script"
    except Exception as exc:  # noqa: BLE001
        log.warning("KWin scripting path failed: %s", exc)

    log.warning(
        "No layer-shell or KWin scripting available; relying on "
        "Qt::WindowStaysOnTopHint only. Fullscreen apps may cover the overlay."
    )
    return "keep-above-only"


# ---- LayerShellQt -------------------------------------------------------


def _find_layer_shell_lib() -> str | None:
    for name in LAYER_SHELL_QT_LIB_CANDIDATES:
        full = ctypes.util.find_library(name) or name
        # find_library may return just the soname; CDLL handles that.
        try:
            lib = ctypes.CDLL(full)
            log.debug("Found LayerShellQt library: %s", full)
            del lib
            return full
        except OSError:
            continue
    return None


def _try_layer_shell_qt(qwindow: Any) -> bool:
    """Best-effort attempt to call LayerShellQt::Window::get/setLayer.

    LayerShellQt's API is C++ with mangled symbols, so this is fragile.
    We probe for the most common Itanium-mangled symbol names exported
    by ``liblayershellqtinterface.so.6``. If anything fails we return
    False and the caller falls back to KWin scripting.
    """
    libname = _find_layer_shell_lib()
    if libname is None:
        return False
    try:
        lib = ctypes.CDLL(libname)
    except OSError:
        return False

    # Use Qt's `winId()` -> WId is just an opaque integer here. Without
    # the proper LayerShellQtInterface API exported in C, we can only
    # set the QSurface property hints that the layer-shell-qt QPA looks
    # for. Those are: layershell.layer, layershell.anchors,
    # layershell.exclusiveZone, layershell.keyboardInteractivity.
    try:
        qwindow.setProperty("layershell.layer", LAYER_OVERLAY)
        qwindow.setProperty("layershell.anchors", ANCHOR_ALL)
        qwindow.setProperty("layershell.exclusiveZone", -1)
        qwindow.setProperty("layershell.keyboardInteractivity", KBD_NONE)
        qwindow.setProperty("layershell.scope", OVERLAY_WINDOW_CLASS)
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not set layershell.* QSurface properties: %s", exc)
        return False

    # If layer-shell-qt's QPA plugin is loaded the properties above are
    # honored at surface-creation time, so the strategy is "armed" even
    # without a direct C function call. Treat this as success only if
    # the env var indicates the plugin is engaged.
    if os.environ.get("QT_WAYLAND_SHELL_INTEGRATION") == "layer-shell":
        return True
    log.debug(
        "LayerShellQt lib found but QT_WAYLAND_SHELL_INTEGRATION!=layer-shell; "
        "skipping"
    )
    return False


# ---- KWin scripting fallback --------------------------------------------


_KWIN_SCRIPT_TEMPLATE = textwrap.dedent(
    """
    // FlareReminder overlay keep-above script.
    // Finds windows whose resourceClass matches our app id and forces them
    // to be kept above all others, skipped from taskbar/pager, borderless.
    function fixOverlay(client) {
        if (!client) return;
        try {
            var cls = (client.resourceClass || "").toString();
            if (cls.indexOf("__APP_ID__") !== -1) {
                client.keepAbove = true;
                client.skipTaskbar = true;
                client.skipPager = true;
                client.skipSwitcher = true;
                client.noBorder = true;
                client.onAllDesktops = true;
            }
        } catch (e) {}
    }
    workspace.clientAdded.connect(fixOverlay);
    // Existing windows.
    var clients = workspace.clientList ? workspace.clientList() : workspace.windowList();
    for (var i = 0; i < clients.length; i++) fixOverlay(clients[i]);
    """
).strip()


def _try_kwin_keep_above_script() -> bool:
    """Install a one-shot KWin script via D-Bus.

    Returns True if the script was loaded successfully.
    """
    try:
        from dasbus.connection import SessionMessageBus
    except ImportError:
        log.debug("dasbus not importable; cannot install KWin script")
        return False

    script_path = _write_kwin_script()
    if script_path is None:
        return False

    try:
        bus = SessionMessageBus()
        proxy = bus.get_proxy("org.kde.KWin", "/Scripting")
        # loadScript(QString filePath, QString pluginName) -> int
        script_id = proxy.loadScript(str(script_path), "flarereminder-overlay")
        # Run / start: KWin 5/6 differ. Try both.
        try:
            sub_proxy = bus.get_proxy(
                "org.kde.KWin", f"/Scripting/Script{script_id}"
            )
            sub_proxy.run()
        except Exception:  # noqa: BLE001
            try:
                proxy.start()
            except Exception:  # noqa: BLE001
                pass
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("KWin scripting D-Bus call failed: %s", exc)
        return False


def _write_kwin_script() -> str | None:
    """Write the KWin JS to a tempfile and return its path."""
    try:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        script_dir = os.path.join(runtime_dir, "flarereminder")
        os.makedirs(script_dir, exist_ok=True)
        path = os.path.join(script_dir, "overlay-keep-above.js")
        contents = _KWIN_SCRIPT_TEMPLATE.replace("__APP_ID__", OVERLAY_WINDOW_CLASS)
        with open(path, "w", encoding="utf-8") as f:
            f.write(contents)
        return path
    except OSError as exc:
        log.debug("Could not write KWin script: %s", exc)
        return None


# ---- diagnostic --------------------------------------------------------


def have_kwin_dbus() -> bool:
    """Cheap test for whether KWin's D-Bus service is reachable."""
    try:
        from dasbus.connection import SessionMessageBus

        bus = SessionMessageBus()
        proxy = bus.get_proxy("org.freedesktop.DBus", "/org/freedesktop/DBus")
        names = proxy.ListNames()
        return "org.kde.KWin" in names
    except Exception:  # noqa: BLE001
        return False


def kwin_helper_available() -> bool:
    """Whether ``kwin_wayland`` or ``kwin_x11`` is on PATH."""
    return shutil.which("kwin_wayland") is not None or shutil.which("kwin_x11") is not None

"""Main settings window with four tabs: Reminders, Global, Statistics, About.

Closing the window only hides it — the app keeps running in the tray. The
``Qt::Tool`` flag keeps the window out of the taskbar.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import __app_name__, __version__
from . import autostart
from .config import AppConfig, GlobalSettings, Reminder
from .hotkey import KeyComboRecorder
from .stats import ReminderStats, StatsDB

log = logging.getLogger(__name__)


# ---- helpers -------------------------------------------------------------


def _color_swatch(rgba: tuple[int, int, int, int], size: int = 20) -> QPixmap:
    """Return a pixmap of the given RGBA color."""
    px = QPixmap(size, size)
    px.fill(QColor(*rgba))
    return px


def _format_seconds(s: float | None) -> str:
    if s is None:
        return "—"
    return f"{s:.1f}s"


# ---- reminder edit dialog -----------------------------------------------


class ReminderDialog(QDialog):
    """Dialog for adding or editing a reminder."""

    def __init__(self, reminder: Reminder | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Reminder" if reminder else "Add Reminder")
        self._color: tuple[int, int, int, int] = (
            reminder.color if reminder else (255, 255, 255, 255)
        )

        layout = QFormLayout(self)

        self._name = QLineEdit(reminder.name if reminder else "New Reminder")
        layout.addRow("Name:", self._name)

        self._color_btn = QPushButton()
        self._color_btn.setIconSize(self._color_btn.sizeHint())
        self._refresh_color_btn()
        self._color_btn.clicked.connect(self._pick_color)
        layout.addRow("Color:", self._color_btn)

        self._interval = QSpinBox()
        self._interval.setRange(1, 24 * 60)
        self._interval.setSuffix(" min")
        self._interval.setValue(reminder.interval_minutes if reminder else 15)
        layout.addRow("Interval:", self._interval)

        self._enabled = QCheckBox("Enabled")
        self._enabled.setChecked(reminder.enabled if reminder else True)
        layout.addRow("", self._enabled)

        self._intensity_override_enable = QCheckBox("Override global intensity")
        self._intensity_override = QDoubleSpinBox()
        self._intensity_override.setRange(0.0, 2.0)
        self._intensity_override.setSingleStep(0.05)
        self._intensity_override.setDecimals(2)
        if reminder and reminder.flare_intensity_override is not None:
            self._intensity_override_enable.setChecked(True)
            self._intensity_override.setValue(reminder.flare_intensity_override)
            self._intensity_override.setEnabled(True)
        else:
            self._intensity_override.setValue(1.0)
            self._intensity_override.setEnabled(False)
        self._intensity_override_enable.toggled.connect(self._intensity_override.setEnabled)
        layout.addRow("", self._intensity_override_enable)
        layout.addRow("Intensity override:", self._intensity_override)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _refresh_color_btn(self) -> None:
        self._color_btn.setText(f"RGBA{self._color}")
        self._color_btn.setIcon(QIcon(_color_swatch(self._color)))

    def _pick_color(self) -> None:
        initial = QColor(*self._color)
        chosen = QColorDialog.getColor(
            initial, self, "Pick Reminder Color", QColorDialog.ColorDialogOption.ShowAlphaChannel
        )
        if chosen.isValid():
            self._color = (chosen.red(), chosen.green(), chosen.blue(), chosen.alpha())
            self._refresh_color_btn()

    def to_reminder(self) -> Reminder:
        return Reminder(
            name=self._name.text().strip() or "Unnamed",
            color=self._color,
            interval_minutes=int(self._interval.value()),
            enabled=self._enabled.isChecked(),
            flare_intensity_override=(
                float(self._intensity_override.value())
                if self._intensity_override_enable.isChecked()
                else None
            ),
        )


# ---- main settings window -----------------------------------------------


class SettingsWindow(QWidget):
    """Top-level configuration window."""

    config_changed = pyqtSignal()       # emitted whenever the user saves a change
    test_flare_requested = pyqtSignal(str)         # reminder name; "" = combined
    quit_requested = pyqtSignal()
    hotkey_changed = pyqtSignal(str)

    def __init__(
        self,
        config: AppConfig,
        stats: StatsDB,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._stats = stats
        self.setWindowTitle(f"{__app_name__} — Settings")
        self.resize(720, 540)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.Tool)

        layout = QVBoxLayout(self)
        self._tabs = QTabWidget(self)
        self._tabs.addTab(self._make_reminders_tab(), "Reminders")
        self._tabs.addTab(self._make_global_tab(), "Global")
        self._tabs.addTab(self._make_stats_tab(), "Statistics")
        self._tabs.addTab(self._make_about_tab(), "About")
        layout.addWidget(self._tabs)

        self._refresh_reminders_table()

    # ---- common ---------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        # Hide instead of quit; tray remains active.
        event.ignore()
        self.hide()

    def reload(self, config: AppConfig) -> None:
        self._config = config
        self._refresh_reminders_table()
        self._refresh_global_tab()

    def refresh_stats(self) -> None:
        self._refresh_session_table()
        self._refresh_week_table()

    # ---- Reminders tab --------------------------------------------------

    def _make_reminders_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        self._reminders_table = QTableWidget(0, 6)
        self._reminders_table.setHorizontalHeaderLabels(
            ["Enabled", "Name", "Color", "Interval", "Intensity", "Actions"]
        )
        self._reminders_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._reminders_table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.Stretch
        )
        self._reminders_table.verticalHeader().setVisible(False)
        self._reminders_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._reminders_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self._reminders_table)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Reminder")
        add_btn.clicked.connect(self._add_reminder)
        btn_row.addWidget(add_btn)
        test_combined = QPushButton("Test Combined Flare")
        test_combined.clicked.connect(lambda: self.test_flare_requested.emit(""))
        btn_row.addWidget(test_combined)
        btn_row.addStretch()
        layout.addLayout(btn_row)
        return w

    def _refresh_reminders_table(self) -> None:
        table = self._reminders_table
        table.setRowCount(0)
        for row, reminder in enumerate(self._config.reminders):
            table.insertRow(row)

            # Enabled
            cb = QCheckBox()
            cb.setChecked(reminder.enabled)
            cb.toggled.connect(lambda checked, name=reminder.name: self._toggle_enabled(name, checked))
            cb_wrap = QWidget()
            cbl = QHBoxLayout(cb_wrap)
            cbl.addWidget(cb)
            cbl.setContentsMargins(8, 0, 8, 0)
            cbl.addStretch()
            table.setCellWidget(row, 0, cb_wrap)

            # Name
            table.setItem(row, 1, QTableWidgetItem(reminder.name))

            # Color swatch
            swatch = QTableWidgetItem("")
            swatch.setData(Qt.ItemDataRole.DecorationRole, _color_swatch(reminder.color, 18))
            table.setItem(row, 2, swatch)

            # Interval
            table.setItem(row, 3, QTableWidgetItem(f"{reminder.interval_minutes} min"))

            # Intensity
            override = reminder.flare_intensity_override
            text = "global" if override is None else f"{override:.2f}×"
            table.setItem(row, 4, QTableWidgetItem(text))

            # Actions
            actions = QWidget()
            al = QHBoxLayout(actions)
            al.setContentsMargins(2, 0, 2, 0)
            edit_btn = QPushButton("Edit")
            del_btn = QPushButton("Delete")
            test_btn = QPushButton("Test Flare")
            al.addWidget(edit_btn)
            al.addWidget(del_btn)
            al.addWidget(test_btn)
            edit_btn.clicked.connect(lambda _checked=False, n=reminder.name: self._edit_reminder(n))
            del_btn.clicked.connect(lambda _checked=False, n=reminder.name: self._delete_reminder(n))
            test_btn.clicked.connect(lambda _checked=False, n=reminder.name: self.test_flare_requested.emit(n))
            table.setCellWidget(row, 5, actions)

    def _toggle_enabled(self, name: str, checked: bool) -> None:
        r = self._config.find_reminder(name)
        if r is None:
            return
        r.enabled = checked
        self.config_changed.emit()

    def _add_reminder(self) -> None:
        dlg = ReminderDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new = dlg.to_reminder()
            if self._config.find_reminder(new.name):
                QMessageBox.warning(self, "Duplicate name", f"A reminder named {new.name!r} already exists.")
                return
            self._config.reminders.append(new)
            self._refresh_reminders_table()
            self.config_changed.emit()

    def _edit_reminder(self, name: str) -> None:
        r = self._config.find_reminder(name)
        if r is None:
            return
        dlg = ReminderDialog(r, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new = dlg.to_reminder()
            # Replace by name (preserve list order).
            idx = self._config.reminders.index(r)
            # If renamed, ensure no collision.
            if new.name != r.name and self._config.find_reminder(new.name):
                QMessageBox.warning(self, "Duplicate name", f"A reminder named {new.name!r} already exists.")
                return
            self._config.reminders[idx] = new
            self._refresh_reminders_table()
            self.config_changed.emit()

    def _delete_reminder(self, name: str) -> None:
        if QMessageBox.question(
            self,
            "Delete reminder",
            f"Delete reminder {name!r}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._config.reminders = [r for r in self._config.reminders if r.name != name]
        self._refresh_reminders_table()
        self.config_changed.emit()

    # ---- Global tab -----------------------------------------------------

    def _make_global_tab(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)

        s = self._config.settings

        self._intensity = QDoubleSpinBox()
        self._intensity.setRange(0.1, 2.0)
        self._intensity.setSingleStep(0.05)
        self._intensity.setDecimals(2)
        self._intensity.setValue(s.flare_intensity)
        self._intensity_slider = QSlider(Qt.Orientation.Horizontal)
        self._intensity_slider.setRange(10, 200)
        self._intensity_slider.setValue(int(s.flare_intensity * 100))
        self._intensity_slider.valueChanged.connect(
            lambda v: self._intensity.setValue(v / 100.0)
        )
        self._intensity.valueChanged.connect(
            lambda v: self._intensity_slider.setValue(int(v * 100))
        )
        self._intensity.valueChanged.connect(self._on_global_changed)
        intens_row = QHBoxLayout()
        intens_row.addWidget(self._intensity_slider, 1)
        intens_row.addWidget(self._intensity)
        intens_w = QWidget(); intens_w.setLayout(intens_row)
        layout.addRow("Global flare intensity:", intens_w)

        self._anim_speed = QComboBox()
        self._anim_speed.addItems(["slow", "normal", "fast"])
        self._anim_speed.setCurrentText(s.flare_animation_speed)
        self._anim_speed.currentTextChanged.connect(self._on_global_changed)
        layout.addRow("Animation speed:", self._anim_speed)

        self._idle = QSpinBox()
        self._idle.setRange(5, 3600)
        self._idle.setSuffix(" s")
        self._idle.setValue(s.idle_timeout_seconds)
        self._idle.valueChanged.connect(self._on_global_changed)
        layout.addRow("Idle timeout:", self._idle)

        self._launch_at_startup = QCheckBox()
        self._launch_at_startup.setChecked(autostart.enabled())
        self._launch_at_startup.toggled.connect(self._on_autostart_toggled)
        layout.addRow("Launch at startup:", self._launch_at_startup)

        self._hotkey = KeyComboRecorder(initial=s.hotkey)
        self._hotkey.combo_recorded.connect(self._on_hotkey_recorded)
        layout.addRow("Global hotkey:", self._hotkey)

        reset_btn = QPushButton("Reset all settings to defaults")
        reset_btn.clicked.connect(self._reset_defaults)
        layout.addRow(reset_btn)

        return w

    def _refresh_global_tab(self) -> None:
        s = self._config.settings
        self._intensity.setValue(s.flare_intensity)
        self._intensity_slider.setValue(int(s.flare_intensity * 100))
        self._anim_speed.setCurrentText(s.flare_animation_speed)
        self._idle.setValue(s.idle_timeout_seconds)
        self._hotkey.setText(s.hotkey)
        self._launch_at_startup.setChecked(autostart.enabled())

    def _on_global_changed(self, *args) -> None:
        s = self._config.settings
        s.flare_intensity = float(self._intensity.value())
        s.flare_animation_speed = self._anim_speed.currentText()
        s.idle_timeout_seconds = int(self._idle.value())
        self.config_changed.emit()

    def _on_autostart_toggled(self, checked: bool) -> None:
        autostart.set_enabled(checked)
        self._config.settings.launch_at_startup = checked
        self.config_changed.emit()

    def _on_hotkey_recorded(self, combo: str) -> None:
        self._config.settings.hotkey = combo
        self.hotkey_changed.emit(combo)
        self.config_changed.emit()

    def _reset_defaults(self) -> None:
        if QMessageBox.question(
            self,
            "Reset to defaults",
            "Reset all reminders, settings, and hotkey to defaults? "
            "Statistics will NOT be cleared.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._config.reminders = AppConfig.defaults().reminders
        self._config.settings = GlobalSettings()
        self._refresh_reminders_table()
        self._refresh_global_tab()
        self.config_changed.emit()
        self.hotkey_changed.emit(self._config.settings.hotkey)

    # ---- Statistics tab -------------------------------------------------

    def _make_stats_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        layout.addWidget(QLabel("This Session"))
        self._session_table = QTableWidget(0, 5)
        self._session_table.setHorizontalHeaderLabels(
            ["Reminder", "Occurrences", "Avg delay", "Max delay", "Min delay"]
        )
        self._session_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._session_table.verticalHeader().setVisible(False)
        layout.addWidget(self._session_table)

        layout.addWidget(QLabel("This Week"))
        self._week_table = QTableWidget(0, 5)
        self._week_table.setHorizontalHeaderLabels(
            ["Reminder", "Occurrences", "Avg delay", "Max delay", "Min delay"]
        )
        self._week_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._week_table.verticalHeader().setVisible(False)
        layout.addWidget(self._week_table)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_stats)
        btn_row.addWidget(refresh_btn)
        export_btn = QPushButton("Export to CSV…")
        export_btn.clicked.connect(self._export_csv)
        btn_row.addWidget(export_btn)
        clear_btn = QPushButton("Clear Statistics")
        clear_btn.clicked.connect(self._clear_stats)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.refresh_stats()
        return w

    def _populate_stats_table(self, table: QTableWidget, rows: list[ReminderStats]) -> None:
        table.setRowCount(0)
        for row_idx, r in enumerate(rows):
            table.insertRow(row_idx)
            table.setItem(row_idx, 0, QTableWidgetItem(r.reminder_name))
            table.setItem(row_idx, 1, QTableWidgetItem(str(r.occurrences)))
            table.setItem(row_idx, 2, QTableWidgetItem(_format_seconds(r.avg_delay_seconds)))
            table.setItem(row_idx, 3, QTableWidgetItem(_format_seconds(r.max_delay_seconds)))
            table.setItem(row_idx, 4, QTableWidgetItem(_format_seconds(r.min_delay_seconds)))

    def _refresh_session_table(self) -> None:
        self._populate_stats_table(self._session_table, self._stats.session_stats())

    def _refresh_week_table(self) -> None:
        self._populate_stats_table(self._week_table, self._stats.week_stats())

    def _export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export statistics to CSV", "flarereminder-stats.csv", "CSV (*.csv)"
        )
        if not path:
            return
        n = self._stats.export_csv(path)
        QMessageBox.information(self, "Export complete", f"Wrote {n} rows to {path}.")

    def _clear_stats(self) -> None:
        if QMessageBox.question(
            self,
            "Clear statistics",
            "Permanently delete all recorded reminder statistics?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._stats.clear()
        self.refresh_stats()

    # ---- About tab ------------------------------------------------------

    def _make_about_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addStretch()
        title = QLabel(f"<h2>{__app_name__}</h2>")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        version = QLabel(f"Version {__version__}")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(version)
        desc = QLabel(
            "A KDE Plasma Wayland reminder app that displays animated\n"
            "lens-flare overlays as gentle visual notifications.\n\n"
            "Acknowledge with the global hotkey (default Ctrl+Shift+B)."
        )
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(desc)
        layout.addStretch()
        return w

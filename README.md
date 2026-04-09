# FlareReminder

A tray-resident reminder app for **KDE Plasma on Wayland** that displays
animated **lens-flare overlays** as visual notifications. Instead of a
traditional popup, each reminder lights up a transparent flare from the
upper-right corner of your screen. Multiple simultaneous reminders blend
their colors additively into one combined flare. Acknowledge them all
with a global hotkey.

- **Settings window** (QTabWidget): Reminders, Global, Statistics, About
- **System tray icon** with Suspend submenu and CSV export
- **Input-passthrough fullscreen overlay** that stays above normal windows
  (and above fullscreen games if `layer-shell-qt` is installed — see
  "Known limitations" below)
- **Global hotkey** via KGlobalAccel (default `Ctrl+Shift+B`)
- **Idle / lock / sleep handling**: timers freeze automatically
- **Per-reminder SQLite stats** with CSV export

## Install

FlareReminder targets Arch Linux + KDE Plasma 6 + Wayland. On that
platform:

```sh
# System dependencies (almost certainly already installed on KDE)
sudo pacman -S python-gobject xdg-desktop-portal-kde

# Strongly recommended: ensures the overlay stacks above fullscreen games
sudo pacman -S layer-shell-qt
```

Then grab the prebuilt standalone binary from `dist/flarereminder` (see
"Build" below) and put it somewhere on your `PATH`.

### Build from source

```sh
cd Flare/
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./build.sh
# Resulting binary: dist/flarereminder  (~60 MB)
```

Or run from source without packaging:

```sh
source .venv/bin/activate
python -m flarereminder.main              # normal run
python -m flarereminder.main --debug      # verbose logging
python -m flarereminder.main --test-flare # render one frame to /tmp and exit
```

## First run

On first launch FlareReminder:

1. Writes defaults to `~/.config/flarereminder/config.json`
2. Creates the SQLite stats DB at `~/.local/share/flarereminder/stats.db`
3. Starts logging to `~/.local/share/flarereminder/flarereminder.log`
4. Shows a tray icon, opens no window. Left-click to open settings.

The two default reminders are:

| Name        | Color            | Interval |
|-------------|------------------|----------|
| Blink       | white            | 15 min   |
| Drink Water | cornflower blue  | 45 min   |

Acknowledge with the global hotkey `Ctrl+Shift+B` (configurable in
Settings → Global).

## Timer semantics

A reminder's next countdown begins at the moment of acknowledgement,
**not** at the moment of firing. If Blink is set to 15 min, fires at
`T+15`, and you ack it 6 minutes later at `T+21`, the next fire is at
`T+36` (21 + 15). Timers freeze when:

- The screen is locked (`org.freedesktop.ScreenSaver.ActiveChanged`)
- The system suspends (`org.freedesktop.login1.PrepareForSleep`)
- You've been idle longer than the configured threshold
- You picked "Suspend Flares" from the tray menu

All freezes are cumulative — if the screen is locked AND you're idle
AND you suspended from the tray, unlocking only resumes the lock reason.
Timers only restart when every reason is cleared.

## Known limitations

**Fullscreen game stacking.** PyQt6 cannot natively create a
`wlr-layer-shell` surface — Qt always creates `xdg_toplevel` surfaces.
FlareReminder deals with this via a three-strategy fallback chain:

1. **LayerShellQt** (preferred): if KDE's `layer-shell-qt` package is
   installed and `QT_WAYLAND_SHELL_INTEGRATION=layer-shell` is set, Qt's
   QPA plugin honors `layershell.*` QSurface properties and creates a
   proper layer-shell overlay surface. In this mode the flare stacks
   above fullscreen Steam/Proton games.
2. **KWin scripting**: we register a one-shot KWin JavaScript via
   `org.kde.KWin/Scripting` that finds our overlay window and forces
   `keepAbove`, `skipTaskbar`, `skipPager`, `noBorder`. Works for
   normal windows; fullscreen games with exclusive fullscreen will
   still cover the flare.
3. **Plain stays-on-top**: fallback if neither of the above is available.

The strategy actually used is logged at startup. To check:

```sh
grep "Overlay stacking strategy" ~/.local/share/flarereminder/flarereminder.log
```

**Global hotkey fallback.** The KGlobalAccel path works on every modern
KDE Plasma 6 install. The `xdg-desktop-portal` fallback is
intentionally minimal — the portal's `GlobalShortcuts` API requires a
full `CreateSession`/`BindShortcuts`/`Activated` handshake that isn't
worth implementing given how reliable KGlobalAccel is. If both fail
(logged as `strategy=none`), you can still acknowledge flares by
right-clicking the tray icon and picking "View This Session Stats" or
using a test flare action — or configure a different hotkey via
System Settings → Shortcuts manually.

**dasbus + PyGObject.** dasbus runs its event loop on GLib via
PyGObject (`import gi`). PyGObject isn't pip-installable without
GObject-Introspection dev headers, so it isn't listed in
`requirements.txt`. On KDE Plasma Arch installs it is always present as
the `python-gobject` system package. If it isn't, every D-Bus feature
(hotkey, idle, lock, sleep) fails gracefully and is logged as a warning
at startup — the app keeps running but reminders will not auto-pause.

## Files and paths

| Path                                                      | Purpose                  |
|-----------------------------------------------------------|--------------------------|
| `~/.config/flarereminder/config.json`                     | user configuration       |
| `~/.config/autostart/flarereminder.desktop`               | autostart entry (opt-in) |
| `~/.local/share/flarereminder/stats.db`                   | SQLite stats             |
| `~/.local/share/flarereminder/flarereminder.log`          | rotating log             |

## Development

```sh
source .venv/bin/activate
pip install pytest
QT_QPA_PLATFORM=offscreen python -m pytest tests/ -v
```

Module layout:

```
flarereminder/
├── main.py              # entry point; wires every module together
├── config.py            # dataclasses + JSON persistence
├── stats.py             # SQLite + CSV export
├── flare_renderer.py    # pure QPainter flare rendering
├── overlay.py           # OverlayWindow with animation phase machine
├── layer_shell.py       # LayerShellQt / KWin-script / keep-above fallback
├── scheduler.py         # timers + idle/lock/sleep/suspend pause logic
├── hotkey.py            # KGlobalAccel + portal + KeyComboRecorder
├── tray.py              # QSystemTrayIcon wrapper
├── settings_window.py   # 4-tab QTabWidget UI
├── autostart.py         # ~/.config/autostart/*.desktop writer
├── logging_setup.py     # console + rotating file logger
└── resources/
    ├── tray_icon.svg
    └── tray_icon_suspended.svg
```

## Manual QA checklist (run on real KDE Plasma Wayland)

- [ ] Tray icon appears after launch
- [ ] Left-click tray opens settings
- [ ] Reminders tab → "Test Flare" on Blink → white flare animates from upper-right
- [ ] Press `Ctrl+Shift+B` → flare fades out in ~0.4 s
- [ ] Set Blink to 1 min, wait — real flare fires, stats row appears
- [ ] Both reminders at 1 min — combined flare is a blue-tinted white
- [ ] Tray → Suspend Flares → 1 hour — badge appears, timers freeze
- [ ] Lock screen (`Super+L`), unlock — verify "Scheduler resume(lock)" in log
- [ ] Suspend laptop, resume — verify "Scheduler resume(sleep)" in log
- [ ] Fullscreen game (Steam Big Picture) — flare visible if `layer-shell-qt` installed
- [ ] Settings → Statistics → Export CSV → open in spreadsheet
- [ ] Settings → Global → Launch at startup → check `~/.config/autostart/flarereminder.desktop`

## License

No license specified. Treat as private/internal.

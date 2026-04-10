#!/usr/bin/env bash
# Build FlareReminder into a single standalone binary with PyInstaller.
#
# Usage:
#   ./build.sh          # normal build
#   ./build.sh clean    # rm build/ and dist/ first
#
# Runtime requirements on the target Arch Linux machine:
#   sudo pacman -S layer-shell-qt xdg-desktop-portal-kde
#
# The resulting binary is dist/flarereminder.

set -euo pipefail
cd "$(dirname "$0")"

if [[ "${1:-}" == "clean" ]]; then
    echo ">> cleaning build/ dist/ *.egg-info"
    rm -rf build dist flarereminder.egg-info
fi

# Prefer the project venv if present.
if [[ -d .venv ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

# Ensure all Python dependencies are installed (dasbus -> jeepney migration
# means deps may have changed since the last pip install).
echo ">> pip install -r requirements.txt"
pip install -q -r requirements.txt

if ! command -v pyinstaller >/dev/null 2>&1; then
    echo "!! pyinstaller not found — installing into current environment"
    pip install pyinstaller
fi

echo ">> pyinstaller flarereminder.spec"
pyinstaller --noconfirm --clean flarereminder.spec

BIN=dist/flarereminder
if [[ ! -x "$BIN" ]]; then
    echo "!! build failed: $BIN not found"
    exit 1
fi

SIZE=$(du -h "$BIN" | cut -f1)
echo
echo ">> built $BIN ($SIZE)"
file "$BIN"
echo
echo "Run it with:  ./$BIN"
echo "Or with verbose logging:  ./$BIN --debug"

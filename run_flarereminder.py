"""Top-level launcher used as PyInstaller's entry point.

Running ``flarereminder/main.py`` directly breaks the relative imports in
that module (``from . import ...``). This shim imports the package
properly and delegates to ``flarereminder.main.main``.
"""

from flarereminder.main import main

if __name__ == "__main__":
    raise SystemExit(main())

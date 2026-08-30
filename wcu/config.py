from pathlib import Path

# The installed package directory. Everything the server shells out to
# (screencast.py, frames.py) and everything it installs (the bundled
# gnome-shell extension) lives inside the package, so a wheel carries them
# and no checkout is required at runtime.
PKG = Path(__file__).resolve().parent

# Where the bundled gnome-shell extension ships. `wcu-setup` copies from here.
EXTENSION_DIR = PKG / "extension"

# Evdev keycodes. Imported from desktop.py so there is one table, not two.
try:
    from .desktop import KEYS, MODIFIERS  # type: ignore
except Exception:  # pragma: no cover - desktop.py sits next to this file
    KEYS, MODIFIERS = {}, set()

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

# Evdev keycodes. Imported from desktop.py so there is one table, not two.
try:
    from desktop import KEYS, MODIFIERS  # type: ignore
except Exception:  # pragma: no cover - desktop.py sits next to this file
    KEYS, MODIFIERS = {}, set()

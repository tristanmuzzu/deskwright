"""Deskwright: an agent uses a real GNOME/Wayland desktop.

The only thing that happens at import is the environment shim below.
"""
from __future__ import annotations

import os

# Every setting was `WCU_*` before the 0.1.0 rename, and the names live in
# shell profiles and MCP client configs that this package cannot edit. So a
# `WCU_*` variable is honoured when its `DESKWRIGHT_*` counterpart is unset,
# once, here, rather than at each of the dozen `os.environ.get` sites.
# Deprecated: it goes away in 0.2.0.
_LEGACY_PREFIX = "WCU_"
_PREFIX = "DESKWRIGHT_"


def _adopt_legacy_env(environ: dict | None = None) -> list[str]:
    """Copy any WCU_* variable onto its DESKWRIGHT_* name. Returns what moved."""
    env = os.environ if environ is None else environ
    moved = []
    for key in [k for k in env if k.startswith(_LEGACY_PREFIX)]:
        new = _PREFIX + key[len(_LEGACY_PREFIX):]
        if new not in env:
            env[new] = env[key]
            moved.append(f"{key} -> {new}")
    return moved


_adopt_legacy_env()

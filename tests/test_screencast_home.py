"""screencast finds its window helper in the real home, not a rewritten $HOME.

A private desktop session points HOME at its own directory; recording a named
window there failed with ".../codex-desktops/<id>/.claude/bin/windows is missing".
"""
import importlib
import os
import pwd
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
pytest.importorskip("gi")


def test_window_helper_ignores_rewritten_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from deskwright import screencast
    screencast = importlib.reload(screencast)
    real = pwd.getpwuid(os.getuid()).pw_dir
    expected = os.path.join(real, ".claude/bin/windows")
    assert expected == screencast.WINDOWS_HELPER
    assert not screencast.WINDOWS_HELPER.startswith(str(tmp_path))

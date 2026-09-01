#!/usr/bin/env python3
"""What the 0.1.0 rename has to keep working.

The project was `wayland-computer-use` until the name turned out to be taken
on PyPI by an unrelated project. Renaming is cheap while nothing is published,
but two things outlive a rename on a machine that already had the old one:

  * gnome-shell keeps running the OLD extension until the next logout, so the
    old bus name has to stay reachable in the meantime;
  * `WCU_*` variables live in shell profiles and MCP client configs that this
    package cannot edit.

Both are deprecated and both go in 0.2.0. Until then they are tested, because
a compatibility path nobody exercises is just a comment.

    python3 -m pytest tests/test_rename_compat.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskwright import _adopt_legacy_env, shell

# ------------------------------------------------------------ environment

def test_a_legacy_variable_is_adopted():
    env = {"WCU_SESSION": "headless:work"}
    assert _adopt_legacy_env(env) == ["WCU_SESSION -> DESKWRIGHT_SESSION"]
    assert env["DESKWRIGHT_SESSION"] == "headless:work"
    assert env["WCU_SESSION"] == "headless:work"   # left alone, not moved


def test_the_new_name_wins_when_both_are_set():
    env = {"WCU_SESSION": "old", "DESKWRIGHT_SESSION": "new"}
    assert _adopt_legacy_env(env) == []
    assert env["DESKWRIGHT_SESSION"] == "new"


def test_every_setting_is_covered_not_just_the_session():
    env = {"WCU_INPUT_BACKEND": "portal", "WCU_JOURNAL_TEXT": "1",
           "WCU_HALT_FAIL_CLOSED": "1", "WCU_HEADLESS_MAX": "2"}
    _adopt_legacy_env(env)
    assert env["DESKWRIGHT_INPUT_BACKEND"] == "portal"
    assert env["DESKWRIGHT_JOURNAL_TEXT"] == "1"
    assert env["DESKWRIGHT_HALT_FAIL_CLOSED"] == "1"
    assert env["DESKWRIGHT_HEADLESS_MAX"] == "2"


def test_unrelated_variables_are_untouched():
    env = {"PATH": "/usr/bin", "WCUX": "not ours", "AWCU_THING": "nor this"}
    assert _adopt_legacy_env(env) == []
    assert set(env) == {"PATH", "WCUX", "AWCU_THING"}


# -------------------------------------------------------------- the bus

def _probe(monkeypatch, answering):
    """Only `answering` responds to introspection."""
    monkeypatch.setattr(shell, "_BUS_PROBED", False)
    monkeypatch.setattr(shell, "_EXTENSION_METHODS", None)
    monkeypatch.setattr(shell, "BUS_NAME", shell.OLD_BUS)
    monkeypatch.setattr(shell, "OBJ_PATH", shell.OLD_PATH)
    monkeypatch.setattr(shell, "EXTENSION_UUID", shell.OLD_UUID)
    monkeypatch.setattr(shell, "_introspect_methods",
                        lambda bus, path: {"Ping"} if bus == answering else set())
    shell._pick_bus()
    return shell.BUS_NAME, shell.OBJ_PATH, shell.EXTENSION_UUID


def test_the_renamed_extension_is_preferred(monkeypatch):
    assert _probe(monkeypatch, shell.NEW_BUS) == (
        shell.NEW_BUS, shell.NEW_PATH, shell.NEW_UUID)


def test_the_pre_rename_extension_still_works(monkeypatch):
    """A machine that installed the old one is running it until it logs out.
    A rename must not take the desktop away in the meantime."""
    assert _probe(monkeypatch, shell.WCU_BUS) == (
        shell.WCU_BUS, shell.WCU_PATH, shell.WCU_UUID)


def test_nothing_answering_leaves_the_legacy_default(monkeypatch):
    assert _probe(monkeypatch, "nothing.answers.this") == (
        shell.OLD_BUS, shell.OLD_PATH, shell.OLD_UUID)


def test_the_bus_is_probed_once_per_process(monkeypatch):
    calls = []
    monkeypatch.setattr(shell, "_BUS_PROBED", False)
    monkeypatch.setattr(shell, "_introspect_methods",
                        lambda bus, path: calls.append(bus) or {"Ping"})
    shell._pick_bus()
    shell._pick_bus()
    assert calls == [shell.NEW_BUS]


# ------------------------------------------------------- no stragglers

def test_the_old_name_is_gone_from_everything_but_the_compat_paths():
    """One grep, so a half-finished rename cannot ship."""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    tracked = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                             text=True, check=True).stdout.split()
    allowed = {"tests/test_rename_compat.py", "deskwright/shell.py",
               "deskwright/__init__.py", "CHANGELOG.md", "uv.lock"}
    stragglers = []
    for name in tracked:
        if name in allowed:
            continue
        path = root / name
        if not path.is_file() or path.suffix in {".compiled"}:
            continue
        try:
            body = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if "wayland-computer-use" in body or "org.wcu." in body:
            stragglers.append(name)
    assert not stragglers, f"old name still present in: {stragglers}"


def test_a_headless_session_accepts_either_extension(monkeypatch):
    """The headless shell loads whatever is in
    ~/.local/share/gnome-shell/extensions, which after a rename is the OLD
    extension until the user logs out. Waiting only for the new bus name made
    every headless start fail for 45 seconds on a machine that had been
    working a minute earlier."""
    from deskwright import headless

    seen = []

    class Proc:
        def __init__(self, xml): self.stdout = xml

    def fake_run(cmd, **kw):
        bus = cmd[cmd.index("--dest") + 1]
        seen.append(bus)
        return Proc("<node><method name='Ping'/></node>"
                    if bus == shell.WCU_BUS else "")

    monkeypatch.setattr(headless.subprocess, "run", fake_run)
    assert headless._extension_answering("unix:path=/tmp/nope") is True
    assert shell.NEW_BUS in seen and shell.WCU_BUS in seen


def test_a_headless_session_with_no_extension_says_no(monkeypatch):
    from deskwright import headless

    class Proc:
        stdout = ""

    monkeypatch.setattr(headless.subprocess, "run", lambda *a, **k: Proc())
    assert headless._extension_answering("unix:path=/tmp/nope") is False

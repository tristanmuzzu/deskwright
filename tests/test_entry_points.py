#!/usr/bin/env python3
"""The entry-point contract: which desktop a start actually lands on.

`wayland-computer-use` (wcu/cli.py) and the checkout's `./mcp_server.py` are
the same server by two names, and both go through `wcu/session.py`. What they
have to agree on is small and easy to get wrong: `--session headless:alpha`
must pin *alpha*, a cold session must not be paid for inside the MCP client's
initialize window, and a self-test must not quietly start some other desktop.
That last one was a real bug -- `pin_env(ensure())` with no name ran the
self-test against `default` while `WCU_SESSION` still said `alpha`.

Everything here is pure environment: the headless module is stubbed, so no
nested gnome-shell is ever started and these run anywhere, including CI.

    python3 -m pytest tests/test_entry_points.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wcu import session as sess
from wcu.errors import ToolError


@pytest.fixture
def headless(monkeypatch):
    """A stubbed headless module that records what it was asked for."""
    from wcu import headless as real

    calls: dict[str, list] = {"ensure": [], "pin": [], "status": []}
    running: set[str] = set()

    def status(name):
        calls["status"].append(name)
        return {"running": name in running, "name": name}

    def ensure(name=None):
        calls["ensure"].append(name)
        running.add(real.session_name(name))
        return {"running": True, "name": real.session_name(name)}

    def pin_env(st):
        calls["pin"].append(st.get("name"))
        return st

    monkeypatch.setattr(real, "status", status)
    monkeypatch.setattr(real, "ensure", ensure)
    monkeypatch.setattr(real, "pin_env", pin_env)
    monkeypatch.setattr(sess, "_RESOLVED", False)
    monkeypatch.delenv("WCU_HEADLESS_LAZY", raising=False)
    monkeypatch.delenv("WCU_SESSION", raising=False)
    calls["running"] = running
    return calls


# ------------------------------------------------------------ pinning

def test_unset_session_is_a_no_op(headless, monkeypatch):
    sess.resolve_session(as_server=True, argv=["wayland-computer-use"])
    assert headless["ensure"] == [] and headless["status"] == []
    assert "WCU_SESSION" not in __import__("os").environ


def test_primary_is_a_no_op(headless, monkeypatch):
    monkeypatch.setenv("WCU_SESSION", "primary")
    sess.resolve_session(as_server=True, argv=["wayland-computer-use"])
    assert headless["ensure"] == [] and headless["status"] == []


def test_flag_beats_environment(headless, monkeypatch):
    monkeypatch.setenv("WCU_SESSION", "primary")
    sess.resolve_session(as_server=True,
                         argv=["x", "--session", "headless:alpha"])
    import os
    assert os.environ["WCU_SESSION"] == "headless:alpha"


def test_bare_headless_normalises_to_the_default_name(headless, monkeypatch):
    monkeypatch.setenv("WCU_SESSION", "headless")
    sess.resolve_session(as_server=True, argv=["x"])
    import os
    assert os.environ["WCU_SESSION"] == "headless:default"


def test_unknown_session_kind_exits_two(headless, monkeypatch):
    monkeypatch.setenv("WCU_SESSION", "nonsense")
    with pytest.raises(SystemExit) as e:
        sess.resolve_session(as_server=True, argv=["x"])
    assert e.value.code == 2


def test_invalid_name_exits_two(headless, monkeypatch):
    monkeypatch.setenv("WCU_SESSION", "headless:Not A Name")
    with pytest.raises(SystemExit) as e:
        sess.resolve_session(as_server=True, argv=["x"])
    assert e.value.code == 2


# --------------------------------------------------- server vs importer

def test_a_cold_session_is_deferred_for_the_server(headless, monkeypatch):
    """A cold start costs 15-20s. Paying it here would spend it inside the
    MCP client's connect window, which killed the connection (2026-08-25)."""
    monkeypatch.setenv("WCU_SESSION", "headless:alpha")
    sess.resolve_session(as_server=True, argv=["x"])
    import os
    assert os.environ.get("WCU_HEADLESS_LAZY") == "1"
    assert headless["ensure"] == []          # nothing started yet
    assert headless["pin"] == []


def test_a_warm_session_is_pinned_immediately(headless, monkeypatch):
    headless["running"].add("alpha")
    monkeypatch.setenv("WCU_SESSION", "headless:alpha")
    sess.resolve_session(as_server=True, argv=["x"])
    import os
    assert "WCU_HEADLESS_LAZY" not in os.environ
    assert headless["pin"] == ["alpha"]


def test_an_importer_gets_the_session_ready(headless, monkeypatch):
    """A test or script calling tool functions directly bypasses serve(),
    so there is no first tool call to pay a deferred start."""
    monkeypatch.setenv("WCU_SESSION", "headless:alpha")
    sess.resolve_session(as_server=False, argv=["x"])
    assert headless["ensure"] == ["alpha"]
    assert headless["pin"] == ["alpha"]


def test_resolving_twice_costs_nothing(headless, monkeypatch):
    """`./mcp_server.py` resolves at import and hands off to wcu.cli.main."""
    headless["running"].add("alpha")
    monkeypatch.setenv("WCU_SESSION", "headless:alpha")
    sess.resolve_session(as_server=True, argv=["x"])
    sess.resolve_session(as_server=True, argv=["x"])
    assert headless["status"] == ["alpha"]   # probed once, not twice


# ------------------------------------------------ the deferred start

def test_deferred_start_uses_the_name_that_was_asked_for(headless, monkeypatch):
    """The bug this file exists for: `--self-test` on a cold `headless:alpha`
    used to call `ensure()` with no name, start `default`, and leave alpha
    cold while WCU_SESSION still advertised it."""
    monkeypatch.setenv("WCU_SESSION", "headless:alpha")
    sess.resolve_session(as_server=True, argv=["x", "--self-test"])
    assert sess.start_deferred() is True
    assert headless["ensure"] == ["alpha"]
    assert headless["pin"] == ["alpha"]


def test_deferred_start_is_a_no_op_without_the_marker(headless):
    assert sess.start_deferred() is False
    assert headless["ensure"] == []


def test_pinned_name_reads_the_environment(headless, monkeypatch):
    assert sess.pinned_name() is None
    monkeypatch.setenv("WCU_SESSION", "primary")
    assert sess.pinned_name() is None
    monkeypatch.setenv("WCU_SESSION", "headless")
    assert sess.pinned_name() == "default"
    monkeypatch.setenv("WCU_SESSION", "headless:beta")
    assert sess.pinned_name() == "beta"
    monkeypatch.setenv("WCU_SESSION", "headless:NOPE NOPE")
    with pytest.raises(ToolError):
        sess.pinned_name()


# ------------------------------------------------------------ the CLI

def test_cli_self_test_starts_the_named_session_then_runs(headless, monkeypatch):
    from wcu import cli
    ran: list[str] = []
    monkeypatch.setitem(sys.modules, "wcu.server", _FakeServer(ran))
    monkeypatch.setenv("WCU_SESSION", "headless:alpha")
    rc = cli.main(["wayland-computer-use", "--self-test"])
    assert rc == 0
    assert ran == ["self_test"]
    assert headless["ensure"] == ["alpha"]


def test_cli_without_self_test_serves(headless, monkeypatch):
    from wcu import cli
    ran: list[str] = []
    monkeypatch.setitem(sys.modules, "wcu.server", _FakeServer(ran))
    rc = cli.main(["wayland-computer-use"])
    assert rc == 0
    assert ran == ["serve"]


class _FakeServer:
    """Stands in for wcu.server so no real stdio loop or desktop is touched."""

    def __init__(self, ran: list[str]) -> None:
        self._ran = ran

    def self_test(self) -> int:
        self._ran.append("self_test")
        return 0

    def serve(self) -> int:
        self._ran.append("serve")
        return 0

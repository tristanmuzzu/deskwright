"""The session bus is resolved, not assumed from the environment.

Regression contract for the sanitized-environment host: MCP clients that
spawn this server as a stdio subprocess strip everything but a safe
baseline (PATH/HOME/USER/LANG/XDG_*), so DBUS_SESSION_BUS_ADDRESS is absent
even on the user's own desktop. GLib still reaches the systemd user bus at
$XDG_RUNTIME_DIR/bus in that case; the guard in shell._gdbus refused before
even trying, and execution.session_key() handed two servers on one desktop
different lease keys.
"""
import os
from unittest import mock

import pytest

from deskwright.errors import ToolError
from deskwright.execution import session_bus_address, session_key


def _sanitize(monkeypatch, runtime_dir):
    """Reproduce a sanitized MCP-host environment in one line."""
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime_dir)


def test_explicit_address_wins(monkeypatch, tmp_path):
    _sanitize(monkeypatch, str(tmp_path))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/custom/bus")
    assert session_bus_address() == "unix:path=/custom/bus"


def test_fallback_to_systemd_user_bus(monkeypatch, tmp_path):
    (tmp_path / "bus").write_text("")  # the socket exists, GLib would use it
    _sanitize(monkeypatch, str(tmp_path))
    assert session_bus_address() == f"unix:path={tmp_path}/bus"


def test_none_when_no_bus_exists(monkeypatch, tmp_path):
    _sanitize(monkeypatch, str(tmp_path))  # runtime dir, but no bus socket
    assert session_bus_address() is None


def test_guard_tries_fallback_bus_before_refusing(monkeypatch, tmp_path):
    (tmp_path / "bus").write_text("")
    _sanitize(monkeypatch, str(tmp_path))
    fake_proc = mock.Mock(returncode=0, stdout="(true,)")
    fake = mock.Mock(return_value=fake_proc)
    monkeypatch.setattr("deskwright.shell.subprocess.run", fake)
    import deskwright.shell as shell
    out = shell._gdbus("ListWindows")  # must not raise extension_unavailable
    assert out == "(true,)"
    # and the subprocess environment now carries the resolved address, so
    # gdbus/gnome-extensions children inherit the bus GLib picked
    assert os.environ["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={tmp_path}/bus"


def test_guard_still_refuses_genuinely_busless_environments(monkeypatch, tmp_path):
    _sanitize(monkeypatch, str(tmp_path))  # no `bus` socket created
    import deskwright.shell as shell
    with pytest.raises(ToolError) as err:
        shell._gdbus("ListWindows")
    assert err.value.code == "extension_unavailable"
    assert "bare ssh login" in str(err.value)


def test_session_key_is_stable_across_connection_routes(monkeypatch, tmp_path):
    """Two servers on ONE desktop must hash to the same lease key."""
    (tmp_path / "bus").write_text("")
    _sanitize(monkeypatch, str(tmp_path))
    fallback_key = session_key()
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={tmp_path}/bus")
    explicit_key = session_key()
    assert fallback_key == explicit_key

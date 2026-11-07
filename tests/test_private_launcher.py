"""The private launcher's desktop names must fit the Wayland socket path.

The socket lives at $XDG_RUNTIME_DIR/deskwright-headless-<name>/wayland-deskwright-<name>
and a UNIX socket path holds at most 107 bytes. A `claude-` name with the same
20-character hash as `codex-` was one byte over and gnome-shell aborted
("Failed to create_socket") before the desktop came up.
"""
import importlib.util
from pathlib import Path

from deskwright.headless import DISPLAY_PREFIX, RUNTIME_PREFIX

SCRIPT = Path(__file__).resolve().parents[1] / "skills/deskwright-codex/scripts/private_server.py"
spec = importlib.util.spec_from_file_location("private_server", SCRIPT)
private_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(private_server)


def socket_path(name):
    return f"/run/user/1000/{RUNTIME_PREFIX}-{name}/{DISPLAY_PREFIX}-{name}"


def test_names_fit_the_socket_limit_for_each_host():
    for prefix in ("codex", "claude"):
        plan = private_server.allocation(None, "c" * 32, Path("/tmp/x"), prefix)
        assert plan["name"].startswith(prefix + "-")
        assert len(socket_path(plan["name"])) <= 107, plan["name"]


def test_codex_names_are_unchanged():
    plan = private_server.allocation("task-1", "conn", Path("/tmp/x"))
    assert len(plan["name"]) == len("codex-") + 20

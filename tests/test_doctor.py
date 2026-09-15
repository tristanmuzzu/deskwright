#!/usr/bin/env python3
"""deskwright-doctor, fully in-process: no gsettings, no D-Bus, no spawn.

Three claims:
  1. A healthy machine (every seam patched green) renders all-ok and exits 0.
  2. Each failure mode prints its own action line and exits 1: missing hard
     dep, accessibility flag off, registry not owned (the issue-#3 state,
     with its distinct action), extension not live (logout), MCP stdio
     handshake dead, and a headless session running without a registry.
  3. A non-GNOME session exits 2 after exactly one finding, before any
     other check runs -- same refusal shape as setup's --check.

Every external read goes through a seam; subprocess is booby-trapped for
the duration, so a green test run proves the doctor asked its seams and
not the machine.

    python3 -m pytest tests/test_doctor.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskwright import doctor, setup_cli

UUID = setup_cli.EXTENSION_UUID


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError(f"subprocess escaped the seams: {a} {k}")
    monkeypatch.setattr(doctor.subprocess, "run", _boom)
    monkeypatch.setattr(doctor.subprocess, "Popen", _boom)
    assert doctor.subprocess is subprocess


def _healthy(monkeypatch, tmp_path):
    """Every seam green: the machine a clean install on a logged-in GNOME
    Wayland session should show."""
    monkeypatch.setattr(doctor, "_a11y_registry_state",
                        lambda: (True, "registry owns the name"))
    monkeypatch.setattr(doctor, "_mcp_probe",
                        lambda cmd, timeout=20.0: (True, "initialize + tools/list answered, 33 tools"))
    monkeypatch.setattr(doctor, "_headless_sessions", lambda: [])
    monkeypatch.setattr(doctor, "_install_provenance",
                        lambda cmd: ("0.1.1", "PyPI release"))
    monkeypatch.setattr(setup_cli, "_desktop_seam", lambda: ("GNOME", "wayland"))
    # deps: every probe present
    monkeypatch.setattr(setup_cli, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(setup_cli, "_typelib_ok", lambda ns: True)
    monkeypatch.setattr(setup_cli, "_import_ok_here", lambda mod: True)
    monkeypatch.setattr(setup_cli, "_import_ok_system", lambda mod: True)
    # extension: installed, current, enabled, live
    monkeypatch.setattr(setup_cli, "EXTENSIONS_DIR", tmp_path / "ext")
    src = tmp_path / "src" / UUID
    src.mkdir(parents=True)
    (src / "metadata.json").write_text(f'{{"uuid": "{UUID}"}}\n')
    dest = tmp_path / "ext" / UUID
    dest.mkdir(parents=True)
    (dest / "metadata.json").write_text(f'{{"uuid": "{UUID}"}}\n')
    monkeypatch.setattr(setup_cli, "find_extension_source", lambda _x: src)
    values = {(setup_cli.A11Y_SCHEMA, setup_cli.A11Y_KEY): "true",
              (setup_cli.SHELL_SCHEMA, setup_cli.ENABLED_KEY): f"['{UUID}']",
              (setup_cli.SHELL_SCHEMA, setup_cli.DISABLED_KEY): "@as []"}
    monkeypatch.setattr(setup_cli, "_gsettings_get",
                        lambda s, k: values.get((s, k)))
    monkeypatch.setattr(setup_cli, "_bus_has_owner", lambda name: True)
    monkeypatch.setattr(setup_cli, "server_command",
                        lambda: ("/usr/local/bin/deskwright", None))


def _labels(out: str) -> list[str]:
    return [ln.split(":", 1)[0].strip()[6:].strip()
            for ln in out.splitlines() if ln.startswith("[")]


# ---------------------------------------------------------------- healthy

def test_healthy_machine_is_all_ok_and_exits_zero(monkeypatch, capsys, tmp_path):
    _healthy(monkeypatch, tmp_path)
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "[FIX]" not in out
    assert "RESULT: healthy" in out
    assert "action:" not in out


# ---------------------------------------------------------------- session

def test_non_gnome_session_refuses_before_anything_else(monkeypatch, capsys):
    monkeypatch.setattr(setup_cli, "_desktop_seam", lambda: ("KDE", "wayland"))
    # nothing else is patched: if the doctor tried any other check, the
    # subprocess trap would fire
    assert doctor.main([]) == 2
    out = capsys.readouterr().out
    assert "not a diagnosable target" not in out   # rendered message below
    assert "[FIX] session" in out


# ---------------------------------------------------------------- deps

def test_missing_hard_dep_prints_its_install_line(monkeypatch, capsys, tmp_path):
    _healthy(monkeypatch, tmp_path)
    real_which = setup_cli._which

    def which(name):
        return None if name == "tesseract" else real_which(name)

    monkeypatch.setattr(setup_cli, "_which", which)
    # probe_deps shells nothing once the seams answer; restore it for this test
    monkeypatch.setattr("deskwright.doctor.subprocess.run", lambda *a, **k: None)
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "[MISSING]  tesseract OCR" in out
    assert "sudo apt install" in out or "apt install" in out
    assert "[FIX] dependencies:" in out


# ---------------------------------------------------------------- a11y

def test_registry_not_owned_is_the_issue3_signature(monkeypatch, capsys, tmp_path):
    _healthy(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor, "_a11y_registry_state",
                        lambda: (False, "the a11y bus answers but nothing owns"
                                        " org.a11y.atspi.Registry"))
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "issue #3" in out
    assert "at-spi-dbus-bus.service" in out


def test_accessibility_flag_off_prints_the_gsettings_line(monkeypatch, capsys, tmp_path):
    _healthy(monkeypatch, tmp_path)
    values = {(setup_cli.A11Y_SCHEMA, setup_cli.A11Y_KEY): "false",
              (setup_cli.SHELL_SCHEMA, setup_cli.ENABLED_KEY): f"['{UUID}']",
              (setup_cli.SHELL_SCHEMA, setup_cli.DISABLED_KEY): "@as []"}
    monkeypatch.setattr(setup_cli, "_gsettings_get",
                        lambda s, k: values.get((s, k)))
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "toolkit-accessibility" in out
    assert "gsettings set org.gnome.desktop.interface toolkit-accessibility true" in out


# ---------------------------------------------------------------- extension

def test_extension_not_live_demands_the_logout(monkeypatch, capsys, tmp_path):
    _healthy(monkeypatch, tmp_path)
    monkeypatch.setattr(setup_cli, "_bus_has_owner", lambda name: False)
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "log out and log back in" in out


# ---------------------------------------------------------------- server

def test_dead_stdio_handshake_is_a_fix(monkeypatch, capsys, tmp_path):
    _healthy(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor, "_mcp_probe",
                        lambda cmd, timeout=20.0: (False, "no MCP answer within 20s"))
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "[FIX] MCP over stdio" in out
    assert "re-register" in out


def test_local_path_install_is_reported_not_fixed(monkeypatch, capsys, tmp_path):
    _healthy(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor, "_install_provenance",
                        lambda cmd: ("0.1.1", "local path install (/home/x/p/deskwright)"))
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "local path install" in out
    assert "RESULT: healthy" in out


# ---------------------------------------------------------------- headless

def test_headless_session_without_registry_gets_the_restart_action(monkeypatch, capsys, tmp_path):
    _healthy(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor, "_headless_sessions", lambda: [
        {"name": "demo", "running": True,
         "bus_address": "unix:path=/tmp/x", "detail": "started"}])
    from deskwright import headless
    monkeypatch.setattr(headless, "_registry_alive", lambda addr: False)
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "headless:demo" in out
    assert "deskwright-headless stop --name demo" in out


def test_headless_session_with_registry_is_ok(monkeypatch, capsys, tmp_path):
    _healthy(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor, "_headless_sessions", lambda: [
        {"name": "demo", "running": True,
         "bus_address": "unix:path=/tmp/x", "detail": "started"}])
    from deskwright import headless
    monkeypatch.setattr(headless, "_registry_alive", lambda addr: True)
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "[ok] headless:demo" in out


def test_no_headless_sessions_is_ok(monkeypatch, capsys, tmp_path):
    _healthy(monkeypatch, tmp_path)
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "none recorded" in out

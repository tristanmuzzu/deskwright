#!/usr/bin/env python3
"""wcu-setup, fully in-process: no gsettings, no D-Bus, no subprocess.

Three claims:
  1. Distro-family detection reads synthetic /etc/os-release content
     correctly, including the ID_LIKE fallback, and unknown distros get apt
     phrasing.
  2. `--check` builds its plan purely from the probe seams (_which,
     _py_import_ok, _gsettings_get, _bus_has_owner): with everything
     monkeypatched present it exits 0; with a hard dependency missing it
     exits 1 and prints that dependency's exact install line. No real
     gsettings/gdbus call happens -- subprocess.run is booby-trapped for the
     duration.
  3. The GVariant enabled-extensions helpers round-trip and are idempotent
     (append when absent, None when present; the `@as []` empty form parses).

    python3 -m pytest tests/test_setup_cli.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wcu import setup_cli

UUID = setup_cli.EXTENSION_UUID


# ------------------------------------------------------------- distro

OS_RELEASE_UBUNTU = 'NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="26.04"\n'
OS_RELEASE_DEBIAN = 'ID=debian\nNAME="Debian GNU/Linux"\n'
OS_RELEASE_FEDORA = 'NAME="Fedora Linux"\nID=fedora\n'
OS_RELEASE_ROCKY = 'ID="rocky"\nID_LIKE="rhel centos fedora"\n'
OS_RELEASE_MANJARO = 'ID=manjaro\nID_LIKE=arch\n'
OS_RELEASE_STEAMOS = 'ID=steamos\nID_LIKE=arch\n'   # ID unknown, ID_LIKE known
OS_RELEASE_NIXOS = 'ID=nixos\nNAME=NixOS\n'


@pytest.mark.parametrize("text,family", [
    (OS_RELEASE_UBUNTU, "debian"),
    (OS_RELEASE_DEBIAN, "debian"),
    (OS_RELEASE_FEDORA, "fedora"),
    (OS_RELEASE_ROCKY, "fedora"),
    (OS_RELEASE_MANJARO, "arch"),
    (OS_RELEASE_STEAMOS, "arch"),
    (OS_RELEASE_NIXOS, "unknown"),
    ("", "unknown"),
])
def test_detect_family(text, family):
    assert setup_cli.detect_family(text) == family


def test_install_line_per_family():
    pkgs = {"debian": "python3-gi", "fedora": "python3-gobject",
            "arch": "python-gobject"}
    assert setup_cli.install_line("debian", pkgs) == \
        "sudo apt install -y python3-gi"
    assert setup_cli.install_line("fedora", pkgs) == \
        "sudo dnf install -y python3-gobject"
    assert setup_cli.install_line("arch", pkgs) == \
        "sudo pacman -S --needed python-gobject"


def test_install_line_unknown_defaults_to_apt():
    pkgs = {"debian": "wl-clipboard", "fedora": "wl-clipboard",
            "arch": "wl-clipboard"}
    line = setup_cli.install_line("unknown", pkgs)
    assert line.startswith("sudo apt install -y wl-clipboard")
    assert "apt phrasing" in line


# ------------------------------------------------------- gvariant lists

def test_string_list_roundtrip():
    src = "['a@b', 'c@d']"
    assert setup_cli.parse_string_list(src) == ["a@b", "c@d"]
    assert setup_cli.format_string_list(["a@b", "c@d"]) == src


def test_empty_list_forms_parse():
    assert setup_cli.parse_string_list("@as []") == []
    assert setup_cli.parse_string_list("[]") == []


def test_append_uuid_idempotent():
    assert setup_cli.list_with_uuid("@as []", UUID) == f"['{UUID}']"
    assert setup_cli.list_with_uuid("['other@x']", UUID) == \
        f"['other@x', '{UUID}']"
    assert setup_cli.list_with_uuid(f"['other@x', '{UUID}']", UUID) is None


def test_remove_uuid():
    assert setup_cli.list_without_uuid(f"['{UUID}', 'other@x']", UUID) == \
        "['other@x']"
    assert setup_cli.list_without_uuid("['other@x']", UUID) is None


# ------------------------------------------------------------- --check

@pytest.fixture
def fake_repo(tmp_path):
    (tmp_path / "mcp_server.py").write_text("#!/usr/bin/env python3\n")
    ext = tmp_path / "extension" / UUID
    ext.mkdir(parents=True)
    (ext / "metadata.json").write_text(f'{{"uuid": "{UUID}"}}\n')
    return tmp_path


@pytest.fixture
def no_subprocess(monkeypatch):
    """Prove the check path never shells out once the seams are patched."""
    def _boom(*a, **k):
        raise AssertionError(f"subprocess escaped the seams: {a} {k}")
    monkeypatch.setattr(setup_cli.subprocess, "run", _boom)
    assert setup_cli.subprocess is subprocess  # the trap covers the real one


def _patch_probes(monkeypatch, tmp_path, *, missing_bins=(), missing_mods=(),
                  only_system_mods=(), a11y="true", enabled=None):
    # this machine has the extension really installed -- point the dest
    # elsewhere so the check reads the fake state, not the real one
    monkeypatch.setattr(setup_cli, "EXTENSIONS_DIR", tmp_path / "gnome-ext")
    monkeypatch.setattr(
        setup_cli, "_which",
        lambda name: None if name in missing_bins else f"/usr/bin/{name}")
    # Two interpreters, two seams: `_import_ok_here` is the one the console
    # script runs under, `_import_ok_system` is the distro python3 the
    # checkout's shebang and the plugin route use.
    monkeypatch.setattr(
        setup_cli, "_import_ok_here",
        lambda mod: mod not in missing_mods and mod not in only_system_mods)
    monkeypatch.setattr(
        setup_cli, "_import_ok_system",
        lambda mod: mod not in missing_mods)
    values = {
        (setup_cli.A11Y_SCHEMA, setup_cli.A11Y_KEY): a11y,
        (setup_cli.SHELL_SCHEMA, setup_cli.ENABLED_KEY):
            enabled if enabled is not None else "@as []",
        (setup_cli.SHELL_SCHEMA, setup_cli.DISABLED_KEY): "@as []",
    }
    monkeypatch.setattr(setup_cli, "_gsettings_get",
                        lambda schema, key: values.get((schema, key)))
    monkeypatch.setattr(setup_cli, "_bus_has_owner", lambda name: False)


def test_check_all_present_exits_zero(monkeypatch, capsys, fake_repo,
                                      tmp_path, no_subprocess):
    _patch_probes(monkeypatch, tmp_path)
    rc = setup_cli.run(check_only=True, repo_arg=str(fake_repo))
    out = capsys.readouterr().out
    assert rc == 0
    assert "read-only" in out
    assert "[MISSING]" not in out
    assert "all hard requirements present" in out
    # the plan still names every later step without doing it
    assert "claude mcp add wayland-computer-use --scope user" in out
    assert "--self-test" in out
    assert "LOG OUT" not in out          # check mode never reaches the banner


def test_check_missing_hard_dep_exits_nonzero(monkeypatch, capsys, fake_repo,
                                              tmp_path, no_subprocess):
    _patch_probes(monkeypatch, tmp_path,
                  missing_bins={"wl-paste"}, missing_mods={"gi"})
    rc = setup_cli.run(check_only=True, repo_arg=str(fake_repo))
    out = capsys.readouterr().out
    assert rc == 1
    assert "sudo apt install -y wl-clipboard" in out
    assert "sudo apt install -y python3-gi" in out
    assert "2 hard requirement(s) missing" in out


def test_check_missing_optional_dep_still_zero(monkeypatch, capsys, fake_repo,
                                               tmp_path, no_subprocess):
    _patch_probes(monkeypatch, tmp_path, missing_bins={"ydotool"})
    rc = setup_cli.run(check_only=True, repo_arg=str(fake_repo))
    out = capsys.readouterr().out
    assert rc == 0
    assert "[absent, optional]" in out
    assert "sudo apt install -y ydotool" in out


def test_check_reports_a11y_would_set(monkeypatch, capsys, fake_repo,
                                      tmp_path, no_subprocess):
    _patch_probes(monkeypatch, tmp_path, a11y="false")
    rc = setup_cli.run(check_only=True, repo_arg=str(fake_repo))
    out = capsys.readouterr().out
    assert rc == 0                        # gsettings state is not a dep
    assert "before: org.gnome.desktop.interface toolkit-accessibility = false" in out
    assert "would set it to true" in out
    assert "ALREADY RUNNING" in out


def test_check_extension_states(monkeypatch, capsys, fake_repo,
                                tmp_path, no_subprocess):
    _patch_probes(monkeypatch, tmp_path, enabled=f"['{UUID}']")
    rc = setup_cli.run(check_only=True, repo_arg=str(fake_repo))
    out = capsys.readouterr().out
    assert rc == 0
    assert "not installed" in out         # tmp repo, nothing under ~/.local
    assert "enabled: yes" in out


def test_no_argument_finds_the_extension_bundled_with_the_package(
        monkeypatch, capsys, tmp_path, no_subprocess):
    """The install story: `pipx install ... && wcu-setup`, no clone anywhere.

    The extension ships inside the `wcu` package, so with no --repo, no
    checkout and a foreign cwd, setup still knows what to copy.
    """
    _patch_probes(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)           # cwd has no checkout
    rc = setup_cli.run(check_only=True, repo_arg=None)
    out = capsys.readouterr().out
    assert rc == 0
    packaged = Path(setup_cli.__file__).resolve().parent / "extension" / UUID
    assert f"extension source: {packaged}" in out
    assert "NOT FOUND" not in out


def test_missing_extension_source_is_reported(monkeypatch, capsys, tmp_path,
                                              no_subprocess):
    _patch_probes(monkeypatch, tmp_path)
    monkeypatch.setattr(setup_cli, "find_extension_source",
                        lambda explicit: None)
    rc = setup_cli.run(check_only=True, repo_arg=None)
    out = capsys.readouterr().out
    assert rc == 1                        # nothing was installed; say so
    assert "extension source: NOT FOUND" in out
    assert "extension source not found" in out


def test_find_extension_source_default_is_the_packaged_copy():
    packaged = Path(setup_cli.__file__).resolve().parent / "extension" / UUID
    assert setup_cli.find_extension_source(None) == packaged
    assert (packaged / "metadata.json").is_file()


@pytest.mark.parametrize("layout", ["extension", "wcu/extension", "direct"])
def test_find_extension_source_accepts_explicit_overrides(tmp_path, layout):
    """--repo takes a checkout in either layout, or the extension dir itself."""
    ext = tmp_path / UUID if layout == "direct" else tmp_path / layout / UUID
    ext.mkdir(parents=True)
    (ext / "metadata.json").write_text(f'{{"uuid": "{UUID}"}}\n')
    arg = str(ext) if layout == "direct" else str(tmp_path)
    assert setup_cli.find_extension_source(arg) == ext.resolve()


def test_find_extension_source_rejects_a_path_with_no_extension(tmp_path):
    assert setup_cli.find_extension_source(str(tmp_path / "nowhere")) is None


def test_server_command_prefers_the_installed_console_script(monkeypatch):
    monkeypatch.setattr(setup_cli, "_which",
                        lambda n: "/usr/local/bin/wayland-computer-use"
                        if n == "wayland-computer-use" else None)
    assert setup_cli.server_command() == (
        "/usr/local/bin/wayland-computer-use", None)


def test_server_command_finds_the_script_beside_the_interpreter(monkeypatch, tmp_path):
    """A pipx install whose bin dir is not on PATH must still get a command
    that runs -- printing the bare name there hands the client an ENOENT."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python").write_text("")
    script = bindir / "wayland-computer-use"
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(setup_cli, "_which", lambda n: None)
    monkeypatch.setattr(setup_cli.sys, "executable", str(bindir / "python"))
    cmd, caveat = setup_cli.server_command()
    assert cmd == str(script)
    assert "PATH" in caveat


def test_server_command_falls_back_to_the_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_cli, "_which", lambda n: None)
    monkeypatch.setattr(setup_cli.sys, "executable", str(tmp_path / "python"))
    cmd, caveat = setup_cli.server_command()
    assert cmd.endswith("mcp_server.py")
    assert Path(cmd).is_file()
    assert caveat is None


def test_a_venv_without_system_site_packages_is_called_out(
        monkeypatch, capsys, tmp_path, no_subprocess):
    """The green check that used to be earned by the WRONG interpreter.

    `pipx install` without `--system-site-packages` leaves the venv unable to
    import gi while the distro python3 can. Every pointer and AT-SPI call
    then fails against a setup report that said everything was present.
    """
    _patch_probes(monkeypatch, tmp_path, only_system_mods=("gi",))
    rc = setup_cli.run(check_only=True, repo_arg=None)
    out = capsys.readouterr().out
    assert rc == 0                       # it IS present, for one of the routes
    assert "NOTE: the system python3 has 'gi'" in out
    assert "--system-site-packages" in out


def test_missing_extension_source_is_a_failure_not_a_quiet_success(
        monkeypatch, capsys, tmp_path, no_subprocess):
    _patch_probes(monkeypatch, tmp_path)
    monkeypatch.setattr(setup_cli, "find_extension_source",
                        lambda explicit: None)
    rc = setup_cli.run(check_only=True, repo_arg=None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "RESULT:" in out and "extension" in out

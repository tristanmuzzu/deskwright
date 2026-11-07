"""Live semantic workflow oracle. Run inside a private desktop_exec worker.

Uses an already launched benchmarks/semantic_demo.py and its disposable output
folder. No screenshot loop, input injection or application-state backdoor.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from deskwright.errors import ToolError


def run(desktop, window_id, output, samples=5):
    output = Path(output)
    expected = {
        "Project": "Northstar desktop trial", "Owner": "Demo team", "Location": "München",
        "Reference": "研究-042", "Task 1": "Prepare release notes", "Assignee 1": "Alex",
        "Due 1": "2026-10-12", "Task 2": "Check keyboard access", "Assignee 2": "Sam",
        "Due 2": "2026-10-13", "Task 3": "Verify saved package", "Assignee 3": "Robin",
        "Due 3": "2026-10-14", "Notes": "Café, Unicode, exact recovery; local files only.",
    }
    options = {"Include checklist": True, "Enable review": True, "Mark ready": True}
    measurements = []

    def until(predicate, seconds=3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            desktop.sleep(.02)
        raise AssertionError("application outcome did not arrive")

    for trial in range(samples):
        desktop.call("activate_window", target=window_id)
        start = time.monotonic()
        snapshot = desktop.call("ui_snapshot", window_id=window_id)
        assert snapshot["complete"], snapshot["limits_hit"]
        snapshot_ms = snapshot["elapsed_ms"]
        controls = {(n["role"], n["name"]): n["ref"] for n in snapshot["controls"]}

        def invoke(name, controls=controls):
            return desktop.call("ui_action", ref=controls["button", name], action="invoke")

        invoke("Clear form")
        for name in expected:
            desktop.call("ui_wait", ref=controls["text", name], text="")
        fill_start = time.monotonic()
        for name, value in expected.items():
            desktop.call("ui_action", ref=controls["text", name], action="set_text",
                         text=value, expected_text="")
        for name, checked in options.items():
            desktop.call("ui_action", ref=controls["check box", name], action="set_checked",
                         checked=checked)
        fill_seconds = time.monotonic() - fill_start
        # Retry-safe assignment must leave an already checked box unchanged.
        same = desktop.call("ui_action", ref=controls["check box", "Mark ready"],
                            action="set_checked", checked=True)
        assert same["changed"] is False
        try:
            desktop.call("ui_action", ref=controls["text", "Project"], action="set_text",
                         text="must not be written", expected_text="old unrelated value")
        except ToolError as error:
            assert error.code == "stale_observation", error
        else:
            raise AssertionError("stale text was overwritten")
        accepted = invoke("Review package")
        assert accepted["action_status"] == "accepted" and not accepted["verified"]
        dialog = until(lambda: next((w for w in desktop.call("list_windows")["windows"]
                                    if w["title"] == "Review demonstration package"), None))
        try:
            desktop.call("ui_action", ref=controls["button", "Clear form"], action="invoke")
        except ToolError as error:
            assert error.code in ("occluded", "focus_not_acquired"), error
            modal_refusal = error.code
        else:
            raise AssertionError("parent acted behind modal")
        # Disk and GUI are independent completion oracles, not just do_action's bool.
        desktop.call("activate_window", target=dialog["id"])
        dialog_tree = desktop.call("ui_snapshot", window_id=dialog["id"])
        save = next(n["ref"] for n in dialog_tree["controls"]
                    if n["name"] == "Save package" and n["role"] == "button")
        desktop.call("ui_action", ref=save, action="invoke")
        expected_package = {"fields": expected, "options": options}
        until(lambda: (output / "package.json").exists())
        until(lambda expected_package=expected_package: json.loads((output / "package.json").read_text()) == expected_package)
        until(lambda dialog=dialog: all(w["id"] != dialog["id"]
                          for w in desktop.call("list_windows")["windows"]))
        try:
            desktop.call("ui_action", ref=save, action="invoke")
        except ToolError as error:
            assert error.code in ("stale_observation", "window_not_found"), error
            stale_refusal = error.code
        else:
            raise AssertionError("destroyed dialog ref was accepted")
        desktop.call("activate_window", target=window_id)
        recover_start = time.monotonic()
        invoke("Clear form")
        for name in expected:
            desktop.call("ui_wait", ref=controls["text", name], text="")
        invoke("Reopen package")
        for name, value in expected.items():
            desktop.call("ui_wait", ref=controls["text", name], text=value)
        for name in options:
            desktop.call("ui_wait", ref=controls["check box", name], checked=True)
        # Read every field independently as well as condition checking it.
        actual = {name: desktop.call("ui_inspect", ref=controls["text", name],
                                    include_text=True)["text"] for name in expected}
        assert actual == expected
        invoke("Verify recovery")
        desktop.call("ui_wait", ref=controls["label", "Run status"],
                     text="PASS: all 14 fields and 3 options exactly match the saved package.")
        assert json.loads((output / "verified.json").read_text())["exact"]
        measurements.append({"trial": trial, "snapshot_ms": snapshot_ms,
                             "fill_14_fields_3_options_seconds": fill_seconds,
                             "clear_reopen_verify_seconds": time.monotonic()-recover_start,
                             "workflow_seconds": time.monotonic()-start,
                             "modal_refusal": modal_refusal, "stale_dialog_refusal": stale_refusal,
                             "exact": True, "screenshots": 0})
    return {"backend": "linux-atspi", "samples": measurements,
            "description": "Worker-local time, includes guards and exact readback; excludes model latency."}

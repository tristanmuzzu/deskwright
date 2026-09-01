#!/usr/bin/env python3
"""do_steps refuses a sequence it cannot finish BEFORE running any of it.

Pure in-process: every handler is replaced by a recorder, the look machinery
by stubs, and time.sleep by a counter. Nothing here touches D-Bus, the
compositor, or the screen, so it runs headless and on a locked machine.

Three claims:
  1. A malformed step anywhere in the sequence raises ToolError(bad_args)
     naming that step's index, and NOTHING executes -- no handler is called,
     no sleep happens. (A run that cannot finish must never start.)
  2. `look: "region"` respects `look_at` in both documented forms -- object
     and [x, y, width, height] array -- even when the sequence ends on a
     window. (It used to be silently replaced by the window's geometry.)
  3. `wait_for` is a step verb with the wait_for tool's arguments, and
     `sleep` allows up to 60 s, pointing at wait_for beyond that.

    python3 -m pytest tests/test_steps_validation.py    # or run it directly
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskwright import (
    server,
    steps,
)
from deskwright.capture import _Look
from deskwright.errors import ToolError


class Harness:
    """tool_do_steps with every side effect replaced by a recorder."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.sleeps: list[float] = []
        self.looked: _Look | None = None

    def __enter__(self):
        self._saved = (server.HANDLERS, steps.time.sleep, steps._look_before,
                       steps._look, steps._resolve_target, steps.window_at,
                       steps._window_for_atspi_app)

        def record(name):
            def handler(a, _name=name):
                self.calls.append((_name, dict(a)))
                if _name == "pointer_click":
                    return {"window": {"id": 42, "x": 500, "y": 500,
                                       "width": 300, "height": 200,
                                       "wm_class": "gedit"}}
                return {"detail": f"{_name} ran"}
            return handler

        server.HANDLERS = {name: record(name) for name in server.HANDLERS}
        steps.time.sleep = lambda s: self.sleeps.append(s)

        def look_before(a, hint_window=None, point=None):
            # The real resolver, minus the captures it would take.
            from deskwright.capture import _look_region
            mode, region, window = _look_region(a, hint_window, point)
            return _Look(mode, region, window, None)

        def look(a, result, prepared):
            self.looked = prepared
            return result

        def refuse(*_a, **_k):
            raise ToolError("no desktop in this test", code="window_not_found")

        steps._look_before = look_before
        steps._look = look
        steps._resolve_target = refuse
        steps.window_at = refuse
        steps._window_for_atspi_app = lambda _name: None
        return self

    def __exit__(self, *exc):
        (server.HANDLERS, steps.time.sleep, steps._look_before, steps._look,
         steps._resolve_target, steps.window_at,
         steps._window_for_atspi_app) = self._saved
        return False

    def nothing_ran(self) -> bool:
        return not self.calls and not self.sleeps


def rejects(args: dict, *needles: str) -> ToolError:
    """The call fails up front with bad_args, and nothing executed."""
    with Harness() as h:
        try:
            steps.tool_do_steps(args)
        except ToolError as e:
            assert e.code == "bad_args", f"code {e.code!r}, wanted bad_args: {e}"
            for needle in needles:
                assert needle in str(e), f"{needle!r} not in error: {e}"
            assert h.nothing_ran(), \
                f"executed before refusing: calls={h.calls} sleeps={h.sleeps}"
            return e
        raise AssertionError(f"no ToolError for {args}")


# --- 1. up-front validation: the whole sequence, before anything runs -------

def test_unknown_verb_names_the_step_and_nothing_runs():
    rejects({"steps": [{"do": "sleep", "ms": 4000},
                       {"do": "click", "x": 1, "y": 2},
                       {"do": "frobnicate"}]},
            "step 2", "unknown do")


def test_non_dict_step():
    rejects({"steps": [{"do": "sleep", "ms": 5}, "click"]},
            "step 1", "not an object")


def test_missing_required_key_from_schema():
    # click's x/y come from the tool schema, not the verb table.
    rejects({"steps": [{"do": "click", "y": 2}]}, "step 0", "x")


def test_missing_required_key_from_verb_table():
    rejects({"steps": [{"do": "type", "text": "hi"}]}, "step 0", "target")


def test_unknown_key_in_step():
    rejects({"steps": [{"do": "click", "x": 1, "y": 2, "tex": "typo"}]},
            "step 0", "tex")


def test_empty_and_non_list():
    rejects({"steps": []})
    rejects({"steps": "click"})


def test_late_bad_step_after_valid_action_step():
    # The regression this feature exists for: steps 0-1 are fine, step 2 is
    # not, and steps 0-1 must not fire.
    rejects({"steps": [{"do": "click", "x": 1, "y": 2},
                       {"do": "key", "target": "gedit", "combo": "ctrl+s"},
                       {"do": "click"}]},
            "step 2")


# --- 2. look:"region" respects look_at, both forms --------------------------

def region_survives(look_at) -> None:
    with Harness() as h:
        out = steps.tool_do_steps({"steps": [{"do": "click", "x": 600, "y": 550}],
                                   "look": "region", "look_at": look_at})
        assert out["all_ok"], out
        assert [c[0] for c in h.calls] == ["pointer_click"]
        assert h.looked is not None
        assert h.looked.mode == "region"
        # The caller's rectangle, not the window (500,500,300,200) the
        # sequence ended on.
        assert h.looked.region == (5, 6, 200, 100), h.looked.region


def test_look_region_object_form_survives_a_window_sequence():
    region_survives({"x": 5, "y": 6, "width": 200, "height": 100})


def test_look_region_array_form_survives_a_window_sequence():
    region_survives([5, 6, 200, 100])


def test_look_false_still_attaches_nothing():
    with Harness() as h:
        steps.tool_do_steps({"steps": [{"do": "click", "x": 1, "y": 2}],
                             "look": False})
        assert h.looked is not None and h.looked.mode is False


def test_look_region_without_look_at_is_refused_up_front():
    rejects({"steps": [{"do": "click", "x": 1, "y": 2}], "look": "region"},
            "look_at")


def test_malformed_look_at_is_refused_up_front():
    rejects({"steps": [{"do": "click", "x": 1, "y": 2}],
             "look": "region", "look_at": "nonsense"})
    rejects({"steps": [{"do": "click", "x": 1, "y": 2}],
             "look": "region", "look_at": {"x": 1, "y": 2}})
    rejects({"steps": [{"do": "click", "x": 1, "y": 2}],
             "look": "region", "look_at": [1, 2, 3]})


def test_bad_look_value_is_refused_up_front():
    rejects({"steps": [{"do": "click", "x": 1, "y": 2}], "look": "windw"},
            "look must be")


# --- 3. wait_for step verb, and the sleep cap -------------------------------

def test_wait_for_is_a_step_verb():
    with Harness() as h:
        out = steps.tool_do_steps({"steps": [{"do": "wait_for",
                                              "condition": "window_exists",
                                              "target": "gedit",
                                              "timeout": 5}]})
        assert out["all_ok"], out
        assert h.calls[0][0] == "wait_for"
        assert h.calls[0][1]["condition"] == "window_exists"
        assert h.calls[0][1]["look"] is False   # one picture per call


def test_wait_for_bad_condition_refused_up_front():
    rejects({"steps": [{"do": "sleep", "ms": 3000},
                       {"do": "wait_for", "condition": "pigs_fly"}]},
            "step 1", "condition")


def test_wait_for_missing_target_refused_up_front():
    rejects({"steps": [{"do": "wait_for", "condition": "window_gone"}]},
            "step 0", "target")


def test_wait_for_focus_changes_needs_no_target():
    with Harness() as h:
        out = steps.tool_do_steps({"steps": [{"do": "wait_for",
                                              "condition": "focus_changes"}]})
        assert out["all_ok"] and h.calls[0][0] == "wait_for"


def test_wait_for_timeout_out_of_range():
    rejects({"steps": [{"do": "wait_for", "condition": "focus_changes",
                        "timeout": 500}]},
            "step 0", "timeout")


def test_sleep_cap_is_60s_and_points_at_wait_for():
    e = rejects({"steps": [{"do": "sleep", "ms": 60_001}]},
                "step 0", "60000")
    assert "wait_for" in str(e)


def test_sleep_up_to_60s_is_allowed():
    with Harness() as h:
        out = steps.tool_do_steps({"steps": [{"do": "sleep", "ms": 60_000}],
                                   "look": False})
        assert out["all_ok"] and h.sleeps == [60.0]


def test_sleep_rejects_non_numeric_and_unknown_keys():
    rejects({"steps": [{"do": "sleep", "ms": "soon"}]}, "step 0")
    rejects({"steps": [{"do": "sleep", "seconds": 2}]}, "step 0", "seconds")


def test_sleep_zero_is_refused_not_silently_defaulted():
    # `ms or wait_ms or 200` turned an explicit ms: 0 into a 200ms sleep;
    # an explicit zero must hit the range check and be refused instead.
    rejects({"steps": [{"do": "sleep", "ms": 0}]}, "step 0", "between")
    rejects({"steps": [{"do": "wait_ms", "wait_ms": 0}]}, "step 0", "between")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL  {name}: {e}")
    print(f"{failures} failure(s)")
    sys.exit(1 if failures else 0)

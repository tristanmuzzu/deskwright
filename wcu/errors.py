"""Tool failures, with stable machine-readable codes.

A ToolError is a failure the model should see and can act on, not a crash.
Every raise site states a `code` from CODES so a harness (or the retry
machinery in steps.py) can branch on the *kind* of failure without parsing
prose. The prose stays the primary payload -- codes classify it, they do not
replace it. On the wire the code is prefixed to the message as `[code] ...`;
in-process, catch the exception and read `.code`.

The taxonomy is deliberately small. A code earns its place by being a
distinct *recovery path*, not a distinct message: `focus_not_acquired` is
retryable, `refused_combo` never is, and collapsing either into a generic
bucket would cost the caller exactly the decision it needs to make.
"""


CODES = {
    # The request itself is wrong; retrying the same call can never help.
    "bad_args":            "malformed or missing arguments, schema-level",
    "refused_combo":       "combo is refused by policy (VT switch, logout)",
    "no_expectation":      "an identity/target expectation is required and missing",

    # The named thing is not there; retry only after the world changes.
    "window_not_found":    "no window matches the target",
    "app_not_on_bus":      "application is not on the AT-SPI bus",
    "widget_missing":      "no widget at the path / matching the query",

    # The world moved between look and act; re-look, then retry.
    "widget_moved":        "widget at the path no longer matches expect_name/expect_role",
    "focus_not_acquired":  "the target window never reported focused",
    "occluded":            "the point would be delivered to a different window",
    "off_screen":          "coordinates are outside the desktop",

    # The write path accepted the request and it did not take effect;
    # recovery is switching route (keyboard injection instead of AT-SPI),
    # not retrying the same call.
    "atspi_write_failed":  "AT-SPI action/write executed but did not take effect",

    # A mechanism is down; recovery is environmental, not a retry.
    "extension_unavailable": "the shell extension is not reachable (inactive or locked screen)",
    "needs_relogin":       "the running shell predates this extension method",
    "input_backend_failed": "the injection backend (RemoteDesktop / ydotool) failed",
    "capture_failed":      "screenshot / recording could not be produced",
    "atspi_unavailable":   "the AT-SPI bus itself is not reachable",

    # Time ran out on an honest wait.
    "timeout":             "the awaited condition did not occur in time",

    # Everything not yet classified. Raise sites should not choose this;
    # it is the default for a bare ToolError until every site is tagged.
    "unclassified":        "failure without a specific code",
}


class ToolError(Exception):
    """A failure the model should see and can act on, not a crash."""

    def __init__(self, message: str, *, code: str = "unclassified"):
        assert code in CODES, f"unknown error code {code!r}"
        super().__init__(message)
        self.code = code

    def wire_text(self) -> str:
        """The message as sent over MCP: `[code] prose`."""
        return f"[{self.code}] {self}"

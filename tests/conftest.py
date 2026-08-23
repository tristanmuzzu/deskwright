"""Keep pytest away from the script-style live-session suites.

test_clipboard.py, test_pointer.py and test_screencast.py are scripts with
their own main(): they check for a live Wayland session, save and restore
the user's clipboard, and print a report. Run them directly:

    ./tests/test_clipboard.py

Under pytest their parametered helpers error on missing fixtures, and --
worse -- their parameterless helpers RUN, writing the real clipboard and
recording the real screen without the save/restore and session guards that
main() provides. A plain `pytest tests/` must never clobber user state, so
these files are not collected at all.
"""
collect_ignore = [
    "test_clipboard.py",
    "test_pointer.py",
    "test_screencast.py",
]

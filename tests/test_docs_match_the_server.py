#!/usr/bin/env python3
"""The auto-approval doc has to name the tools the server actually serves.

`docs/claude-code-setup.md` offers an explicit allowlist as the safer option
-- "a tool added by a future version is not silently pre-approved". That is
only true if the list is complete when it is written, and it stopped being:
it named 25 of 33 tools and 9 of 14 acting ones, so anyone who took the safer
option got eight tools prompting in the middle of an unattended run, which is
the exact failure the document exists to prevent.

    python3 -m pytest tests/test_docs_match_the_server.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from deskwright.server import _READ_ONLY_TOOLS, TOOLS

DOC = ROOT / "docs" / "claude-code-setup.md"
PREFIX = "mcp__deskwright__"


def _tool_names() -> set[str]:
    return {t["name"] for t in TOOLS} if isinstance(TOOLS, list) else set(TOOLS)


def test_the_allowlist_names_every_tool():
    text = DOC.read_text()
    listed = set(re.findall(re.escape(PREFIX) + r"([a-z_]+)", text))
    served = _tool_names()
    assert listed == served, (
        f"allowlist is out of date: missing {sorted(served - listed)}, "
        f"stale {sorted(listed - served)}")


def test_the_stated_tool_count_is_right():
    served = _tool_names()
    assert f"all {len(served)} tools the server serves" in DOC.read_text()


def test_the_cautious_split_names_every_acting_tool():
    """The 'add these to permissions.ask' half has to match what the server
    journals and what the halt switch gates, or the split is decorative."""
    text = DOC.read_text()
    cautious = text[text.index("## The cautious variant"):]
    acting = {n for n in _tool_names() if n not in _READ_ONLY_TOOLS}
    looking = _tool_names() - acting
    listed_acting = set(re.findall(r"^- `([a-z_]+)`", cautious, re.M))
    assert acting <= listed_acting, f"not listed as acting: {sorted(acting - listed_acting)}"
    assert looking <= listed_acting, f"not listed as looking: {sorted(looking - listed_acting)}"
    assert f"the same {len(acting)} it" in cautious


def test_the_readme_names_every_tool_and_counts_them_right():
    """The README's table is what people read before installing. A tool that
    is not in it is a tool nobody knows about; a count that is wrong is the
    first thing a reader notices."""
    readme = (ROOT / "README.md").read_text()
    served = _tool_names()
    missing = sorted(n for n in served if f"`{n}`" not in readme)
    assert not missing, f"not in the README table: {missing}"
    assert f"{len(served)} tools" in readme

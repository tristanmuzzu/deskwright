#!/usr/bin/env python3
"""The docs have to read like a person wrote them.

Not a style opinion for its own sake. Every reader who lands on this repo is
deciding in about fifteen seconds whether it was written by somebody who uses
the thing, and the em-dash-heavy register that large language models default
to is the single fastest tell. The first draft of the README had 83 of them
across 725 lines; the two projects this one is modelled on have one and zero.

So this is a regression test on voice. If you genuinely want an em-dash,
delete this file and say why in the commit.

    python3 -m pytest tests/test_prose.py
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

DOCS = sorted(
    p for p in (
        list(ROOT.glob("*.md"))
        + list((ROOT / "docs").glob("*.md"))
        + list((ROOT / "skills").rglob("*.md"))
        + list((ROOT / "deskwright" / "extension").glob("*.md"))
    )
)


def test_there_are_docs_to_check():
    assert len(DOCS) >= 8, [p.name for p in DOCS]


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_em_dashes(doc):
    lines = [f"{n}: {line.strip()}"
             for n, line in enumerate(doc.read_text().split("\n"), 1)
             if "—" in line]
    assert not lines, (
        f"{doc.relative_to(ROOT)} has em dashes. Use a comma, a colon, "
        "parentheses, or two sentences:\n  " + "\n  ".join(lines[:10]))


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_smart_quotes(doc):
    """They arrive with the same paste and break copied commands."""
    curly = "\u201c\u201d\u2018\u2019"
    bad = [f"{n}: {line.strip()}"
           for n, line in enumerate(doc.read_text().split("\n"), 1)
           if any(ch in line for ch in curly)]
    assert not bad, (f"{doc.relative_to(ROOT)} has curly quotes:\n  "
                     + "\n  ".join(bad[:10]))


def test_the_readme_stays_readable_in_one_sitting():
    """It was 725 lines once. Nobody read it. The deep material lives in
    docs/field-notes.md, which is linked from the README and not in its way."""
    readme = (ROOT / "README.md").read_text().split("\n")
    assert len(readme) < 420, (
        f"README is {len(readme)} lines. Move the detail into docs/ and link "
        "it, the way field-notes.md is linked.")


def test_the_agent_runbook_exists_and_is_linked():
    """The README's headline install path is 'hand it to your agent', which
    only works if the runbook it points at is really there."""
    agents = ROOT / "AGENTS.md"
    assert agents.is_file()
    body = agents.read_text()
    for command in ("pipx install --system-site-packages deskwright",
                    "deskwright-setup --check",
                    "claude mcp add deskwright",
                    "DESKWRIGHT_SESSION=headless deskwright --self-test"):
        assert command in body, f"AGENTS.md never tells the agent to run: {command}"
    assert "AGENTS.md" in (ROOT / "README.md").read_text()

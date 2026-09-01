"""Append-only action journal: evidence, not surveillance.

Every acted (non-read-only) tool call appends one JSON line to
$XDG_STATE_HOME/deskwright/journal/<YYYY-MM-DD>.jsonl so a human
can review an unattended run afterwards, an agent can re-read its own trail
after context loss, and a bug report becomes replayable. Reading tools are
NOT journaled -- that would be noise, and the caller (server.py) decides via
SHOULD_JOURNAL.

Contract:
  * `record()` is fire-and-forget: it never raises. A journal failure must
    never fail the action it describes.
  * Each entry is ONE line written with O_APPEND, so concurrent writers
    interleave whole lines, not bytes.
  * Redaction: string arguments longer than TEXT_TRUNCATE_AT are truncated
    with a length note, and inline screenshot payloads
    (`__inline_image__`-like fields) are never stored -- the journal keeps
    the shot's path and sha256 instead.
  * Retention mirrors the safe-rm graveyard: journal files older than
    RETENTION_DAYS are deleted on module load.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import ToolError

RETENTION_DAYS = 14
TEXT_TRUNCATE_AT = 200
DEFAULT_TAIL = 20
MAX_TAIL = 1000
HASH_MAX_BYTES = 64 * 1024 * 1024   # a shot is ~100KB; never grind on a giant file
_REDACT_MAX_DEPTH = 8

# The seam server.py consults: journal acted tools, skip reading tools.
def SHOULD_JOURNAL(tool_name: str, read_only: bool) -> bool:
    return not read_only


def _today() -> date:
    """Local calendar date, matching the local-time file names."""
    return datetime.now(UTC).astimezone().date()


def _journal_dir() -> Path:
    """Resolved on every call so a test's XDG_STATE_HOME override is honoured."""
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(state) / "deskwright" / "journal"


_session_id: str | None = None


def _session() -> str:
    """This process's identity: pid plus kernel start-time, computed once.

    A bare pid recycles; pid + starttime (clock ticks since boot, field 22 of
    /proc/self/stat) does not, so two runs of the server never share an id.
    """
    global _session_id
    if _session_id is None:
        pid = os.getpid()
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
                stat = f.read()
            # comm (field 2) may contain spaces and parens; split after the
            # LAST ')' so fields 3+ are clean. starttime is field 22.
            fields = stat.rsplit(")", 1)[1].split()
            _session_id = f"{pid}-{fields[19]}"
        except Exception:
            _session_id = f"{pid}-{int(time.time())}"
    return _session_id


def _is_image_field(key: str) -> bool:
    """Fields that carry inline image payloads (base64) -- never stored.

    Matches `__inline_image__` / `__inline_image__s` (capture.py's _INLINE_KEY)
    and any `_image_data`-like name, without catching a plain informational
    "image" note string.
    """
    k = str(key).lower()
    return "image" in k and (k.startswith("_") or "data" in k)


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > _REDACT_MAX_DEPTH:
        return "... [depth limit]"
    if isinstance(value, str) and len(value) > TEXT_TRUNCATE_AT:
        return value[:TEXT_TRUNCATE_AT] + f"... [truncated, {len(value)} chars total]"
    if isinstance(value, dict):
        return {k: _redact(v, depth + 1)
                for k, v in value.items() if not _is_image_field(k)}
    if isinstance(value, (list, tuple)):
        return [_redact(v, depth + 1) for v in value]
    return value


# Tools whose text argument is, sometimes, a password. Everything an agent
# uses to enter one goes through exactly these three, and a password is
# comfortably under the 200-character truncation, so it landed verbatim in a
# file on disk. The journal never needed the characters: what a reviewer
# reconstructs from it is "something was typed here, this much of it, and it
# was/was not the same string as that other time" -- which a length and a
# digest answer exactly. `DESKWRIGHT_JOURNAL_TEXT=1` restores the old behaviour for
# anyone debugging their own flows.
TEXT_BEARING = {
    "type_text": ("text",),
    "clipboard_write": ("text",),
    "ui_set_text": ("text",),
}


def _fingerprint(text: str) -> dict:
    return {"chars": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]}


# The same three tools reached through `do_steps`, by their step verb. A
# password typed inside a batch is still a password.
STEP_VERBS_BEARING_TEXT = {"type", "set_text", "clipboard_write"}


def _redact_text_args(tool: str, args: dict) -> dict:
    if os.environ.get("DESKWRIGHT_JOURNAL_TEXT") == "1":
        return args
    out = dict(args)
    for key in TEXT_BEARING.get(str(tool), ()):
        value = out.get(key)
        if isinstance(value, str):
            out[key] = _fingerprint(value)
    if str(tool) == "do_steps" and isinstance(out.get("steps"), (list, tuple)):
        out["steps"] = [_redact_step(st) for st in out["steps"]]
    return out


def _redact_step(step: Any) -> Any:
    if not isinstance(step, dict):
        return step
    verb = str(step.get("do") or "").strip().lower()
    if verb not in STEP_VERBS_BEARING_TEXT or not isinstance(step.get("text"), str):
        return step
    return {**step, "text": _fingerprint(step["text"])}


def _sha256_of(path: str) -> str | None:
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size > HASH_MAX_BYTES:
            return None
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _summary(outcome: Any) -> dict:
    """The evidence a reviewer needs, not the whole result.

    ok/error(+code), the human `detail`, the hit/miss verdict fields
    (`changed`, `verified`, `widget_focus`) wherever the tool put them, and
    the shot's path + file sha256 when one was taken.
    """
    if not isinstance(outcome, dict):
        return {"ok": True, "detail": _redact(str(outcome))}
    s: dict[str, Any] = {"ok": "error" not in outcome}
    if not s["ok"]:
        s["error"] = _redact(str(outcome["error"]))
        if outcome.get("code"):
            s["code"] = str(outcome["code"])
    look = outcome.get("look")
    look = look if isinstance(look, dict) else {}
    detail = outcome.get("detail", look.get("detail"))
    if detail is not None:
        s["detail"] = _redact(str(detail))
    for key in ("changed", "verified", "widget_focus"):
        if key in outcome:
            s[key] = _redact(outcome[key])
        elif key in look:
            s[key] = _redact(look[key])
    shot = outcome.get("path", look.get("path"))
    if isinstance(shot, str) and shot:
        s["shot"] = {"path": shot}
        digest = _sha256_of(shot)
        if digest:
            s["shot"]["sha256"] = digest
    return s


def record(tool: str, args: dict, outcome: dict) -> None:
    """Append one journal line. Fire-and-forget: never raises.

    `outcome` is the handler's result dict on success, or
    {"error": <message>, "code": <ToolError code>} on failure.
    """
    try:
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "session": _session(),
            # WHICH desktop. With several named headless sessions live at
            # once, "a click happened" is not a reviewable record unless it
            # says where -- and telling a headless action from one on the
            # user's real screen is the first question anyone asks of this
            # file. Read at call time, because pin_env() sets it after import.
            "desktop": os.environ.get("DESKWRIGHT_HEADLESS_NAME")
            or ("headless" if os.environ.get("DESKWRIGHT_HEADLESS") else "primary"),
            "tool": str(tool),
            "args": _redact(_redact_text_args(tool, dict(args or {}))),
            "outcome": _summary(outcome),
        }
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        d = _journal_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / (time.strftime("%Y-%m-%d") + ".jsonl")
        # O_APPEND + a single write: whole lines land atomically even with a
        # concurrent writer, and there is no lock to leak on a crash.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass  # the journal must never fail the action it describes


def _rotate() -> None:
    """Delete journal files older than RETENTION_DAYS. Never raises.

    Only files whose stem parses as YYYY-MM-DD are touched; anything else in
    the directory is not this module's to delete.
    """
    try:
        d = _journal_dir()
        if not d.is_dir():
            return
        cutoff = _today() - timedelta(days=RETENTION_DAYS)
        for p in d.glob("*.jsonl"):
            try:
                day = date.fromisoformat(p.stem)
            except ValueError:
                continue
            if day < cutoff:
                try:
                    p.unlink()
                except OSError:
                    pass
    except Exception:
        pass


def tool_journal(a: dict) -> dict:
    """Read the trail back: the last `tail` acted calls, oldest first."""
    try:
        tail = int(a.get("tail") or DEFAULT_TAIL)
    except (TypeError, ValueError):
        raise ToolError(f"tail must be an integer, got {a.get('tail')!r}",
                        code="bad_args") from None
    tail = max(1, min(tail, MAX_TAIL))
    session = str(a.get("session") or "current")
    if session not in ("current", "all"):
        raise ToolError(f'session must be "current" or "all", got {session!r}',
                        code="bad_args")
    want = _session() if session == "current" else None

    d = _journal_dir()
    files = sorted(d.glob("*.jsonl"), reverse=True) if d.is_dir() else []
    entries: list[dict] = []
    skipped = 0
    for f in files:                       # newest day first, stop when full
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        day: list[dict] = []
        for line in lines:                # chronological within a file
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(e, dict):
                skipped += 1
                continue
            if want is not None and e.get("session") != want:
                continue
            day.append(e)
        entries = day[-(tail - len(entries)):] + entries if day else entries
        if len(entries) >= tail:
            break

    result: dict[str, Any] = {
        "entries": entries,
        "count": len(entries),
        "session": session,
        "dir": str(d),
        "detail": (f"last {len(entries)} journaled action(s), oldest first"
                   + (f", of session {want}" if want else ", across all sessions")),
    }
    if want:
        result["session_id"] = want
    if skipped:
        result["skipped_corrupt_lines"] = skipped
    return result


_rotate()   # retention runs once, when the server imports the journal

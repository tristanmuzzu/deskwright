"""`browser_devtools`: read and steer a Chrome page through its DevTools port.

Chrome and Electron expose almost nothing through AT-SPI, so an agent that
launched a browser on the private desktop could see pixels and OCR but not the
page's own state. Agents worked around it by writing throwaway CDP scripts; one
session journal reads "there is no way to read a launched browser's
console/JS state through Deskwright, so I wrote a separate CDP script". This
module is that script, once, bounded and tested.

The browser has to be started with `--remote-debugging-port=N` and its own
`--user-data-dir` (Chrome ignores the port flag when it joins a running
profile). Only loopback is ever contacted: the host is checked before anything
is sent, the HTTP request bypasses any configured proxy, and the websocket URL
Chrome hands back must point at the same loopback port or it is refused.

The websocket client is a minimal RFC 6455 client on the stdlib. Checked
2026-11-09 on this machine: `websocket-client` is not installed, and
`websockets` is present only as the distro's python3-websockets, which
`pipx install --system-site-packages` cannot promise on another machine.
Pulling in a dependency for one text-frame client was not worth it.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

from .errors import ToolError
from .execution import remaining

ACTIONS = ("targets", "evaluate", "console", "screenshot", "navigate")

# Loopback names, and the address each one is dialled as. "localhost" is
# mapped here rather than resolved, so /etc/hosts cannot point it elsewhere.
LOCAL_HOSTS = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1", "::1": "::1"}

TIMEOUT_DEFAULT_S = 10.0
TIMEOUT_MAX_S = 60.0
CONSOLE_DEFAULT_S = 2.0
CONSOLE_MAX_S = 30.0

LIST_MAX_BYTES = 4 << 20        # /json/list with hundreds of targets is ~100 KB
HEAD_MAX_BYTES = 16 << 10       # the 101 response is a few hundred bytes
MESSAGE_MAX_BYTES = 64 << 20    # a full-HD PNG screenshot is ~1-3 MB base64
EVENTS_MAX = 10_000             # a chatty page must not grow memory unbounded
CONSOLE_MAX_ENTRIES = 500
TEXT_MAX_CHARS = 2000
VALUE_MAX_CHARS = 100_000

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"   # RFC 6455 section 1.3


class _Timeout(Exception):
    """The deadline passed while waiting on the socket."""


class _Closed(Exception):
    """The peer closed the websocket (or the TCP connection)."""


class _CDPError(Exception):
    def __init__(self, method: str, error: dict):
        super().__init__(f"{method}: {error.get('message', error)}")
        self.method = method
        self.error = error


def _left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _Timeout()
    return left


def _mask(data: bytes, key: bytes) -> bytes:
    """XOR with the 4-byte key as one big integer: about 100x faster than a
    per-byte loop, which matters only for large frames but costs nothing."""
    n = len(data)
    if not n:
        return b""
    stream = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(n, "big")


class WebSocket:
    """A client websocket for one CDP target: text frames out, messages in.

    Handles what a DevTools endpoint actually sends: fragmented messages
    (continuation frames), pings, close, and 16- and 64-bit payload lengths.
    No extensions are negotiated, so RSV bits set by the peer are an error.
    """

    def __init__(self, host: str, port: int, path: str, deadline: float):
        self.deadline = deadline
        self._buf = bytearray()
        try:
            self.sock = socket.create_connection((host, port), timeout=_left(deadline))
        except TimeoutError as e:
            raise _Timeout() from e
        try:
            self._handshake(host, port, path)
        except BaseException:
            self.sock.close()
            raise

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        # No Origin header on purpose: since Chrome 111 a websocket that sends
        # one is refused unless the browser was started with
        # --remote-allow-origins, and a non-browser client has no origin.
        request = (f"GET {path} HTTP/1.1\r\nHost: {authority}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self._sendall(request.encode("ascii"))
        while b"\r\n\r\n" not in self._buf:
            if len(self._buf) > HEAD_MAX_BYTES:
                raise _Closed("handshake response too large")
            self._fill()
        head, _, rest = bytes(self._buf).partition(b"\r\n\r\n")
        self._buf = bytearray(rest)
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ", 2)
        if len(status) < 2 or status[1] != "101":
            raise _Closed(f"handshake refused: {lines[0]!r}")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise _Closed("handshake answered with the wrong Sec-WebSocket-Accept")

    # -- socket I/O, every wait bounded by self.deadline ----------------------
    def _sendall(self, data: bytes) -> None:
        self.sock.settimeout(_left(self.deadline))
        try:
            self.sock.sendall(data)
        except TimeoutError as e:
            raise _Timeout() from e
        except OSError as e:
            raise _Closed(str(e)) from e

    def _fill(self) -> None:
        self.sock.settimeout(_left(self.deadline))
        try:
            chunk = self.sock.recv(1 << 16)
        except TimeoutError as e:
            raise _Timeout() from e
        except OSError as e:
            raise _Closed(str(e)) from e
        if not chunk:
            raise _Closed("connection closed")
        self._buf += chunk

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._fill()
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    # -- frames ---------------------------------------------------------------
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 1 << 16:
            header.append(0x80 | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, "big")
        key = os.urandom(4)                  # clients MUST mask (RFC 6455 5.3)
        self._sendall(bytes(header) + key + _mask(payload, key))

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_message(self) -> str:
        parts: list[bytes] = []
        total = 0
        started: int | None = None
        while True:
            b0, b1 = self._recv_exact(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            if b0 & 0x70:
                raise _Closed("peer set reserved bits without an extension")
            n = b1 & 0x7F
            if n == 126:
                n = int.from_bytes(self._recv_exact(2), "big")
            elif n == 127:
                n = int.from_bytes(self._recv_exact(8), "big")
            if opcode >= 0x8 and (n > 125 or not fin):
                raise _Closed("malformed control frame")
            if total + n > MESSAGE_MAX_BYTES:
                raise _Closed(f"message larger than {MESSAGE_MAX_BYTES} bytes")
            key = self._recv_exact(4) if b1 & 0x80 else None
            payload = self._recv_exact(n)
            if key:
                payload = _mask(payload, key)
            if opcode == 0x9:                       # ping -> pong, same payload
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:                       # unsolicited pong
                continue
            if opcode == 0x8:
                with contextlib.suppress(_Timeout, _Closed):
                    self._send_frame(0x8, payload[:2])
                raise _Closed("peer sent close")
            if opcode == 0x0:
                if started is None:
                    raise _Closed("continuation frame with nothing to continue")
            elif opcode in (0x1, 0x2):
                if started is not None:
                    raise _Closed("new message started inside a fragmented one")
                started = opcode
            else:
                raise _Closed(f"unknown opcode {opcode:#x}")
            parts.append(payload)
            total += n
            if fin:
                return b"".join(parts).decode("utf-8", errors="replace")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.deadline = time.monotonic() + 0.5
            self._send_frame(0x8, (1000).to_bytes(2, "big"))
        with contextlib.suppress(OSError):
            self.sock.close()


class _Session:
    """CDP request/response over one websocket, with events kept in order."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.events: list[dict] = []
        self.dropped = 0
        self._id = 0

    def _read(self) -> dict:
        try:
            msg = json.loads(self.ws.recv_message())
        except json.JSONDecodeError:
            return {}
        if isinstance(msg, dict) and "method" in msg and "id" not in msg:
            if len(self.events) >= EVENTS_MAX:
                self.dropped += 1
            else:
                self.events.append(msg)
        return msg if isinstance(msg, dict) else {}

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = self._read()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise _CDPError(method, msg["error"])
                return msg.get("result") or {}

    def listen(self, until: float) -> None:
        """Collect events until `until`; reaching it is the normal end."""
        saved = self.ws.deadline
        self.ws.deadline = until
        try:
            while True:
                self._read()
        except _Timeout:
            pass
        finally:
            self.ws.deadline = saved

    def wait_event(self, method: str, start: int = 0) -> dict:
        """The first `method` event at index >= start, reading until it arrives."""
        seen = start
        while True:
            for event in self.events[seen:]:
                if event.get("method") == method:
                    return event
            seen = len(self.events)
            self._read()


# =========================================================================
# endpoint discovery
# =========================================================================
def _host(raw: Any) -> tuple[str, str]:
    name = str(raw if raw is not None else "127.0.0.1").strip().lower().strip("[]")
    if name not in LOCAL_HOSTS:
        raise ToolError(
            f"browser_devtools only talks to this machine; host {raw!r} is refused. "
            "Use 127.0.0.1 (the default), localhost or ::1.", code="bad_args")
    return name, LOCAL_HOSTS[name]


def _authority(addr: str, port: int) -> str:
    return f"[{addr}]:{port}" if ":" in addr else f"{addr}:{port}"


def _unreachable(port: int, why: str) -> ToolError:
    return ToolError(
        f"nothing is answering DevTools on 127.0.0.1:{port} ({why}). Launch the "
        f"browser with --remote-debugging-port={port} AND its own "
        "--user-data-dir=<fresh dir> (Chrome ignores the port when it joins an "
        "already-running profile), and give it a moment to start.",
        code="app_not_on_bus")


def list_targets(addr: str, port: int, deadline: float) -> list[dict]:
    url = f"http://{_authority(addr, port)}/json/list"
    # An empty ProxyHandler: urllib otherwise honours http_proxy, and this
    # request must never leave the machine.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=_left(deadline)) as response:
            raw = response.read(LIST_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise ToolError(f"port {port} answered HTTP {e.code} for /json/list; it is "
                        "not a Chrome DevTools endpoint", code="app_not_on_bus") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise _Timeout() from e
        raise _unreachable(port, str(e.reason)) from e
    except TimeoutError as e:
        raise _Timeout() from e
    except OSError as e:
        raise _unreachable(port, str(e)) from e
    if len(raw) > LIST_MAX_BYTES:
        raise ToolError("/json/list is larger than 4 MB; refusing to parse it",
                        code="app_not_on_bus")
    try:
        targets = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ToolError(f"port {port} did not answer /json/list with JSON; it is not "
                        "a Chrome DevTools endpoint", code="app_not_on_bus") from e
    if not isinstance(targets, list):
        raise ToolError(f"port {port} answered /json/list with something other than "
                        "a list", code="app_not_on_bus")
    return [t for t in targets if isinstance(t, dict)]


def _brief(t: dict) -> dict:
    return {k: t.get(k, "") for k in ("id", "type", "title", "url")}


def pick_target(targets: list[dict], want: Any) -> dict:
    """An exact id, else a unique title/url substring, else the first page."""
    pages = [t for t in targets if t.get("type") == "page"]
    if want is None or str(want).strip() == "":
        if not pages:
            raise ToolError("the browser has no page target open",
                            code="window_not_found",
                            details={"targets": [_brief(t) for t in targets][:20]})
        return pages[0]
    want = str(want)
    for t in targets:
        if t.get("id") == want:
            return t
    needle = want.lower()
    hits = [t for t in targets
            if needle in str(t.get("title", "")).lower()
            or needle in str(t.get("url", "")).lower()]
    hits = [t for t in hits if t.get("type") == "page"] or hits
    if not hits:
        raise ToolError(f"no target has id {want!r} or that text in its title or url",
                        code="window_not_found",
                        details={"targets": [_brief(t) for t in targets][:20]})
    if len(hits) > 1:
        raise ToolError(f"{len(hits)} targets match {want!r}; pass the id instead",
                        code="ambiguous_control",
                        details={"candidates": [_brief(t) for t in hits][:20]})
    return hits[0]


def _ws_path(target: dict, port: int) -> str:
    raw = target.get("webSocketDebuggerUrl")
    if not raw:
        raise ToolError(
            f"target {target.get('id')!r} offers no webSocketDebuggerUrl; another "
            "DevTools client (an open DevTools window) is probably attached to it. "
            "Close that, or pick another target.", code="app_not_on_bus")
    parts = urllib.parse.urlsplit(str(raw))
    if (parts.scheme != "ws" or (parts.hostname or "").lower() not in LOCAL_HOSTS
            or parts.port != port):
        raise ToolError(f"Chrome advertised {raw!r}, which is not ws:// on this "
                        f"machine's port {port}; refusing to connect to it",
                        code="app_not_on_bus")
    return parts.path + (f"?{parts.query}" if parts.query else "")


@contextlib.contextmanager
def _connect(addr: str, port: int, target: dict, deadline: float) -> Iterator[_Session]:
    path = _ws_path(target, port)
    try:
        ws = WebSocket(addr, port, path, deadline)
    except ConnectionRefusedError as e:
        raise _unreachable(port, "connection refused") from e
    except _Closed as e:
        raise ToolError(f"DevTools refused the websocket for target "
                        f"{target.get('id')!r}: {e}", code="app_not_on_bus") from e
    except OSError as e:
        raise _unreachable(port, str(e)) from e
    try:
        yield _Session(ws)
    finally:
        ws.close()


# =========================================================================
# the actions
# =========================================================================
def _clip(text: Any, limit: int = TEXT_MAX_CHARS) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more chars]"


def _remote_text(o: dict) -> str:
    if "value" in o:
        v = o["value"]
        return v if isinstance(v, str) else json.dumps(v)
    for key in ("unserializableValue", "description"):
        if key in o:
            return str(o[key])
    return str(o.get("type", ""))


def _exception_text(details: dict) -> str:
    exc = details.get("exception") or {}
    return str(exc.get("description") or details.get("text") or "exception")


def _top_frame(stack: dict | None) -> dict:
    frames = (stack or {}).get("callFrames") or []
    return frames[0] if frames else {}


def _console_entry(event: dict) -> dict | None:
    method, p = event.get("method"), event.get("params") or {}
    if method == "Runtime.consoleAPICalled":
        frame = _top_frame(p.get("stackTrace"))
        return {"source": "console", "level": p.get("type", "log"),
                "text": _clip(" ".join(_remote_text(a) for a in p.get("args") or [])),
                "url": frame.get("url", ""), "line": frame.get("lineNumber"),
                "timestamp": p.get("timestamp")}
    if method == "Runtime.exceptionThrown":
        d = p.get("exceptionDetails") or {}
        return {"source": "exception", "level": "error",
                "text": _clip(_exception_text(d)),
                "url": d.get("url", ""), "line": d.get("lineNumber"),
                "timestamp": p.get("timestamp")}
    if method == "Log.entryAdded":
        e = p.get("entry") or {}
        return {"source": f"log:{e.get('source', '')}", "level": e.get("level", ""),
                "text": _clip(e.get("text", "")), "url": e.get("url", ""),
                "line": e.get("lineNumber"), "timestamp": e.get("timestamp")}
    return None


def _evaluate(s: _Session, expression: str, deadline: float) -> dict:
    try:
        r = s.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
            # Terminates a runaway synchronous script inside the page, so an
            # infinite loop does not leave the tab wedged after we give up.
            "timeout": int(max(1.0, deadline - time.monotonic()) * 1000)})
    except _CDPError as e:
        raise ToolError(
            f"Chrome could not evaluate or return the value: {e.error.get('message')}. "
            "returnByValue needs a JSON-serialisable result; return the fields you "
            "need, or JSON.stringify(...).", code="bad_args", action_status="unknown") from e
    if "exceptionDetails" in r:
        d = r["exceptionDetails"]
        raise ToolError(f"the expression threw: {_clip(_exception_text(d))}",
                        code="bad_args", action_status="unknown",
                        details={"line": d.get("lineNumber"), "column": d.get("columnNumber")})
    res = r.get("result") or {}
    out: dict[str, Any] = {"type": res.get("type")}
    if res.get("subtype"):
        out["subtype"] = res["subtype"]
    if "unserializableValue" in res:
        out["value"] = res["unserializableValue"]
        out["note"] = "not representable in JSON (NaN, Infinity, -0 or a BigInt); shown as text"
        return out
    value = res.get("value")
    dumped = json.dumps(value)
    if len(dumped) > VALUE_MAX_CHARS:
        out["value_truncated"] = dumped[:VALUE_MAX_CHARS]
        out["value_chars"] = len(dumped)
    else:
        out["value"] = value
    return out


def _console(s: _Session, seconds: float) -> dict:
    # Chrome replays what it kept from before this connection while handling
    # the enable calls, so every event that arrives before the second reply is
    # history, not something that happened while listening.
    s.call("Runtime.enable")
    s.call("Log.enable")
    buffered = len(s.events)
    s.listen(time.monotonic() + seconds)
    entries = []
    for i, event in enumerate(s.events):
        entry = _console_entry(event)
        if entry is not None:
            entry["buffered"] = i < buffered
            entries.append(entry)
    dropped = max(0, len(entries) - CONSOLE_MAX_ENTRIES) + s.dropped
    entries = entries[-CONSOLE_MAX_ENTRIES:]
    out: dict[str, Any] = {
        "messages": entries, "count": len(entries), "listened_s": seconds,
        "note": "buffered=true entries were logged before this call (Chrome "
                "replays them on enable); the rest arrived while listening."}
    if dropped:
        out["dropped_oldest"] = dropped
    return out


def _screenshot(s: _Session, a: dict) -> dict:
    from .capture import _attach_inline, _png_dimensions, _shot_path
    path, persistent = _shot_path(a)
    try:
        r = s.call("Page.captureScreenshot", {"format": "png"})
        data = base64.b64decode(r.get("data") or "", validate=True)
    except (_CDPError, ValueError) as e:
        raise ToolError(f"Chrome could not capture the page: {e}",
                        code="capture_failed") from e
    if not data:
        raise ToolError("Chrome returned an empty screenshot", code="capture_failed")
    path.write_bytes(data)
    result: dict[str, Any] = {"path": str(path), "bytes": len(data),
                              "dimensions": _png_dimensions(path)}
    if not persistent:
        result["path_note"] = "kept in the shot cache; pass path= to keep it somewhere"
    return _attach_inline(result, path, {"inline": bool(a.get("inline", True))})


def _navigate(s: _Session, url: str, deadline: float, timeout: float) -> dict:
    s.call("Page.enable")
    mark = len(s.events)
    try:
        r = s.call("Page.navigate", {"url": url})
    except _CDPError as e:
        raise ToolError(f"Chrome refused to navigate to {url!r}: {e.error.get('message')}",
                        code="bad_args") from e
    if r.get("errorText"):
        raise ToolError(f"navigation to {url!r} failed: {r['errorText']}",
                        code="verification_failed")
    out: dict[str, Any] = {"requested": url}
    if r.get("loaderId"):
        try:
            s.wait_event("Page.loadEventFired", mark)
        except _Timeout as e:
            raise ToolError(f"navigation to {url!r} started but the load event did not "
                            f"fire within {timeout:g}s; the page may still be loading",
                            code="timeout") from e
        out["loaded"] = True
    else:
        out["loaded"] = True
        out["same_document"] = True        # a fragment change fires no load event
    here = _evaluate(s, "({url: location.href, title: document.title})", deadline)
    out.update(here.get("value") or {})
    return out


def _number(a: dict, key: str, default: float, low: float, high: float) -> float:
    raw = a.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not low <= raw <= high:
        raise ToolError(f"{key} must be a number from {low:g} to {high:g}", code="bad_args")
    return float(raw)


def tool_browser_devtools(a: dict) -> dict:
    port = a.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ToolError("port must be the integer given to --remote-debugging-port",
                        code="bad_args")
    host, addr = _host(a.get("host"))
    action = a.get("action")
    if action not in ACTIONS:
        raise ToolError(f"action must be one of {', '.join(ACTIONS)}", code="bad_args")
    timeout = _number(a, "timeout", TIMEOUT_DEFAULT_S, 0.5, TIMEOUT_MAX_S)
    seconds = _number(a, "seconds", CONSOLE_DEFAULT_S, 0.0, CONSOLE_MAX_S)
    expression, url = a.get("expression"), a.get("url")
    if action == "evaluate" and not (isinstance(expression, str) and expression.strip()):
        raise ToolError("evaluate needs expression, a JavaScript string", code="bad_args")
    if action == "navigate" and not (isinstance(url, str) and url.strip()):
        raise ToolError("navigate needs url", code="bad_args")
    if action == "screenshot" and a.get("path"):
        from .capture import _shot_path
        _shot_path(a)                                # refuse a bad path before connecting

    deadline = time.monotonic() + remaining(timeout)
    try:
        targets = list_targets(addr, port, deadline)
        if action == "targets":
            pages = [t for t in targets if t.get("type") == "page"]
            return {"host": host, "port": port, "count": len(targets),
                    "targets": [_brief(t) for t in targets],
                    "default": pages[0]["id"] if pages else None}
        target = pick_target(targets, a.get("target"))
        with _connect(addr, port, target, deadline) as s:
            if action == "evaluate":
                result = _evaluate(s, expression, deadline)
            elif action == "console":
                result = _console(s, remaining(seconds) if seconds else 0.0)
            elif action == "screenshot":
                result = _screenshot(s, a)
            else:
                result = _navigate(s, url, deadline, timeout)
    except _Timeout as e:
        raise ToolError(f"browser_devtools {action}: Chrome did not answer within "
                        f"{timeout:g}s", code="timeout") from e
    except _Closed as e:
        raise ToolError(f"the DevTools connection closed during {action} ({e}); the "
                        "tab or browser may have gone away", code="app_not_on_bus") from e
    except _CDPError as e:
        raise ToolError(f"Chrome rejected {e}", code="bad_args",
                        action_status="unknown") from e
    return {"action": action, "target": _brief(target), **result}

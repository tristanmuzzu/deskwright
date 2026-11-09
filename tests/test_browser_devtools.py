#!/usr/bin/env python3
"""`browser_devtools` against a fake Chrome DevTools endpoint. NO desktop.

The fake is an in-process HTTP + websocket server on 127.0.0.1: it answers
/json/list, upgrades /devtools/page/<id> to a websocket, insists on masked
client frames, and scripts CDP replies per method. That covers the stdlib
websocket client (fragmentation, ping, 16- and 64-bit lengths) and every
action, plus refusing non-loopback hosts and honouring timeouts.

    python3 -m pytest -q tests/test_browser_devtools.py
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from deskwright import browser, server
from deskwright.browser import tool_browser_devtools
from deskwright.capture import _INLINE_KEY
from deskwright.errors import ToolError

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class Peer:
    """The server side of one websocket: unmasked frames out, masked in."""

    def __init__(self, conn: socket.socket, fake: FakeChrome):
        self.conn, self.fake, self.buf = conn, fake, b""

    def _exact(self, n: int) -> bytes | None:
        while len(self.buf) < n:
            chunk = self.conn.recv(65536)
            if not chunk:
                return None
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def frame(self, opcode: int, payload: bytes, fin: bool = True) -> None:
        n = len(payload)
        head = bytes([(0x80 if fin else 0) | opcode])
        if n < 126:
            head += bytes([n])
        elif n < 1 << 16:
            head += bytes([126]) + n.to_bytes(2, "big")
        else:
            head += bytes([127]) + n.to_bytes(8, "big")
        self.conn.sendall(head + payload)

    def send(self, obj: dict, fragments: int = 1, ping_first: bool = False) -> None:
        if ping_first:
            self.frame(0x9, b"are-you-there")
        data = json.dumps(obj).encode()
        if fragments <= 1:
            self.frame(0x1, data)
            return
        size = len(data) // fragments + 1
        chunks = [data[i:i + size] for i in range(0, len(data), size)]
        for i, chunk in enumerate(chunks):
            self.frame(0x1 if i == 0 else 0x0, chunk, fin=i == len(chunks) - 1)

    def reply(self, msg: dict, result: dict, **kw) -> None:
        self.send({"id": msg["id"], "result": result}, **kw)

    def event(self, method: str, params: dict) -> None:
        self.send({"method": method, "params": params})

    def recv(self) -> dict | None:
        while True:
            head = self._exact(2)
            if head is None:
                return None
            b0, b1 = head
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = int.from_bytes(self._exact(2), "big")
            elif n == 127:
                n = int.from_bytes(self._exact(8), "big")
            if not b1 & 0x80:
                self.fake.unmasked += 1
                key = None
            else:
                key = self._exact(4)
            payload = self._exact(n) or b""
            if key:
                payload = bytes(c ^ key[i % 4] for i, c in enumerate(payload))
            if opcode == 0x8:
                return None
            if opcode == 0xA:
                self.fake.pongs.append(payload)
                continue
            return json.loads(payload)


class FakeChrome:
    def __init__(self):
        self.lsock = socket.socket()
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(8)
        self.port = self.lsock.getsockname()[1]
        self.methods: dict = {}
        self.calls: list[tuple[str, dict]] = []
        self.pongs: list[bytes] = []
        self.unmasked = 0
        self.ws_host = "127.0.0.1"
        self.targets = [
            {"id": "SW1", "type": "service_worker", "title": "sw", "url": "https://a.test/sw.js"},
            {"id": "P1", "type": "page", "title": "First page", "url": "data:text/html,<h1>hi</h1>"},
            {"id": "P2", "type": "page", "title": "Second page", "url": "https://example.test/two"},
        ]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self.lsock.close()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.lsock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _target_list(self) -> list[dict]:
        return [{**t, "webSocketDebuggerUrl":
                 f"ws://{self.ws_host}:{self.port}/devtools/page/{t['id']}"}
                for t in self.targets]

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            path = lines[0].split(" ")[1]
            headers = {k.strip().lower(): v.strip()
                       for k, _, v in (ln.partition(":") for ln in lines[1:])}
            if path == "/json/list":
                body = json.dumps(self._target_list()).encode()
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             b"Content-Length: " + str(len(body)).encode()
                             + b"\r\nConnection: close\r\n\r\n" + body)
                return
            if headers.get("upgrade", "").lower() != "websocket":
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                return
            assert "origin" not in headers
            accept = base64.b64encode(hashlib.sha1(
                (headers["sec-websocket-key"] + GUID).encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            peer = Peer(conn, self)
            peer.buf = rest
            peer.target = path.rsplit("/", 1)[-1]
            while True:
                try:
                    msg = peer.recv()
                except OSError:
                    return
                if msg is None:
                    return
                self.calls.append((msg["method"], msg.get("params") or {}))
                handler = self.methods.get(msg["method"])
                if handler is None:
                    peer.reply(msg, {})
                else:
                    handler(peer, msg)


@pytest.fixture
def chrome():
    fake = FakeChrome()
    yield fake
    fake.close()


def call(fake: FakeChrome, **kw) -> dict:
    return tool_browser_devtools({"port": fake.port, **kw})


# -- targets -------------------------------------------------------------------
def test_targets_lists_every_target_and_names_the_default_page(chrome):
    out = call(chrome, action="targets")
    assert out["count"] == 3
    assert [t["id"] for t in out["targets"]] == ["SW1", "P1", "P2"]
    assert out["targets"][1] == {"id": "P1", "type": "page", "title": "First page",
                                 "url": "data:text/html,<h1>hi</h1>"}
    assert out["default"] == "P1"


def test_target_selection_by_id_fragment_ambiguity_and_absence(chrome):
    targets = chrome._target_list()
    assert browser.pick_target(targets, None)["id"] == "P1"
    assert browser.pick_target(targets, "SW1")["id"] == "SW1"
    assert browser.pick_target(targets, "example.test")["id"] == "P2"
    with pytest.raises(ToolError) as e:
        browser.pick_target(targets, "page")
    assert e.value.code == "ambiguous_control"
    with pytest.raises(ToolError) as e:
        browser.pick_target(targets, "nothing-like-this")
    assert e.value.code == "window_not_found"


# -- evaluate ------------------------------------------------------------------
def test_evaluate_returns_the_json_value_and_asks_for_it_by_value(chrome):
    chrome.methods["Runtime.evaluate"] = lambda p, m: p.reply(
        m, {"result": {"type": "object", "value": {"a": [1, 2], "b": "x"}}})
    out = call(chrome, action="evaluate", expression="({a:[1,2], b:'x'})", target="Second")
    assert out["value"] == {"a": [1, 2], "b": "x"}
    assert out["target"]["id"] == "P2"
    method, params = chrome.calls[-1]
    assert method == "Runtime.evaluate"
    assert params["returnByValue"] is True and params["awaitPromise"] is True
    assert params["expression"] == "({a:[1,2], b:'x'})"
    assert chrome.unmasked == 0


def test_evaluate_survives_ping_fragments_and_a_64_bit_length(chrome):
    big = "y" * 70_000                         # > 65535 bytes: the 8-byte length form
    chrome.methods["Runtime.evaluate"] = lambda p, m: p.reply(
        m, {"result": {"type": "string", "value": big}}, fragments=3, ping_first=True)
    out = call(chrome, action="evaluate", expression="'y'.repeat(70000)")
    assert out["value"] == big
    assert chrome.pongs == [b"are-you-there"]


def test_evaluate_truncates_a_huge_value(chrome):
    chrome.methods["Runtime.evaluate"] = lambda p, m: p.reply(
        m, {"result": {"type": "string", "value": "z" * 150_000}})
    out = call(chrome, action="evaluate", expression="x")
    assert "value" not in out
    assert len(out["value_truncated"]) == browser.VALUE_MAX_CHARS
    assert out["value_chars"] == 150_002


def test_evaluate_reports_a_thrown_exception_as_bad_args(chrome):
    chrome.methods["Runtime.evaluate"] = lambda p, m: p.reply(m, {
        "result": {"type": "object", "subtype": "error"},
        "exceptionDetails": {"text": "Uncaught", "lineNumber": 0, "columnNumber": 6,
                             "exception": {"description": "ReferenceError: nope is not defined"}}})
    with pytest.raises(ToolError) as e:
        call(chrome, action="evaluate", expression="nope()")
    assert e.value.code == "bad_args"
    assert "ReferenceError: nope is not defined" in str(e.value)
    assert e.value.action_status == "unknown"


def test_evaluate_explains_a_value_chrome_cannot_return(chrome):
    chrome.methods["Runtime.evaluate"] = lambda p, m: p.send(
        {"id": m["id"], "error": {"code": -32000, "message": "Object reference chain is too long"}})
    with pytest.raises(ToolError) as e:
        call(chrome, action="evaluate", expression="window")
    assert e.value.code == "bad_args"
    assert "JSON.stringify" in str(e.value)


def test_evaluate_reports_undefined_and_unserialisable(chrome):
    chrome.methods["Runtime.evaluate"] = lambda p, m: p.reply(
        m, {"result": {"type": "number", "unserializableValue": "NaN"}})
    assert call(chrome, action="evaluate", expression="NaN")["value"] == "NaN"
    chrome.methods["Runtime.evaluate"] = lambda p, m: p.reply(
        m, {"result": {"type": "undefined"}})
    out = call(chrome, action="evaluate", expression="undefined")
    assert out["type"] == "undefined" and out["value"] is None


# -- console -------------------------------------------------------------------
def test_console_returns_replayed_and_live_messages_in_order(chrome):
    def runtime_enable(p, m):
        p.event("Runtime.executionContextCreated", {"context": {"id": 1}})
        p.event("Runtime.consoleAPICalled", {
            "type": "log", "timestamp": 1.0,
            "args": [{"type": "string", "value": "hello-cdp"}, {"type": "number", "value": 3}],
            "stackTrace": {"callFrames": [{"url": "data:text/html,x", "lineNumber": 0}]}})
        p.reply(m, {})

    def log_enable(p, m):
        p.event("Log.entryAdded", {"entry": {"source": "network", "level": "error",
                                             "text": "Failed to load resource", "url": "u"}})
        p.reply(m, {})

        def later():
            time.sleep(0.15)
            p.event("Runtime.exceptionThrown", {"timestamp": 2.0, "exceptionDetails": {
                "text": "Uncaught", "url": "data:x", "lineNumber": 4,
                "exception": {"description": "TypeError: boom"}}})
        threading.Thread(target=later, daemon=True).start()

    chrome.methods["Runtime.enable"] = runtime_enable
    chrome.methods["Log.enable"] = log_enable
    t0 = time.monotonic()
    out = call(chrome, action="console", seconds=0.6)
    assert 0.55 <= time.monotonic() - t0 < 3
    texts = [(m["source"], m["level"], m["text"], m["buffered"]) for m in out["messages"]]
    assert texts == [
        ("console", "log", "hello-cdp 3", True),
        ("log:network", "error", "Failed to load resource", True),
        ("exception", "error", "TypeError: boom", False),
    ]
    assert out["messages"][2]["line"] == 4


def test_console_seconds_is_bounded():
    with pytest.raises(ToolError) as e:
        tool_browser_devtools({"port": 9, "action": "console", "seconds": 31})
    assert e.value.code == "bad_args"


# -- screenshot ----------------------------------------------------------------
def _png(width: int = 40, height: int = 30) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


def test_screenshot_writes_the_png_and_inlines_it(chrome, tmp_path):
    png = _png()
    chrome.methods["Page.captureScreenshot"] = lambda p, m: p.reply(
        m, {"data": base64.b64encode(png).decode()})
    dest = tmp_path / "page.png"
    out = call(chrome, action="screenshot", path=str(dest))
    assert dest.read_bytes() == png
    assert out["path"] == str(dest) and out["bytes"] == len(png)
    assert out["dimensions"] == "40x30"
    assert out[_INLINE_KEY]["media_type"] == "image/jpeg"
    out = call(chrome, action="screenshot", path=str(dest), inline=False)
    assert _INLINE_KEY not in out


def test_screenshot_refuses_a_non_png_path_before_connecting(chrome, tmp_path):
    with pytest.raises(ToolError) as e:
        call(chrome, action="screenshot", path=str(tmp_path / "x.sh"))
    assert e.value.code == "bad_args"
    assert chrome.calls == []


def test_screenshot_failure_is_capture_failed(chrome, tmp_path):
    chrome.methods["Page.captureScreenshot"] = lambda p, m: p.send(
        {"id": m["id"], "error": {"code": -32000, "message": "Unable to capture screenshot"}})
    with pytest.raises(ToolError) as e:
        call(chrome, action="screenshot", path=str(tmp_path / "x.png"))
    assert e.value.code == "capture_failed"


# -- navigate ------------------------------------------------------------------
def _location(p, m):
    p.reply(m, {"result": {"type": "object", "value": {"url": "https://example.test/next",
                                                       "title": "Next"}}})


def test_navigate_waits_for_the_load_event(chrome):
    def navigate(p, m):
        p.reply(m, {"frameId": "F", "loaderId": "L"})

        def later():
            time.sleep(0.2)
            p.event("Page.loadEventFired", {"timestamp": 3.0})
        threading.Thread(target=later, daemon=True).start()

    chrome.methods["Page.navigate"] = navigate
    chrome.methods["Runtime.evaluate"] = _location
    t0 = time.monotonic()
    out = call(chrome, action="navigate", url="https://example.test/next")
    assert time.monotonic() - t0 >= 0.18
    assert out["loaded"] is True
    assert out["url"] == "https://example.test/next" and out["title"] == "Next"
    assert [c[0] for c in chrome.calls] == ["Page.enable", "Page.navigate", "Runtime.evaluate"]


def test_navigate_error_text_is_reported(chrome):
    chrome.methods["Page.navigate"] = lambda p, m: p.reply(
        m, {"frameId": "F", "errorText": "net::ERR_NAME_NOT_RESOLVED"})
    with pytest.raises(ToolError) as e:
        call(chrome, action="navigate", url="https://nowhere.invalid/")
    assert e.value.code == "verification_failed"
    assert "ERR_NAME_NOT_RESOLVED" in str(e.value)


def test_navigate_without_a_load_event_times_out_in_bounds(chrome):
    chrome.methods["Page.navigate"] = lambda p, m: p.reply(m, {"frameId": "F", "loaderId": "L"})
    t0 = time.monotonic()
    with pytest.raises(ToolError) as e:
        call(chrome, action="navigate", url="https://slow.test/", timeout=0.6)
    assert e.value.code == "timeout"
    assert time.monotonic() - t0 < 2


def test_navigate_needs_a_url():
    with pytest.raises(ToolError) as e:
        tool_browser_devtools({"port": 9, "action": "navigate"})
    assert e.value.code == "bad_args"


# -- loopback only, and bounded ------------------------------------------------
@pytest.mark.parametrize("host", ["example.com", "10.0.0.1", "0.0.0.0", "127.0.0.2.nip.io", ""])
def test_non_loopback_hosts_are_refused_before_any_connection(host, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("tried to connect")
    monkeypatch.setattr(socket, "create_connection", boom)
    with pytest.raises(ToolError) as e:
        tool_browser_devtools({"port": 9222, "action": "targets", "host": host})
    assert e.value.code == "bad_args"
    assert "only talks to this machine" in str(e.value)


def test_localhost_and_loopback_spellings_are_accepted(chrome):
    for host in ("localhost", "127.0.0.1", "LOCALHOST"):
        assert call(chrome, action="targets", host=host)["count"] == 3


def test_a_websocket_url_off_this_machine_is_refused(chrome):
    chrome.ws_host = "evil.example"
    with pytest.raises(ToolError) as e:
        call(chrome, action="evaluate", expression="1")
    assert e.value.code == "app_not_on_bus"
    assert "refusing to connect" in str(e.value)
    assert chrome.calls == []


def test_a_configured_proxy_is_never_used(chrome, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    assert call(chrome, action="targets")["count"] == 3


def test_connection_refused_says_how_to_launch_the_browser():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                   # nothing listens there now
    with pytest.raises(ToolError) as e:
        tool_browser_devtools({"port": port, "action": "targets"})
    assert e.value.code == "app_not_on_bus"
    assert f"--remote-debugging-port={port}" in str(e.value)
    assert "--user-data-dir" in str(e.value)


def test_a_silent_browser_times_out_in_bounds(chrome):
    chrome.methods["Runtime.evaluate"] = lambda p, m: None        # never answers
    t0 = time.monotonic()
    with pytest.raises(ToolError) as e:
        call(chrome, action="evaluate", expression="1", timeout=0.5)
    assert e.value.code == "timeout"
    assert time.monotonic() - t0 < 1.5


def test_timeout_is_bounded():
    for bad in (0, 61, "10", True):
        with pytest.raises(ToolError) as e:
            tool_browser_devtools({"port": 9, "action": "targets", "timeout": bad})
        assert e.value.code == "bad_args"


def test_mask_round_trips():
    key = b"\x01\x02\x03\x04"
    for data in (b"", b"a", b"abcd", bytes(range(256)) * 300):
        assert browser._mask(browser._mask(data, key), key) == data
    assert browser._mask(b"\x00\x00\x00\x00\x00", key) == b"\x01\x02\x03\x04\x01"


# -- registration --------------------------------------------------------------
def test_the_tool_is_registered_as_acting():
    tool = next(t for t in server.TOOLS if t["name"] == "browser_devtools")
    assert tool["inputSchema"]["required"] == ["port", "action"]
    assert "browser_devtools" not in server._READ_ONLY_TOOLS
    with pytest.raises(ToolError):
        server.validate_tool_args("browser_devtools", {"port": 9222, "action": "dump"})
    server.validate_tool_args("browser_devtools", {"port": 9222, "action": "targets"})


def test_a_non_loopback_host_through_the_server_gets_the_explanation(capsys, monkeypatch):
    """The schema leaves host open so the handler's refusal, which says what to
    use instead, is what the model reads (live run 2026-11-09: an enum made the
    model read a generic 'invalid choices' plus the whole schema instead)."""
    # No desktop and no journal: the halt probe and the journal are stubbed.
    monkeypatch.setattr(server, "halt_active", lambda: False)
    monkeypatch.setattr(server, "journal_record", lambda *a, **k: None)
    server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "browser_devtools",
        "arguments": {"port": 9222, "action": "targets", "host": "example.com"}}})
    reply = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    text = reply["result"]["content"][0]["text"]
    assert reply["result"]["isError"] is True
    assert text.startswith("[bad_args] browser_devtools only talks to this machine")

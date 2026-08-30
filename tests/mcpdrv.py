#!/usr/bin/env python3
"""Speak MCP to a fresh mcp_server.py over stdio, from a test or a shell.

The server Claude Code is holding open is the version that was on disk when the
session started, so every change here is invisible until a restart. This driver
launches its own copy, which makes an edit measurable in the same minute it is
written -- and makes an A/B against `git stash` possible at all.

    ./mcpdrv.py screenshot '{"path":"/tmp/a.png","inline":true}'
    ./mcpdrv.py --raw list_windows '{}'

Programmatic use:

    with Server() as s:
        out = s.call("screenshot", {"path": "/tmp/a.png"})
        out["_elapsed"]      # wall clock of the round trip, seconds
        out["_blocks"]       # ['text', 'image'] -- what came back
        out["_image_bytes"]  # decoded size of the first image block, or 0
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "mcp_server.py"


class Server:
    """One long-lived server subprocess, spoken to in JSON-RPC."""

    def __init__(self, server: Path | str = SERVER, env: dict | None = None):
        self.server = str(server)
        self.env = {**os.environ, **(env or {})}
        self.proc: subprocess.Popen | None = None
        self._id = 0

    def __enter__(self) -> Server:
        self.proc = subprocess.Popen(
            [sys.executable, self.server],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=self.env,
        )
        self._rpc("initialize", {"protocolVersion": "2025-06-18",
                                 "capabilities": {}, "clientInfo": {"name": "mcpdrv"}})
        self._notify("notifications/initialized")
        return self

    def __exit__(self, *_exc) -> None:
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()

    # -- transport ---------------------------------------------------------
    def _notify(self, method: str, params: dict | None = None) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()

    def _rpc(self, method: str, params: dict | None = None, timeout: float = 180.0):
        assert self.proc and self.proc.stdin and self.proc.stdout
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method,
               "params": params or {}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            line = self.proc.stdout.readline()
            if not line:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"server died: {err[-2000:]}")
            try:
                got = json.loads(line)
            except json.JSONDecodeError:
                continue                      # stray output is not our business
            if got.get("id") == self._id:
                return got
            if time.monotonic() > deadline:
                raise TimeoutError(f"{method} did not answer in {timeout}s")

    # -- the bit tests use -------------------------------------------------
    def tools(self) -> list[dict]:
        return self._rpc("tools/list")["result"]["tools"]

    def call(self, name: str, args: dict | None = None, timeout: float = 180.0) -> dict:
        t0 = time.monotonic()
        got = self._rpc("tools/call", {"name": name, "arguments": args or {}},
                        timeout=timeout)
        elapsed = time.monotonic() - t0
        if "error" in got:
            return {"_error": got["error"], "_elapsed": elapsed, "_blocks": []}

        result = got.get("result") or {}
        content = result.get("content") or []
        blocks = [c.get("type") for c in content]
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        images = [c for c in content if c.get("type") == "image"]

        try:
            payload = json.loads(text) if text.strip().startswith(("{", "[")) else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {"_list": payload}

        payload["_elapsed"] = elapsed
        payload["_blocks"] = blocks
        payload["_text"] = text
        payload["_is_error"] = bool(result.get("isError"))
        payload["_images"] = len(images)
        # MCP ImageContent is flat: {type, data, mimeType}. This driver used to
        # read the Anthropic API's nested source{} instead, which meant it
        # validated the server's wrong wire format as enthusiastically as the
        # server produced it. The host was the only thing that noticed.
        first = images[0] if images else {}
        payload["_image_bytes"] = len(base64.b64decode(first["data"])) if images else 0
        payload["_image_b64_len"] = len(first.get("data", "")) if images else 0
        payload["_media_type"] = first.get("mimeType") if images else None
        payload["_image_data"] = first.get("data") if images else None
        return payload


def main() -> int:
    args = sys.argv[1:]
    raw = "--raw" in args
    args = [a for a in args if a != "--raw"]
    if not args:
        print(__doc__)
        return 2
    name = args[0]
    params = json.loads(args[1]) if len(args) > 1 else {}
    with Server() as s:
        if name == "tools":
            for t in s.tools():
                print(f"{t['name']:24s} {t.get('description','')[:100]}")
            return 0
        out = s.call(name, params)
    if not raw:
        out.pop("_image_data", None)
    print(json.dumps(out, indent=1)[:8000])
    return 1 if out.get("_is_error") or out.get("_error") else 0


if __name__ == "__main__":
    raise SystemExit(main())

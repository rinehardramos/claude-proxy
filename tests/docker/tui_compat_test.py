#!/usr/bin/env python3
"""TUI compatibility test — verifies claude-proxy handles opencode + gemini-cli traffic.

Architecture:
  1. Mock backend (records requests, returns stub responses)
  2. claude-proxy proxy (configured to forward to mock backend, TLS off)
  3. Test client (sends requests in Anthropic + Gemini formats)

Tests correct proxying only — redaction is the responsibility of plugins,
not the proxy core.
"""
from __future__ import annotations

import http.client
import http.server
import json
import os
import sys
import threading
import time

# ── Mock backend ──────────────────────────────────────────────────────────

_received: list[dict] = []  # thread-safe enough for sequential tests
_received_lock = threading.Lock()


class MockBackendHandler(http.server.BaseHTTPRequestHandler):
    """Records request bodies and returns stub API responses."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body)
        except Exception:
            payload = None

        with _received_lock:
            _received.append({
                "path": self.path,
                "body": body.decode("utf-8", errors="replace"),
                "payload": payload,
                "headers": dict(self.headers),
            })

        # Return a stub response appropriate to the API format
        if "/v1/messages" in self.path:
            resp = {
                "id": "msg_stub",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "stub response"}],
                "model": "stub",
                "stop_reason": "end_turn",
            }
        elif "/v1beta/models/" in self.path or "/v1/models/" in self.path:
            resp = {
                "candidates": [{"content": {"parts": [{"text": "stub response"}]}}],
            }
        else:
            resp = {"status": "ok"}

        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.do_POST()

    def log_message(self, fmt, *args):
        pass


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Test helpers ──────────────────────────────────────────────────────────

def _clear():
    with _received_lock:
        _received.clear()


def _last_payload() -> dict | None:
    with _received_lock:
        return _received[-1]["payload"] if _received else None


def _last_body() -> str:
    with _received_lock:
        return _received[-1]["body"] if _received else ""


def _last_path() -> str:
    with _received_lock:
        return _received[-1]["path"] if _received else ""


def _send(proxy_port: int, method: str, path: str,
          payload: dict | None = None) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=10)
    body = json.dumps(payload).encode() if payload else None
    headers = {"Content-Type": "application/json"}
    if body:
        headers["Content-Length"] = str(len(body))
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    resp.read()  # consume body
    conn.close()
    return resp


# ── Payload builders (Anthropic format / opencode) ────────────────────────

def _anthropic_payload(user_text: str, stream: bool = False) -> dict:
    return {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "stream": stream,
        "messages": [{"role": "user", "content": user_text}],
    }


# ── Payload builders (Gemini format / gemini-cli) ─────────────────────────

def _gemini_payload(user_text: str) -> dict:
    return {
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1024},
    }


def _gemini_multi_turn(*texts: str) -> dict:
    contents = []
    for i, text in enumerate(texts):
        role = "user" if i % 2 == 0 else "model"
        contents.append({"role": role, "parts": [{"text": text}]})
    return {"contents": contents}


# ── Test vectors ──────────────────────────────────────────────────────────

class R:
    """Result tracker."""
    def __init__(self, name: str, cat: str):
        self.name = name
        self.cat = cat
        self.ok = False
        self.detail = ""
        self.sev = ""

    def p(self, detail: str = "") -> "R":
        self.ok = True
        self.detail = detail
        return self

    def f(self, detail: str, sev: str = "HIGH") -> "R":
        self.ok = False
        self.detail = detail
        self.sev = sev
        return self


def run_tests(proxy_port: int) -> list[R]:
    results: list[R] = []

    # ── opencode (Anthropic format) ──────────────────────────────────

    # 1. Clean payload forwarded intact
    r = R("opencode-clean-forward", "opencode")
    _clear()
    _send(proxy_port, "POST", "/v1/messages",
          _anthropic_payload("Hello, world"))
    p = _last_payload()
    if p and p.get("messages", [{}])[0].get("content") == "Hello, world":
        r.p("clean text forwarded intact")
    else:
        r.f("clean text modified or lost")
    results.append(r)

    # 2. stream=true flag preserved
    r = R("opencode-stream-flag", "opencode")
    _clear()
    _send(proxy_port, "POST", "/v1/messages",
          _anthropic_payload("test", stream=True))
    p = _last_payload()
    if p and p.get("stream") is True:
        r.p("stream=true preserved")
    else:
        r.f("stream flag lost or modified")
    results.append(r)

    # ── gemini-cli (Gemini format) ───────────────────────────────────

    # 3. Clean Gemini payload forwarded intact
    r = R("gemini-clean-forward", "gemini")
    _clear()
    _send(proxy_port, "POST",
          "/v1beta/models/gemini-2.5-pro:generateContent",
          _gemini_payload("Hello from Gemini"))
    p = _last_payload()
    text = (p or {}).get("contents", [{}])[0].get(
        "parts", [{}])[0].get("text", "")
    if text == "Hello from Gemini":
        r.p("clean Gemini text forwarded intact")
    else:
        r.f("clean Gemini text modified or lost")
    results.append(r)

    # 4. Multi-turn Gemini: all contents[] entries forwarded
    r = R("gemini-multi-turn", "gemini")
    _clear()
    _send(proxy_port, "POST",
          "/v1beta/models/gemini-2.5-pro:generateContent",
          _gemini_multi_turn("turn one", "model reply", "turn two"))
    p = _last_payload()
    if p and len(p.get("contents", [])) == 3:
        r.p("all 3 contents[] entries forwarded")
    else:
        count = len((p or {}).get("contents", []))
        r.f("expected 3 contents, got %d" % count)
    results.append(r)

    # 5. /v1/models/ path also detected as Gemini and forwarded
    r = R("gemini-v1-models-path", "gemini")
    _clear()
    _send(proxy_port, "POST",
          "/v1/models/gemini-2.5-pro:generateContent",
          _gemini_payload("v1 path test"))
    p = _last_payload()
    path = _last_path()
    if p and "/v1/models/" in path:
        r.p("/v1/models/ path routed correctly")
    else:
        r.f("/v1/models/ path not forwarded to backend")
    results.append(r)

    # ── Cross-cutting ────────────────────────────────────────────────

    # 6. Health endpoint returns JSON with status key
    r = R("health-endpoint", "infra")
    conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
    conn.request("GET", "/status")
    resp = conn.getresponse()
    try:
        data = json.loads(resp.read())
    except Exception:
        data = {}
    conn.close()
    if resp.status == 200 and "status" in data:
        r.p("status=" + str(data["status"]))
    else:
        r.f("health returned " + str(resp.status))
    results.append(r)

    # 7. Non-API path forwarded transparently
    r = R("non-api-forward", "infra")
    _clear()
    _send(proxy_port, "POST", "/some/other/path", {"foo": "bar"})
    body = _last_body()
    if "bar" in body:
        r.p("non-API path forwarded transparently")
    else:
        r.f("non-API path not forwarded")
    results.append(r)

    return results


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.join(here, "..", "..")
    sys.path.insert(0, root)

    import proxy as px

    # Start mock backend
    mock_port = _free_port()
    mock_server = http.server.HTTPServer(
        ("127.0.0.1", mock_port), MockBackendHandler)
    mock_thread = threading.Thread(
        target=mock_server.serve_forever, daemon=True)
    mock_thread.start()

    # Configure proxy to forward to mock backend (no TLS)
    px.UPSTREAM_HOST = "127.0.0.1"
    px.UPSTREAM_PORT = mock_port
    px.UPSTREAM_TLS = False
    px.GEMINI_UPSTREAM_HOST = "127.0.0.1"
    px.GEMINI_UPSTREAM_PORT = mock_port
    px.GEMINI_UPSTREAM_TLS = False

    # Start proxy
    proxy_port = _free_port()
    proxy_server = px.ThreadedHTTPServer(
        ("127.0.0.1", proxy_port), px.ProxyHandler)
    proxy_thread = threading.Thread(
        target=proxy_server.serve_forever, daemon=True)
    proxy_thread.start()
    time.sleep(0.3)

    print("=" * 60)
    print("  claude-proxy TUI Compatibility Test")
    print("  proxy=%d  mock_backend=%d" % (proxy_port, mock_port))
    print("=" * 60)
    print()

    results = run_tests(proxy_port)

    # Report
    failures = 0
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        sev = " [%s]" % r.sev if r.sev else ""
        print("  %s  %-10s %-35s %s%s" % (
            status, r.cat, r.name, r.detail, sev))
        if not r.ok:
            failures += 1

    print()
    print("=" * 60)
    if failures:
        print("  %d FAILED out of %d" % (failures, len(results)))
    else:
        print("  All %d tests passed" % len(results))
    print("=" * 60)

    # Cleanup
    proxy_server.shutdown()
    mock_server.shutdown()

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

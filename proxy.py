"""
claude-proxy — extensible HTTP proxy for Claude Code.

Sits between Claude Code and the Anthropic API. Provides:
  - Plugin system (on_outbound / on_inbound hooks)
  - File-based sideload mechanism for external agent injection
  - SSE response injection (new content blocks synthesised before message_stop)

Ported from leak-guard's proxy.py, stripped of redaction logic, extended
with the plugin/sideload architecture described in the design spec.
"""

from __future__ import annotations

import copy
import http.client
import http.server
import importlib.util
import json
import os
import signal
import ssl
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

# ── Constants ──────────────────────────────────────────────────────────────

LISTEN_PORT = int(os.environ.get("CLAUDE_PROXY_PORT", "18019"))
UPSTREAM_HOST = "api.anthropic.com"
UPSTREAM_PORT = 443
STATE_DIR = Path("~/.claude/claude-proxy").expanduser()
PLUGINS_DIR = STATE_DIR / "plugins"
PLUGINS_TOML = STATE_DIR / "plugins.toml"
SIDELOAD_DIR = STATE_DIR / "sideload"
SIDELOAD_OUTBOUND = SIDELOAD_DIR / "outbound"
SIDELOAD_INBOUND = SIDELOAD_DIR / "inbound"
PID_FILE = STATE_DIR / "proxy.pid"
LOG_FILE = STATE_DIR / "proxy.log"
_SIDELOAD_TTL = 300        # discard sideload files older than 5 minutes
_INACTIVITY_TIMEOUT = 4 * 3600  # auto-exit after 4 hours idle


# ── Minimal TOML subset parser ─────────────────────────────────────────────

def _parse_plugins_toml(text: str) -> dict:
    """Parse the TOML subset used by plugins.toml.

    Handles:
      - enabled = ["a", "b"]         top-level list
      - [section]                    section headers
      - key = "value"                string values inside sections
      - # comments                   ignored
    """
    result: dict[str, Any] = {}
    current_section: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("["):
            current_section = line.strip("[]").strip()
            result.setdefault(current_section, {})
            continue

        if "=" not in line:
            continue

        key, _, raw_value = line.partition("=")
        key = key.strip()
        raw_value = raw_value.strip()

        if raw_value.startswith('"') and raw_value.endswith('"'):
            value: Any = raw_value[1:-1]
        elif raw_value.startswith("["):
            # Simple inline list: ["a", "b"] or ['a', 'b']
            inner = raw_value.strip("[]")
            value = [item.strip().strip('"\'') for item in inner.split(",") if item.strip().strip('"\'')]
        else:
            value = raw_value

        if current_section is None:
            result[key] = value
        else:
            result[current_section][key] = value

    return result


# ── Plugin loader ──────────────────────────────────────────────────────────

def load_plugins(
    plugins_dir: Path = PLUGINS_DIR,
    config_file: Path = PLUGINS_TOML,
) -> list:
    """Discover, import, and configure enabled plugins.

    Only plugins listed in plugins.toml's `enabled` array are loaded.
    Each plugin's config section is passed to configure() if defined.
    Import errors are caught and logged — they never crash the proxy.
    """
    plugins: list = []
    enabled: list[str] = []
    plugin_configs: dict[str, dict] = {}

    if config_file.exists():
        try:
            text = config_file.read_text(encoding="utf-8")
            config = _parse_plugins_toml(text)
            enabled = config.get("enabled", [])
            plugin_configs = {k: v for k, v in config.items() if isinstance(v, dict)}
        except Exception as exc:
            print(f"[proxy] Failed to read {config_file}: {exc}", file=sys.stderr)

    if not plugins_dir.exists():
        return plugins

    for py_file in sorted(plugins_dir.glob("*.py")):
        plugin_name = py_file.stem
        if plugin_name not in enabled:
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"_proxy_plugin_{plugin_name}", py_file
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]

            if hasattr(mod, "configure"):
                mod.configure(plugin_configs.get(plugin_name, {}))

            plugins.append(mod)
            label = mod.plugin_info()["name"] if hasattr(mod, "plugin_info") else plugin_name
            print(f"[proxy] loaded plugin: {label}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"[proxy] Failed to load plugin '{plugin_name}': {exc}", file=sys.stderr)

    return plugins


# ── Sideload ───────────────────────────────────────────────────────────────

def load_sideload(directory: Path, ttl: int = _SIDELOAD_TTL) -> list[dict]:
    """Load and consume all .json files from *directory*.

    Files are sorted by name before processing (timestamp-prefix ordering).
    Files whose mtime is older than *ttl* seconds are deleted and skipped
    (stale protection — external agent may have crashed).

    Returns a list of parsed dicts.
    """
    items: list[dict] = []
    if not directory.exists():
        return items

    now = time.time()
    for json_file in sorted(directory.glob("*.json")):
        try:
            mtime = json_file.stat().st_mtime
            if now - mtime > ttl:
                json_file.unlink(missing_ok=True)
                continue
            data = json.loads(json_file.read_text(encoding="utf-8"))
            json_file.unlink(missing_ok=True)
            items.append(data)
        except Exception as exc:
            print(f"[proxy] sideload error on {json_file.name}: {exc}", file=sys.stderr)

    return items


def inject_outbound(payload: dict, items: list[dict]) -> dict:
    """Inject outbound sideload items into the request payload.

    Returns a deep copy of *payload* with injections applied.
    Supported targets:
      "system"       — append to the system field (str, list, or missing)
      "user_message" — append a text block to the last user message
      "user_turn"    — append a new user message to messages[]
    """
    if not items:
        return payload
    payload = copy.deepcopy(payload)

    for item in items:
        target = item.get("target", "system")
        content = item.get("content", "")

        if target == "system":
            system = payload.get("system")
            if system is None:
                payload["system"] = content
            elif isinstance(system, str):
                payload["system"] = system + "\n\n" + content
            elif isinstance(system, list):
                payload["system"] = system + [{"type": "text", "text": content}]

        elif target == "user_message":
            messages = payload.get("messages", [])
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    c = msg.get("content", [])
                    if isinstance(c, str):
                        msg["content"] = [
                            {"type": "text", "text": c},
                            {"type": "text", "text": content},
                        ]
                    elif isinstance(c, list):
                        msg["content"] = c + [{"type": "text", "text": content}]
                    break

        elif target == "user_turn":
            messages = payload.get("messages", [])
            messages.append({"role": "user", "content": content})
            payload["messages"] = messages

    return payload


# ── SSE injection ──────────────────────────────────────────────────────────

def build_sse_content_block(index: int, text: str) -> bytes:
    """Synthesise SSE bytes for a complete new text content block.

    Emits: content_block_start → content_block_delta → content_block_stop
    as defined by the Anthropic streaming protocol.
    """
    start = json.dumps({
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "text", "text": ""},
    })
    delta = json.dumps({
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "text_delta", "text": text},
    })
    stop = json.dumps({"type": "content_block_stop", "index": index})

    return (
        f"event: content_block_start\ndata: {start}\n\n"
        f"event: content_block_delta\ndata: {delta}\n\n"
        f"event: content_block_stop\ndata: {stop}\n\n"
    ).encode()


def process_sse_stream(
    resp_lines,
    write_fn: Callable[[bytes], None],
    plugins: list,
    request_summary: dict,
    inbound_sideload: list[str],
) -> None:
    """Stream SSE events to the client, injecting content blocks before message_stop.

    All events except message_stop are forwarded immediately (zero added latency).
    When message_stop is seen:
      1. Assemble full response text from buffered content_block_delta events.
      2. Call on_inbound() for each plugin; collect non-None returns.
      3. Emit synthesised content blocks (inbound sideload first, then plugins).
      4. Forward the original message_stop event.

    Args:
        resp_lines:       Iterable of bytes lines from the upstream response
                          (use ``iter(resp.readline, b"")`` for real responses).
        write_fn:         Callable that writes bytes to the client.
        plugins:          Loaded plugin modules (may have on_inbound hook).
        request_summary:  {"user_text": str, "model": str, "path": str}
        inbound_sideload: Pre-loaded inbound sideload text strings.
    """
    last_block_index = -1
    response_text_parts: list[str] = []
    event_lines: list[bytes] = []
    current_event_type: str | None = None

    for raw_line in resp_lines:
        event_lines.append(raw_line)
        stripped = raw_line.strip()

        if stripped.startswith(b"event:"):
            current_event_type = stripped[6:].strip().decode(errors="replace")

        elif stripped.startswith(b"data:"):
            data_str = stripped[5:].strip().decode(errors="replace")

            if current_event_type == "content_block_start":
                try:
                    d = json.loads(data_str)
                    last_block_index = max(last_block_index, d.get("index", -1))
                except Exception:
                    pass

            elif current_event_type == "content_block_delta":
                try:
                    d = json.loads(data_str)
                    delta = d.get("delta", {})
                    if delta.get("type") == "text_delta":
                        response_text_parts.append(delta.get("text", ""))
                except Exception:
                    pass

        elif stripped == b"":
            # End of SSE event — process the buffered lines
            event_bytes = b"".join(event_lines)
            event_lines = []

            if current_event_type == "message_stop":
                full_response = "".join(response_text_parts)

                # Collect inbound injections: sideload first, then plugins
                all_inbound: list[str] = list(inbound_sideload)
                for plugin in plugins:
                    if not hasattr(plugin, "on_inbound"):
                        continue
                    try:
                        extra = plugin.on_inbound(full_response, request_summary)
                        if extra is not None:
                            all_inbound.append(extra)
                    except Exception as exc:
                        print(f"[proxy] plugin on_inbound error: {exc}", file=sys.stderr)

                for text in all_inbound:
                    last_block_index += 1
                    write_fn(build_sse_content_block(last_block_index, text))

                write_fn(event_bytes)
            else:
                write_fn(event_bytes)

            current_event_type = None


# ── Runtime state ──────────────────────────────────────────────────────────

_last_activity = time.time()


# ── HTTP server ────────────────────────────────────────────────────────────

class ThreadedHTTPServer(http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def process_request(self, request, client_address):
        t = threading.Thread(target=self._handle, args=(request, client_address))
        t.daemon = True
        t.start()

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            pass
        finally:
            self.shutdown_request(request)


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    plugins: list = []  # set by main() after load_plugins()

    # ── routing ───────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path == "/proxy-status":
            self._health()
        else:
            self._forward("GET")

    def do_POST(self):
        self._forward("POST")

    # ── health ────────────────────────────────────────────────────────────

    def _health(self):
        plugin_names: list[str] = []
        for p in self.plugins:
            if hasattr(p, "plugin_info"):
                try:
                    plugin_names.append(p.plugin_info()["name"])
                except Exception:
                    pass

        pending = 0
        for d in (SIDELOAD_OUTBOUND, SIDELOAD_INBOUND):
            if d.exists():
                pending += sum(1 for _ in d.glob("*.json"))

        body = json.dumps({
            "status": "ok",
            "plugins": plugin_names,
            "sideload_pending": pending,
        }).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── proxy core ────────────────────────────────────────────────────────

    def _forward(self, method: str):
        global _last_activity
        _last_activity = time.time()

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b""

        body = raw_body
        payload: dict | None = None

        if raw_body:
            try:
                payload = json.loads(raw_body)
            except (json.JSONDecodeError, ValueError):
                payload = None

        request_summary: dict = {"path": self.path, "model": "", "user_text": ""}
        inbound_sideload: list[str] = []

        if payload is not None:
            request_summary["model"] = payload.get("model", "")
            request_summary["user_text"] = _extract_user_text(payload)

            # Load + consume sideload files
            outbound_items = load_sideload(SIDELOAD_OUTBOUND)
            inbound_items = load_sideload(SIDELOAD_INBOUND)
            inbound_sideload = [
                item["content"] for item in inbound_items if item.get("content")
            ]

            # Inject outbound sideload into payload
            payload = inject_outbound(payload, outbound_items)

            # Call on_outbound plugins
            for plugin in self.plugins:
                if not hasattr(plugin, "on_outbound"):
                    continue
                try:
                    result = plugin.on_outbound(copy.deepcopy(payload))
                    if result is not None:
                        payload = result
                except Exception as exc:
                    print(f"[proxy] plugin on_outbound error: {exc}", file=sys.stderr)

            body = json.dumps(payload).encode()

        # Build upstream headers (strip hop-by-hop)
        skip = {"host", "transfer-encoding", "content-length"}
        upstream_headers: dict[str, str] = {
            k: v for k, v in self.headers.items() if k.lower() not in skip
        }
        upstream_headers["Host"] = UPSTREAM_HOST
        upstream_headers["Content-Length"] = str(len(body))

        is_stream = bool(payload.get("stream", False)) if payload else False

        # Forward to Anthropic
        try:
            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(UPSTREAM_HOST, UPSTREAM_PORT, context=ctx)
            conn.request(method, self.path, body=body, headers=upstream_headers)
            resp = conn.getresponse()
        except Exception:
            self._send_502()
            return

        # Send response status + headers to client
        self.send_response(resp.status)
        skip_resp = {"transfer-encoding", "content-length"}
        for key, val in resp.getheaders():
            if key.lower() not in skip_resp:
                self.send_header(key, val)

        if is_stream:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def write_chunk(data: bytes) -> None:
                self.wfile.write(f"{len(data):x}\r\n".encode())
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            try:
                process_sse_stream(
                    resp_lines=iter(resp.readline, b""),
                    write_fn=write_chunk,
                    plugins=self.plugins,
                    request_summary=request_summary,
                    inbound_sideload=inbound_sideload,
                )
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass

        else:
            resp_body = resp.read()

            # Non-streaming: inject into JSON content array if needed
            if inbound_sideload or any(hasattr(p, "on_inbound") for p in self.plugins):
                resp_body = _inject_non_streaming(
                    resp_body, self.plugins, request_summary, inbound_sideload
                )

            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)

        conn.close()

    def _send_502(self):
        body = b'{"error": "bad_gateway"}'
        self.send_response(502)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # silence default access log


# ── Helpers ────────────────────────────────────────────────────────────────

def _extract_user_text(payload: dict) -> str:
    """Return the last user message text, skipping <system-reminder> blocks."""
    for msg in reversed(payload.get("messages", [])):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in reversed(content):
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if not text.startswith("<system-reminder>"):
                        return text
    return ""


def _inject_non_streaming(
    resp_body: bytes,
    plugins: list,
    request_summary: dict,
    inbound_sideload: list[str],
) -> bytes:
    """Inject content into a non-streaming JSON response body."""
    try:
        resp_json = json.loads(resp_body)
        content = resp_json.get("content", [])
        last_index = max(
            (b.get("index", i) for i, b in enumerate(content)), default=-1
        )
        full_response = "".join(
            b.get("text", "") for b in content if b.get("type") == "text"
        )

        all_inbound: list[str] = list(inbound_sideload)
        for plugin in plugins:
            if not hasattr(plugin, "on_inbound"):
                continue
            try:
                extra = plugin.on_inbound(full_response, request_summary)
                if extra is not None:
                    all_inbound.append(extra)
            except Exception as exc:
                print(f"[proxy] plugin on_inbound error: {exc}", file=sys.stderr)

        for text in all_inbound:
            last_index += 1
            content.append({"type": "text", "text": text, "index": last_index})

        resp_json["content"] = content
        return json.dumps(resp_json).encode()
    except Exception:
        return resp_body  # leave unchanged on parse failure


# ── PID management ─────────────────────────────────────────────────────────

def _write_pid(pid: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid))


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def _cleanup_pid() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def is_proxy_running() -> bool:
    pid = _read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        _cleanup_pid()
        return False
    except PermissionError:
        return True  # process exists but we can't signal it


# ── Inactivity watchdog ────────────────────────────────────────────────────

def _inactivity_watchdog(server) -> None:
    while True:
        time.sleep(60)
        if time.time() - _last_activity > _INACTIVITY_TIMEOUT:
            print("[proxy] shutting down: inactivity timeout", file=sys.stderr, flush=True)
            _cleanup_pid()
            server.shutdown()
            break


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(prog="claude-proxy")
    parser.add_argument("--daemon", action="store_true", help="Run in background")
    parser.add_argument("--port", type=int, default=LISTEN_PORT)
    args = parser.parse_args()
    port = args.port

    if args.daemon:
        pid = os.fork()
        if pid > 0:
            _write_pid(pid)
            print(f"[proxy] started in background (PID {pid})", flush=True)
            sys.exit(0)
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log_fd = os.open(str(LOG_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(log_fd, 2)

    plugins = load_plugins()
    ProxyHandler.plugins = plugins

    server = ThreadedHTTPServer(("127.0.0.1", port), ProxyHandler)
    _write_pid(os.getpid())
    print(f"[proxy] listening on http://127.0.0.1:{port}", file=sys.stderr, flush=True)

    wd = threading.Thread(target=_inactivity_watchdog, args=(server,), daemon=True)
    wd.start()

    def _shutdown(signum, frame):
        _cleanup_pid()
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _cleanup_pid()
        server.shutdown()


if __name__ == "__main__":
    main()

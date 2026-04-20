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
import re
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


# ── Plugin config helpers ──────────────────────────────────────────────────

def load_plugin_config(plugins_dir: Path, name: str) -> dict:
    """Load <name>.toml from plugins_dir. Returns {} if not found or unreadable."""
    if not plugins_dir.exists():
        return {}
    toml_path = plugins_dir / f"{name}.toml"
    if not toml_path.exists():
        return {}
    try:
        return _parse_plugins_toml(toml_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def is_plugin_enabled(name: str, local_config: dict, global_config: dict) -> bool:
    """Check if a plugin is enabled.

    Resolution order:
      1. local_config "enabled" key (from <name>.toml) — string "true"/"false"
      2. global_config "enabled" list (from plugins.toml)
    """
    if "enabled" in local_config:
        val = local_config["enabled"]
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val)
    return name in global_config.get("enabled", [])


def validate_plugin(module) -> bool:
    """Validate a plugin module before activation.

    Requirements:
      - Must have plugin_info() returning a dict with "name" key.
      - If health_check() is defined, it must return True.
    """
    if not hasattr(module, "plugin_info"):
        return False
    try:
        info = module.plugin_info()
        if not isinstance(info, dict) or "name" not in info:
            return False
    except Exception:
        return False

    if hasattr(module, "health_check"):
        try:
            if not module.health_check():
                return False
        except Exception:
            return False

    return True


# ── PluginManager ──────────────────────────────────────────────────────────

class PluginManager:
    """Manages plugin lifecycle: loading, validation, and zero-downtime hot-reload.

    Plugins are loaded from .py files in the plugins directory. Each plugin's
    config comes from a sibling .toml file (e.g. telegram.py → telegram.toml),
    with fallback to the global plugins.toml for backward compatibility.

    Zero-downtime reload:
      - When no requests are in-flight, swaps happen immediately.
      - When requests are active, the new plugin list is held pending and
        applied the moment the last in-flight request completes.
      - A timeout forces the swap after swap_timeout seconds to prevent
        indefinite delays from long-running requests.
    """

    def __init__(
        self,
        plugins_dir: Path = PLUGINS_DIR,
        global_config_path: Path = PLUGINS_TOML,
        poll_interval: float = 2.0,
        swap_timeout: float = 5.0,
    ):
        self._plugins_dir = plugins_dir
        self._global_config_path = global_config_path
        self._poll_interval = poll_interval
        self._swap_timeout = swap_timeout
        self._plugins: list = []
        self._file_state: dict[str, float] = {}  # path → mtime
        self._in_flight: int = 0
        self._pending_plugins: list | None = None
        self._pending_since: float | None = None
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def plugins(self) -> list:
        return self._plugins

    @property
    def has_pending_swap(self) -> bool:
        return self._pending_plugins is not None

    def enter_request(self) -> None:
        """Call at the start of each proxied request."""
        with self._lock:
            self._in_flight += 1

    def exit_request(self) -> None:
        """Call at the end of each proxied request (in a finally block)."""
        with self._lock:
            self._in_flight -= 1
            if self._in_flight == 0 and self._pending_plugins is not None:
                self._apply_swap()

    def initial_load(self) -> None:
        """Load all enabled plugins. Called once at startup."""
        self._plugins = self._build_plugin_list()
        self._snapshot_files()

    def start_watcher(self) -> None:
        """Start the background file-watcher thread (daemon)."""
        t = threading.Thread(target=self._watch_loop, daemon=True)
        t.start()

    def check_and_reload(self) -> None:
        """Check for file changes and trigger reload. Also enforces swap timeout."""
        # Check timeout on pending swap regardless of file changes
        if self._pending_plugins is not None:
            with self._lock:
                if (self._pending_since is not None
                        and time.time() - self._pending_since > self._swap_timeout):
                    self._apply_swap()
                    self._snapshot_files()
                    return

        if not self._has_changes():
            return

        new_plugins = self._build_plugin_list()
        with self._lock:
            if self._in_flight == 0:
                self._plugins = new_plugins
                self._pending_plugins = None
                self._pending_since = None
            else:
                self._pending_plugins = new_plugins
                self._pending_since = time.time()
        self._snapshot_files()

    # ── Internals ─────────────────────────────────────────────────────────

    def _apply_swap(self) -> None:
        """Apply pending swap. Must be called under self._lock."""
        if self._pending_plugins is not None:
            self._plugins = self._pending_plugins
            self._pending_plugins = None
            self._pending_since = None
            print("[proxy] plugin hot-reload applied", file=sys.stderr, flush=True)

    def _watch_loop(self) -> None:
        while True:
            time.sleep(self._poll_interval)
            try:
                self.check_and_reload()
            except Exception as exc:
                print(f"[proxy] watcher error: {exc}", file=sys.stderr)

    def _has_changes(self) -> bool:
        return self._get_file_state() != self._file_state

    def _snapshot_files(self) -> None:
        self._file_state = self._get_file_state()

    def _get_file_state(self) -> dict[str, float]:
        state: dict[str, float] = {}
        if not self._plugins_dir.exists():
            return state
        for f in self._plugins_dir.iterdir():
            if f.suffix in (".py", ".toml"):
                try:
                    state[str(f)] = f.stat().st_mtime
                except OSError:
                    pass
        # Also track global config
        if self._global_config_path.exists():
            try:
                state[str(self._global_config_path)] = self._global_config_path.stat().st_mtime
            except OSError:
                pass
        return state

    def _load_global_config(self) -> dict:
        if not self._global_config_path.exists():
            return {}
        try:
            return _parse_plugins_toml(
                self._global_config_path.read_text(encoding="utf-8")
            )
        except Exception:
            return {}

    def _build_plugin_list(self) -> list:
        """Load all enabled, valid, healthy plugins."""
        plugins: list = []
        global_config = self._load_global_config()

        if not self._plugins_dir.exists():
            return plugins

        for py_file in sorted(self._plugins_dir.glob("*.py")):
            name = py_file.stem
            local_config = load_plugin_config(self._plugins_dir, name)

            if not is_plugin_enabled(name, local_config, global_config):
                continue

            try:
                spec = importlib.util.spec_from_file_location(
                    f"_proxy_plugin_{name}", py_file
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)  # type: ignore[union-attr]

                if not validate_plugin(mod):
                    print(f"[proxy] plugin '{name}' failed validation", file=sys.stderr)
                    continue

                # Merge config: global [name] section + local .toml (local wins).
                # Strip "enabled" — it's metadata, not plugin config.
                global_section = global_config.get(name, {})
                merged = {}
                if isinstance(global_section, dict):
                    merged.update(global_section)
                merged.update({k: v for k, v in local_config.items() if k != "enabled"})

                if hasattr(mod, "configure"):
                    mod.configure(merged)

                plugins.append(mod)
                label = mod.plugin_info()["name"]
                print(f"[proxy] loaded plugin: {label}", file=sys.stderr, flush=True)
            except Exception as exc:
                print(f"[proxy] failed to load plugin '{name}': {exc}", file=sys.stderr)

        return plugins


# ── Legacy wrapper ─────────────────────────────────────────────────────────

def load_plugins(
    plugins_dir: Path = PLUGINS_DIR,
    config_file: Path = PLUGINS_TOML,
) -> list:
    """Load plugins once (legacy wrapper around PluginManager.initial_load)."""
    mgr = PluginManager(plugins_dir, config_file)
    mgr.initial_load()
    return mgr.plugins


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
    plugin_manager: PluginManager | None = None  # set by main()
    plugins: list = []  # backward compat for tests without PluginManager

    def _get_plugins(self) -> list:
        if self.plugin_manager:
            return self.plugin_manager.plugins
        return self.plugins

    # ── routing ───────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path == "/status":
            self._health()
        elif self.path == "/reload":
            self._reload()
        elif self.path == "/test-telegram":
            self._test_telegram()
        else:
            self._forward("GET")

    def do_POST(self):
        self._forward("POST")

    # ── health ────────────────────────────────────────────────────────────

    def _health(self):
        plugin_names: list[str] = []
        for p in self._get_plugins():
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

    # ── reload ────────────────────────────────────────────────────────────

    def _reload(self):
        """Trigger a zero-downtime plugin hot-reload."""
        if self.plugin_manager:
            self.plugin_manager.check_and_reload()
        body = json.dumps({"status": "reloaded"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── test telegram ─────────────────────────────────────────────────────

    def _test_telegram(self):
        """Send a sample message through the telegram plugin for visual testing."""
        sample_response = (
            "Security scan results:\n\n"
            "| # | Test Case | Result | Notes |\n"
            "|---|-----------|--------|-------|\n"
            "| 1 | SQL Injection | PASS | Inputs sanitized |\n"
            "| 2 | XSS Reflected | PASS | Output encoded |\n"
            "| 3 | Rate Limiting | FAIL | No limits set |\n"
            "| 4 | Auth Bypass | PASS | Middleware OK |\n\n"
            "Overall: 3/4 passed. Action required on rate limiting."
        )
        sample_request = {"user_text": "run security scan and present a report"}
        errors = []
        plugins = self._get_plugins()
        for p in plugins:
            on_inbound = getattr(p, "on_inbound", None)
            if on_inbound:
                try:
                    on_inbound(sample_response, sample_request)
                except Exception as exc:
                    errors.append(f"{p}: {exc}")
                    print(f"[proxy] test-telegram error: {exc}", file=sys.stderr, flush=True)
        status = {"status": "sent", "plugins_called": len(plugins)}
        if errors:
            status["errors"] = errors
        body = json.dumps(status).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── proxy core ────────────────────────────────────────────────────────

    def _forward(self, method: str):
        global _last_activity
        _last_activity = time.time()

        if self.plugin_manager:
            self.plugin_manager.enter_request()
        try:
            self._forward_inner(method)
        finally:
            if self.plugin_manager:
                self.plugin_manager.exit_request()

    def _forward_inner(self, method: str):
        plugins = self._get_plugins()

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b""

        body = raw_body
        payload: dict | None = None

        if raw_body:
            try:
                payload = json.loads(raw_body)
            except (json.JSONDecodeError, ValueError):
                payload = None

        request_summary: dict = {"path": self.path, "model": "", "user_text": "", "cwd": ""}
        inbound_sideload: list[str] = []

        if payload is not None:
            request_summary["model"] = payload.get("model", "")
            request_summary["user_text"] = _extract_user_text(payload)
            request_summary["cwd"] = _extract_cwd(payload)

            # Load + consume sideload files
            outbound_items = load_sideload(SIDELOAD_OUTBOUND)
            inbound_items = load_sideload(SIDELOAD_INBOUND)
            inbound_sideload = [
                item["content"] for item in inbound_items if item.get("content")
            ]

            # Inject outbound sideload into payload
            payload = inject_outbound(payload, outbound_items)

            # Call on_outbound plugins
            for plugin in plugins:
                if not hasattr(plugin, "on_outbound"):
                    continue
                try:
                    result = plugin.on_outbound(copy.deepcopy(payload))
                    if result is not None:
                        payload = result
                except Exception as exc:
                    print(f"[proxy] plugin on_outbound error: {exc}", file=sys.stderr)

            body = json.dumps(payload).encode()

        # Build upstream headers (strip hop-by-hop + force no compression)
        skip = {"host", "transfer-encoding", "content-length", "accept-encoding"}
        upstream_headers: dict[str, str] = {
            k: v for k, v in self.headers.items() if k.lower() not in skip
        }
        upstream_headers["Host"] = UPSTREAM_HOST
        upstream_headers["Content-Length"] = str(len(body))
        upstream_headers["Accept-Encoding"] = "identity"

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
                    plugins=plugins,
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
            if inbound_sideload or any(hasattr(p, "on_inbound") for p in plugins):
                resp_body = _inject_non_streaming(
                    resp_body, plugins, request_summary, inbound_sideload
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


_CWD_PATTERNS = [
    re.compile(r"^\s*[-*]?\s*Primary working directory:\s*(.+?)\s*$", re.MULTILINE),
    re.compile(r"^\s*[-*]?\s*Working directory:\s*(.+?)\s*$", re.MULTILINE),
    re.compile(r"^\s*cwd:\s*(.+?)\s*$", re.MULTILINE),
]


def _extract_cwd(payload: dict) -> str:
    """Extract the working directory from Claude Code's system prompt.

    Claude Code embeds project context in the system field as:
        - Primary working directory: /path/to/project
    Also supports legacy formats (Working directory:, cwd:).
    Returns the cwd path, or empty string if not found.
    """
    system = payload.get("system", "")
    if isinstance(system, list):
        parts = [b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"]
        system = "\n".join(parts)
    if not isinstance(system, str):
        return ""
    for pat in _CWD_PATTERNS:
        m = pat.search(system)
        if m:
            return m.group(1).strip()
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


def _cleanup_pid(expected_pid: int | None = None) -> None:
    """Remove PID file, but only if it belongs to us.

    If *expected_pid* is given, the file is only deleted when its contents
    match.  This prevents a crashing child from wiping the PID written by
    an earlier, healthy instance.
    """
    try:
        if expected_pid is not None:
            current = _read_pid()
            if current != expected_pid:
                return  # not ours — leave it alone
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass




def _port_in_use(port: int) -> bool:
    """Check if a TCP port is already bound on localhost."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def is_proxy_running() -> bool:
    pid = _read_pid()
    if pid is not None:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            _cleanup_pid()
        except PermissionError:
            return True  # process exists but we can't signal it

    # Fallback: PID file missing/stale but port is held by a previous instance
    if _port_in_use(LISTEN_PORT):
        return True

    return False


# ── Inactivity watchdog ────────────────────────────────────────────────────

def _inactivity_watchdog(server, my_pid: int | None = None) -> None:
    while True:
        time.sleep(60)
        if time.time() - _last_activity > _INACTIVITY_TIMEOUT:
            print("[proxy] shutting down: inactivity timeout", file=sys.stderr, flush=True)
            _cleanup_pid(expected_pid=my_pid)
            server.shutdown()
            break


# ── Entry point ────────────────────────────────────────────────────────────

def _acquire_startup_lock() -> "int | None":
    """Try to acquire an exclusive startup lock via a lockfile.

    Returns the fd on success, None if another process holds it.
    Uses fcntl.flock which is automatically released on process exit / crash.
    """
    import fcntl
    lock_path = STATE_DIR / "proxy.lock"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, IOError):
        return None


def _cmd_reload(port: int) -> None:
    """Send a reload request to a running proxy instance."""
    import http.client
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/reload")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        print(f"[proxy] {data.get('status', 'ok')}", flush=True)
    except Exception as exc:
        print(f"[proxy] reload failed — is the proxy running on port {port}? ({exc})", file=sys.stderr)
        sys.exit(1)


def _dispatch_hook(event: str) -> None:
    """Unified Claude Code hook dispatcher.

    Registered once in settings.json as:
        proxy.py --hook pre-tool

    Reads hook input from stdin, loads enabled plugins, calls each plugin's
    hook handler, and returns the most restrictive decision on stdout.
    Plugins implement hook handlers as optional functions:
        on_pre_tool_use(hook_input: dict) -> dict | None

    Returns JSON on stdout per Claude Code hook protocol.
    """
    import subprocess

    hook_input_raw = sys.stdin.read()
    try:
        hook_input = json.loads(hook_input_raw)
    except (json.JSONDecodeError, ValueError):
        # Can't parse input — let Claude Code handle it normally
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "proxy hook: invalid input",
        }}))
        sys.exit(0)

    if event != "pre-tool":
        sys.exit(0)

    # Find plugin hook scripts: ~/.claude/claude-proxy/hooks/<plugin>_*.py
    hooks_dir = STATE_DIR / "hooks"
    if not hooks_dir.exists():
        sys.exit(0)

    # Also check which plugins are enabled and want this hook
    # For now, discover all *_*.py scripts in hooks_dir and run them
    decisions: list[str] = []
    reasons: list[str] = []

    for script in sorted(hooks_dir.glob("*.py")):
        try:
            proc = subprocess.run(
                [sys.executable, str(script)],
                input=hook_input_raw,
                capture_output=True,
                text=True,
                timeout=650,  # leave margin under Claude Code's 660s hook timeout
            )
            if proc.stdout.strip():
                result = json.loads(proc.stdout.strip())
                decision = (result.get("hookSpecificOutput", {})
                            .get("permissionDecision", ""))
                reason = (result.get("hookSpecificOutput", {})
                          .get("permissionDecisionReason", ""))
                if decision:
                    decisions.append(decision)
                if reason:
                    reasons.append(reason)
        except subprocess.TimeoutExpired:
            decisions.append("ask")
            reasons.append(f"{script.name}: timeout")
        except Exception as exc:
            print(f"[proxy-hook] {script.name} error: {exc}", file=sys.stderr)

    if not decisions:
        sys.exit(0)  # no plugin had an opinion — proceed normally

    # Most restrictive wins: deny > ask > allow
    if "deny" in decisions:
        final = "deny"
    elif "ask" in decisions:
        final = "ask"
    else:
        final = "allow"

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": final,
        "permissionDecisionReason": "; ".join(reasons) if reasons else "",
    }}))
    sys.exit(0)


def main():
    import argparse
    parser = argparse.ArgumentParser(prog="claude-proxy")
    parser.add_argument("--daemon", action="store_true", help="Run in background")
    parser.add_argument("--hook", metavar="EVENT", help="Run as Claude Code hook dispatcher (e.g. pre-tool)")
    parser.add_argument("--port", type=int, default=LISTEN_PORT)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("reload", help="Hot-reload plugins on a running proxy")
    args = parser.parse_args()
    port = args.port

    if args.command == "reload":
        _cmd_reload(port)
        return

    if args.hook:
        _dispatch_hook(args.hook)
        return

    # Dedup guard — silently exit if an instance is already running.
    # Allows SessionStart hook to fire on every Claude Code window safely.
    if is_proxy_running():
        sys.exit(0)

    if args.daemon:
        pid = os.fork()
        if pid > 0:
            print(f"[proxy] started in background (PID {pid})", flush=True)
            sys.exit(0)
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log_fd = os.open(str(LOG_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(log_fd, 2)

    # Acquire exclusive startup lock — serialises concurrent launches so only
    # one child proceeds past this point.  The lock is held for the lifetime
    # of the process and auto-released on exit/crash (fcntl.flock).
    lock_fd = _acquire_startup_lock()
    if lock_fd is None:
        sys.exit(0)  # another instance is starting or running

    # Re-check after acquiring lock — the first winner may have already bound
    # the port while we waited (LOCK_NB means we don't actually wait, but
    # being explicit here is cheap insurance).
    if _port_in_use(port):
        os.close(lock_fd)
        sys.exit(0)

    # Write PID immediately so concurrent launches see us before we finish
    # loading plugins.  The lock already prevents races, but the PID file
    # also serves the dedup guard on future launches (after lock is released
    # would be too late — write it now).
    my_pid = os.getpid()
    _write_pid(my_pid)

    plugin_mgr = PluginManager()
    plugin_mgr.initial_load()
    plugin_mgr.start_watcher()
    ProxyHandler.plugin_manager = plugin_mgr

    # Clean up PID file on exit — but only if it still points to us.
    # This prevents a crashing child from deleting a healthy instance's PID.
    import atexit
    atexit.register(_cleanup_pid, expected_pid=my_pid)

    server = ThreadedHTTPServer(("127.0.0.1", port), ProxyHandler)
    print(f"[proxy] listening on http://127.0.0.1:{port}", file=sys.stderr, flush=True)

    wd = threading.Thread(target=_inactivity_watchdog, args=(server, my_pid), daemon=True)
    wd.start()

    def _shutdown(signum, frame):
        _cleanup_pid(expected_pid=my_pid)
        # Call shutdown from a thread — calling it directly in a signal
        # handler can deadlock if serve_forever() holds an internal lock.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _cleanup_pid(expected_pid=my_pid)
        server.shutdown()


if __name__ == "__main__":
    main()

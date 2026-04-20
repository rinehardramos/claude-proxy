import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BANNER = Path(__file__).parent.parent / "hooks" / "session_banner.sh"


def _serve(payload: dict, port_holder: list):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    port_holder.append(server.server_address[1])
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def _run_banner(port: int) -> str:
    result = subprocess.run(
        ["/bin/sh", str(BANNER)],
        env={"CLAUDE_PROXY_PORT": str(port), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=5,
    )
    return result.stdout.strip()


def test_banner_healthy_status():
    ports: list[int] = []
    server = _serve({
        "status": "ok", "uptime_s": 123, "rss_mb": 47,
        "plugin_reloads": 2, "warnings": [],
    }, ports)
    try:
        out = _run_banner(ports[0])
    finally:
        server.shutdown()
    assert "[claude-proxy] ok" in out
    assert "rss 47MB" in out


def test_banner_warning_status():
    ports: list[int] = []
    server = _serve({
        "status": "warning", "uptime_s": 999, "rss_mb": 480,
        "plugin_reloads": 48, "warnings": ["rss 480MB near cap 512MB"],
    }, ports)
    try:
        out = _run_banner(ports[0])
    finally:
        server.shutdown()
    assert "⚠" in out or "!" in out
    assert "480MB" in out


def test_banner_dead_proxy():
    # No server running on an unused port
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    out = _run_banner(free_port)
    assert "not responding" in out or "starting" in out

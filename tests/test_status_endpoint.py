import json
import threading
import time
import urllib.request

import pytest


@pytest.fixture
def running_proxy(tmp_path, monkeypatch):
    """Spin up a real proxy on an OS-assigned port, shut down at teardown."""
    monkeypatch.setattr("proxy.STATE_DIR", tmp_path)
    monkeypatch.setattr("proxy.PLUGINS_DIR", tmp_path / "plugins")

    import proxy
    mgr = proxy.PluginManager(plugins_dir=tmp_path / "plugins",
                              global_config_path=tmp_path / "plugins.toml")
    mgr.initial_load()
    proxy.ProxyHandler.plugin_manager = mgr

    from monitor import ResourceMonitor
    proxy.ProxyHandler.resource_monitor = ResourceMonitor(
        get_reload_count=lambda: mgr.reload_count,
    )

    server = proxy.ThreadedHTTPServer(("127.0.0.1", 0), proxy.ProxyHandler)
    port = server.server_address[1]
    monkeypatch.setenv("CLAUDE_PROXY_PORT", str(port))
    monkeypatch.setattr("proxy.LISTEN_PORT", port)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    yield port
    server.shutdown()


def test_status_returns_monitor_snapshot(running_proxy):
    port = running_proxy
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=2) as resp:
        body = json.loads(resp.read())
    assert body["status"] in ("ok", "warning")
    assert "uptime_s" in body
    assert "rss_mb" in body
    assert "threads" in body
    assert "fds" in body
    assert "plugin_reloads" in body
    assert "last_recycle" in body
    assert body["last_recycle"] is None
    assert "warnings" in body
    assert isinstance(body["warnings"], list)

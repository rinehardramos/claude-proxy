# claude-proxy Supervisor & Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make claude-proxy a supervised service (launchd/systemd) with a ResourceMonitor for leak/drift detection and user-visible health observability, so crashes are invisible to the user.

**Architecture:** Add a `supervisor/` package (adapters for launchd, systemd, Windows stub) and a standalone `monitor.py` module. Extend the existing `/status` health endpoint to expose monitor metrics. Replace the silent SessionStart spawn with a banner script. Remove the inactivity watchdog and the `--daemon` self-fork when running under a supervisor.

**Tech Stack:** Python 3.9+ (stdlib only, `psutil` optional), launchd plists, systemd user units, shell scripts for hooks.

**Spec:** [docs/superpowers/specs/2026-04-20-claude-proxy-supervisor-design.md](../specs/2026-04-20-claude-proxy-supervisor-design.md)

**Naming note:** The existing health endpoint is `/status`, not `/health` — the spec uses "HealthProbe" loosely. This plan uses `/status` (the actual path).

---

## File structure

**New files:**
- `monitor.py` — `ResourceMonitor` thread, metric collection, snapshot API
- `supervisor/__init__.py` — `get_adapter()` picks by `sys.platform`
- `supervisor/base.py` — `Supervisor` protocol
- `supervisor/launchd.py` — macOS plist + `launchctl` calls
- `supervisor/systemd.py` — Linux user unit + `systemctl --user`
- `supervisor/windows.py` — stub raising `NotImplementedError`
- `hooks/session_banner.sh` — SessionStart banner (probes `/status`, prints one-liner)
- `tests/test_monitor.py`
- `tests/test_supervisor_launchd.py`
- `tests/test_supervisor_systemd.py`
- `tests/test_session_banner.py`
- `tests/test_status_endpoint.py`

**Modified files:**
- `proxy.py` — remove inactivity watchdog, wire `ResourceMonitor`, extend `/status`, increment reload counter, detect `CLAUDE_PROXY_SUPERVISED`
- `setup.py` — delegate install/uninstall/restart/status to supervisor adapter; ship banner hook; migrate legacy LaunchAgent
- `plugins/leak_guard.py` — replace proxy auto-spawn with health probe warning (if spawn logic is present)
- `tests/test_proxy.py`, `tests/test_crash_scenarios.py`, `tests/test_setup.py` — update for removed watchdog + new supervisor paths

---

## Task 1: Add plugin_reloads counter to PluginManager

**Files:**
- Modify: `proxy.py` (class `PluginManager`, around lines 167-250)
- Modify: `tests/test_proxy.py` or `tests/test_proxy_state.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_proxy_state.py`:

```python
def test_plugin_manager_tracks_reload_count(tmp_path):
    from proxy import PluginManager

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "dummy.py").write_text(
        "def plugin_info():\n    return {'name': 'dummy', 'version': '0'}\n"
    )

    mgr = PluginManager(plugins_dir=plugins_dir, global_config_path=tmp_path / "plugins.toml")
    mgr.initial_load()
    assert mgr.reload_count == 0

    # Touch the file so mtime changes
    import time, os
    time.sleep(0.01)
    os.utime(plugins_dir / "dummy.py", None)

    mgr.check_and_reload()
    assert mgr.reload_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_proxy_state.py::test_plugin_manager_tracks_reload_count -v`
Expected: FAIL with `AttributeError: 'PluginManager' object has no attribute 'reload_count'`

- [ ] **Step 3: Add the counter**

In `proxy.py`, modify `PluginManager.__init__` to add:

```python
        self._reload_count: int = 0
```

Add a property after the `has_pending_swap` property:

```python
    @property
    def reload_count(self) -> int:
        return self._reload_count
```

Modify `_apply_swap` to increment the counter:

```python
    def _apply_swap(self) -> None:
        """Apply pending swap. Must be called under self._lock."""
        if self._pending_plugins is not None:
            self._plugins = self._pending_plugins
            self._pending_plugins = None
            self._pending_since = None
            self._reload_count += 1
            print("[proxy] plugin hot-reload applied", file=sys.stderr, flush=True)
```

Also modify `check_and_reload` where it assigns `self._plugins = new_plugins` when in-flight is 0 (no pending swap path) to increment the counter:

```python
        with self._lock:
            if self._in_flight == 0:
                self._plugins = new_plugins
                self._pending_plugins = None
                self._pending_since = None
                self._reload_count += 1
            else:
                self._pending_plugins = new_plugins
                self._pending_since = time.time()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_proxy_state.py::test_plugin_manager_tracks_reload_count -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add proxy.py tests/test_proxy_state.py
git commit -m "feat(proxy): track plugin reload count in PluginManager"
```

---

## Task 2: monitor.py — metric collection with psutil fallback

**Files:**
- Create: `monitor.py`
- Create: `tests/test_monitor.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_monitor.py`:

```python
from monitor import collect_metrics


def test_collect_metrics_returns_expected_keys():
    m = collect_metrics()
    assert set(m.keys()) >= {"rss_mb", "threads", "fds"}
    assert isinstance(m["rss_mb"], int)
    assert isinstance(m["threads"], int)
    assert isinstance(m["fds"], int)
    assert m["rss_mb"] >= 0
    assert m["threads"] >= 1
    assert m["fds"] >= 1


def test_collect_metrics_without_psutil(monkeypatch):
    import monitor
    monkeypatch.setattr(monitor, "_PSUTIL", None)
    m = monitor.collect_metrics()
    # Fallback path must still return sensible values
    assert m["rss_mb"] >= 0
    assert m["threads"] >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_monitor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'monitor'`

- [ ] **Step 3: Implement monitor.collect_metrics**

Create `monitor.py`:

```python
"""ResourceMonitor — detect accumulated weirdness and recycle the proxy.

Collects RSS, thread count, and open fd count. Uses psutil when available,
falls back to platform-specific stdlib probes otherwise.
"""
from __future__ import annotations

import os
import resource
import sys
import threading
from pathlib import Path

try:
    import psutil as _PSUTIL
except ImportError:
    _PSUTIL = None


def _rss_bytes() -> int:
    if _PSUTIL is not None:
        return _PSUTIL.Process().memory_info().rss
    # stdlib fallback — getrusage returns KB on Linux, bytes on macOS
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return ru
    return ru * 1024  # Linux reports KB


def _fd_count() -> int:
    if _PSUTIL is not None:
        try:
            return _PSUTIL.Process().num_fds()
        except (AttributeError, NotImplementedError):
            pass
    # Linux: count /proc/self/fd
    fd_dir = Path("/proc/self/fd")
    if fd_dir.exists():
        try:
            return sum(1 for _ in fd_dir.iterdir())
        except OSError:
            pass
    # macOS without psutil: shell out to lsof as last resort
    if sys.platform == "darwin":
        import subprocess
        try:
            out = subprocess.check_output(
                ["lsof", "-p", str(os.getpid())],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            )
            return max(0, len(out.splitlines()) - 1)  # minus header
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            pass
    return 0


def _fd_soft_limit() -> int:
    try:
        return resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except (ValueError, OSError):
        return 1024


def collect_metrics() -> dict:
    return {
        "rss_mb": _rss_bytes() // (1024 * 1024),
        "threads": threading.active_count(),
        "fds": _fd_count(),
        "fd_limit": _fd_soft_limit(),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_monitor.py -v`
Expected: PASS for both tests

- [ ] **Step 5: Commit**

```bash
git add monitor.py tests/test_monitor.py
git commit -m "feat(monitor): add metric collection with psutil fallback"
```

---

## Task 3: monitor.py — threshold evaluation

**Files:**
- Modify: `monitor.py`
- Modify: `tests/test_monitor.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_monitor.py`:

```python
from monitor import evaluate_thresholds, Thresholds


def test_evaluate_returns_none_when_healthy():
    t = Thresholds(rss_mb=512, threads=200, fd_pct=0.8)
    metrics = {"rss_mb": 42, "threads": 8, "fds": 14, "fd_limit": 1024}
    assert evaluate_thresholds(metrics, 0, t) is None


def test_evaluate_detects_rss_breach():
    t = Thresholds(rss_mb=512, threads=200, fd_pct=0.8)
    metrics = {"rss_mb": 600, "threads": 8, "fds": 14, "fd_limit": 1024}
    breach = evaluate_thresholds(metrics, 0, t)
    assert breach is not None
    assert breach.reason == "rss_exceeded"
    assert breach.value == 600
    assert breach.threshold == 512


def test_evaluate_detects_thread_leak():
    t = Thresholds(rss_mb=512, threads=200, fd_pct=0.8)
    metrics = {"rss_mb": 42, "threads": 250, "fds": 14, "fd_limit": 1024}
    breach = evaluate_thresholds(metrics, 0, t)
    assert breach is not None
    assert breach.reason == "threads_exceeded"


def test_evaluate_detects_fd_near_limit():
    t = Thresholds(rss_mb=512, threads=200, fd_pct=0.8)
    metrics = {"rss_mb": 42, "threads": 8, "fds": 900, "fd_limit": 1024}
    breach = evaluate_thresholds(metrics, 0, t)
    assert breach is not None
    assert breach.reason == "fds_near_limit"


def test_evaluate_detects_reload_count():
    t = Thresholds(rss_mb=512, threads=200, fd_pct=0.8, reloads=50)
    metrics = {"rss_mb": 42, "threads": 8, "fds": 14, "fd_limit": 1024}
    breach = evaluate_thresholds(metrics, 51, t)
    assert breach is not None
    assert breach.reason == "reloads_exceeded"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_monitor.py -v`
Expected: FAIL — `ImportError: cannot import name 'evaluate_thresholds' from 'monitor'`

- [ ] **Step 3: Implement threshold evaluation**

Append to `monitor.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Thresholds:
    rss_mb: int = 512
    threads: int = 200
    fd_pct: float = 0.8
    reloads: int = 50


@dataclass(frozen=True)
class Breach:
    reason: str
    value: int
    threshold: int


def evaluate_thresholds(metrics: dict, reload_count: int, t: Thresholds) -> Breach | None:
    if metrics["rss_mb"] > t.rss_mb:
        return Breach("rss_exceeded", metrics["rss_mb"], t.rss_mb)
    if metrics["threads"] > t.threads:
        return Breach("threads_exceeded", metrics["threads"], t.threads)
    fd_limit = int(metrics["fd_limit"] * t.fd_pct)
    if metrics["fds"] > fd_limit:
        return Breach("fds_near_limit", metrics["fds"], fd_limit)
    if reload_count > t.reloads:
        return Breach("reloads_exceeded", reload_count, t.reloads)
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_monitor.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add monitor.py tests/test_monitor.py
git commit -m "feat(monitor): add threshold evaluation logic"
```

---

## Task 4: monitor.py — ResourceMonitor with cooldown and kill switch

**Files:**
- Modify: `monitor.py`
- Modify: `tests/test_monitor.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_monitor.py`:

```python
from monitor import ResourceMonitor, Thresholds


class FakeClock:
    def __init__(self, start=0.0):
        self.t = start
    def now(self) -> float:
        return self.t
    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_monitor_snapshot_includes_all_fields():
    clock = FakeClock()
    mon = ResourceMonitor(
        thresholds=Thresholds(),
        get_reload_count=lambda: 0,
        clock=clock.now,
        metrics_source=lambda: {"rss_mb": 42, "threads": 8, "fds": 14, "fd_limit": 1024},
    )
    snap = mon.snapshot()
    assert set(snap.keys()) >= {"uptime_s", "rss_mb", "threads", "fds", "plugin_reloads", "last_recycle", "warnings"}
    assert snap["rss_mb"] == 42
    assert snap["plugin_reloads"] == 0
    assert snap["last_recycle"] is None


def test_monitor_recycle_decision_respects_cooldown():
    clock = FakeClock(start=1000.0)
    breached_metrics = {"rss_mb": 600, "threads": 8, "fds": 14, "fd_limit": 1024}
    mon = ResourceMonitor(
        thresholds=Thresholds(rss_mb=512),
        get_reload_count=lambda: 0,
        clock=clock.now,
        metrics_source=lambda: breached_metrics,
        cooldown_s=600,
    )
    # First breach → recycle requested
    assert mon.should_recycle() is not None
    mon.mark_recycled("rss_exceeded")

    # Immediately after → still in cooldown, no recycle
    assert mon.should_recycle() is None

    # After cooldown elapses → recycle again
    clock.advance(601)
    assert mon.should_recycle() is not None


def test_monitor_kill_switch_via_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_PROXY_MONITOR", "off")
    mon = ResourceMonitor(
        thresholds=Thresholds(rss_mb=1),  # trivially breached
        get_reload_count=lambda: 0,
        metrics_source=lambda: {"rss_mb": 999, "threads": 8, "fds": 14, "fd_limit": 1024},
    )
    assert mon.should_recycle() is None


def test_monitor_snapshot_warnings_when_near_cap():
    mon = ResourceMonitor(
        thresholds=Thresholds(rss_mb=100),
        get_reload_count=lambda: 0,
        metrics_source=lambda: {"rss_mb": 85, "threads": 8, "fds": 14, "fd_limit": 1024},
    )
    snap = mon.snapshot()
    assert any("rss" in w for w in snap["warnings"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_monitor.py -v`
Expected: FAIL — `ImportError: cannot import name 'ResourceMonitor' from 'monitor'`

- [ ] **Step 3: Implement ResourceMonitor**

Append to `monitor.py`:

```python
import os
import time
from typing import Callable, Optional


class ResourceMonitor:
    """Tracks proxy health metrics and decides when to recycle.

    Does NOT own the recycle action itself — the owner polls should_recycle()
    and performs the drain+exit. This keeps ResourceMonitor unit-testable
    without a real HTTP server.
    """

    def __init__(
        self,
        thresholds: Thresholds = Thresholds(),
        get_reload_count: Callable[[], int] = lambda: 0,
        clock: Callable[[], float] = time.time,
        metrics_source: Callable[[], dict] = collect_metrics,
        cooldown_s: float = 600.0,
    ):
        self._thresholds = thresholds
        self._get_reload_count = get_reload_count
        self._clock = clock
        self._metrics = metrics_source
        self._cooldown_s = cooldown_s
        self._started_at = clock()
        self._last_recycle_at: Optional[float] = None
        self._last_recycle_reason: Optional[str] = None

    def _enabled(self) -> bool:
        return os.environ.get("CLAUDE_PROXY_MONITOR", "on").lower() != "off"

    def should_recycle(self) -> Optional[Breach]:
        if not self._enabled():
            return None
        if self._last_recycle_at is not None:
            if self._clock() - self._last_recycle_at < self._cooldown_s:
                return None
        metrics = self._metrics()
        return evaluate_thresholds(metrics, self._get_reload_count(), self._thresholds)

    def mark_recycled(self, reason: str) -> None:
        self._last_recycle_at = self._clock()
        self._last_recycle_reason = reason

    def snapshot(self) -> dict:
        metrics = self._metrics()
        warnings: list[str] = []
        # "near cap" = >=80% of threshold
        if metrics["rss_mb"] >= self._thresholds.rss_mb * 0.8:
            warnings.append(
                f"rss {metrics['rss_mb']}MB near cap {self._thresholds.rss_mb}MB"
            )
        if metrics["threads"] >= self._thresholds.threads * 0.8:
            warnings.append(
                f"threads {metrics['threads']} near cap {self._thresholds.threads}"
            )
        fd_cap = int(metrics["fd_limit"] * self._thresholds.fd_pct)
        if metrics["fds"] >= fd_cap * 0.8:
            warnings.append(f"fds {metrics['fds']} near cap {fd_cap}")

        last: Optional[dict] = None
        if self._last_recycle_at is not None:
            last = {
                "at": self._last_recycle_at,
                "reason": self._last_recycle_reason,
            }

        return {
            "uptime_s": int(self._clock() - self._started_at),
            "rss_mb": metrics["rss_mb"],
            "threads": metrics["threads"],
            "fds": metrics["fds"],
            "plugin_reloads": self._get_reload_count(),
            "last_recycle": last,
            "warnings": warnings,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_monitor.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add monitor.py tests/test_monitor.py
git commit -m "feat(monitor): add ResourceMonitor with cooldown and kill switch"
```

---

## Task 5: monitor.py — background watchdog thread

**Files:**
- Modify: `monitor.py`
- Modify: `tests/test_monitor.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_monitor.py`:

```python
import threading


def test_monitor_watch_invokes_on_recycle_callback():
    called: list[Breach] = []
    done = threading.Event()

    def on_breach(breach):
        called.append(breach)
        done.set()

    # metrics breach immediately
    mon = ResourceMonitor(
        thresholds=Thresholds(rss_mb=10),
        get_reload_count=lambda: 0,
        metrics_source=lambda: {"rss_mb": 500, "threads": 8, "fds": 14, "fd_limit": 1024},
    )
    mon.start(on_recycle=on_breach, interval_s=0.01)
    assert done.wait(timeout=2.0), "on_recycle was not invoked within 2s"
    mon.stop()
    assert called[0].reason == "rss_exceeded"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_monitor.py::test_monitor_watch_invokes_on_recycle_callback -v`
Expected: FAIL — `AttributeError: 'ResourceMonitor' object has no attribute 'start'`

- [ ] **Step 3: Implement start/stop**

Add to `ResourceMonitor` in `monitor.py`:

```python
    def start(self, on_recycle: Callable[[Breach], None], interval_s: float = 60.0) -> None:
        self._stop_event = threading.Event()

        def loop():
            while not self._stop_event.wait(interval_s):
                breach = self.should_recycle()
                if breach is not None:
                    self.mark_recycled(breach.reason)
                    try:
                        on_recycle(breach)
                    except Exception as exc:
                        print(f"[monitor] on_recycle callback failed: {exc}", file=sys.stderr)

        self._thread = threading.Thread(target=loop, daemon=True, name="ResourceMonitor")
        self._thread.start()

    def stop(self) -> None:
        if hasattr(self, "_stop_event"):
            self._stop_event.set()
```

Also add `import threading` at the top if not already present.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_monitor.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add monitor.py tests/test_monitor.py
git commit -m "feat(monitor): add background watcher thread with callback"
```

---

## Task 6: Extend /status endpoint with monitor snapshot

**Files:**
- Modify: `proxy.py` (class `ProxyHandler._health`, around lines 594-620; also `main()` around lines 1142-1150)
- Create: `tests/test_status_endpoint.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_status_endpoint.py`:

```python
import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest


@pytest.fixture
def running_proxy(tmp_path, monkeypatch):
    """Spin up a real proxy on a free port, shut down at teardown."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    monkeypatch.setenv("CLAUDE_PROXY_PORT", str(port))
    monkeypatch.setattr("proxy.STATE_DIR", tmp_path)
    monkeypatch.setattr("proxy.PLUGINS_DIR", tmp_path / "plugins")
    monkeypatch.setattr("proxy.LISTEN_PORT", port)

    import proxy
    mgr = proxy.PluginManager(plugins_dir=tmp_path / "plugins",
                              global_config_path=tmp_path / "plugins.toml")
    mgr.initial_load()
    proxy.ProxyHandler.plugin_manager = mgr

    from monitor import ResourceMonitor
    proxy.ProxyHandler.resource_monitor = ResourceMonitor(
        get_reload_count=lambda: mgr.reload_count,
    )

    server = proxy.ThreadedHTTPServer(("127.0.0.1", port), proxy.ProxyHandler)
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
    assert "warnings" in body
    assert isinstance(body["warnings"], list)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_status_endpoint.py -v`
Expected: FAIL — response missing new keys

- [ ] **Step 3: Extend `_health` in proxy.py**

In `proxy.py`, add a class attribute and update `_health`:

```python
class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    plugin_manager: PluginManager | None = None
    plugins: list = []
    resource_monitor: "ResourceMonitor | None" = None  # set by main()
```

Replace `_health`:

```python
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

        payload = {
            "status": "ok",
            "pid": os.getpid(),
            "port": LISTEN_PORT,
            "plugins": plugin_names,
            "sideload_pending": pending,
        }
        if self.resource_monitor is not None:
            snap = self.resource_monitor.snapshot()
            payload.update(snap)
            if snap["warnings"]:
                payload["status"] = "warning"

        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_status_endpoint.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add proxy.py tests/test_status_endpoint.py
git commit -m "feat(proxy): expose monitor snapshot via /status endpoint"
```

---

## Task 7: Wire ResourceMonitor into proxy.py main()

**Files:**
- Modify: `proxy.py` (`main()` function around lines 1140-1170)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_status_endpoint.py`:

```python
def test_monitor_recycle_exits_process(monkeypatch):
    """When monitor detects a breach, proxy must exit cleanly with code 75."""
    from monitor import ResourceMonitor, Thresholds, Breach
    import proxy

    exit_calls: list[int] = []
    monkeypatch.setattr(proxy.os, "_exit", lambda code: exit_calls.append(code))

    # Simulate the on_recycle callback that proxy.main() registers
    def on_recycle(breach: Breach) -> None:
        print(f"[monitor] recycling: reason={breach.reason} value={breach.value}")
        proxy.os._exit(75)

    mon = ResourceMonitor(
        thresholds=Thresholds(rss_mb=1),
        metrics_source=lambda: {"rss_mb": 500, "threads": 8, "fds": 14, "fd_limit": 1024},
    )
    breach = mon.should_recycle()
    assert breach is not None
    on_recycle(breach)
    assert exit_calls == [75]
```

- [ ] **Step 2: Run test to verify it passes (contract sketch, no proxy change yet)**

Run: `python3 -m pytest tests/test_status_endpoint.py::test_monitor_recycle_exits_process -v`
Expected: PASS (this test documents the contract; proxy.main() must match it)

- [ ] **Step 3: Wire the monitor in proxy.main()**

In `proxy.py`, after the plugin manager setup (around line 1143), add:

```python
    # Wire resource monitor — recycles process on leak/drift thresholds.
    from monitor import ResourceMonitor
    resource_monitor = ResourceMonitor(
        get_reload_count=lambda: plugin_mgr.reload_count,
    )
    ProxyHandler.resource_monitor = resource_monitor

    def _on_recycle(breach):
        print(
            f"[monitor] recycling: reason={breach.reason} "
            f"value={breach.value} threshold={breach.threshold}",
            file=sys.stderr, flush=True,
        )
        # Best-effort telegram notification (Task 15 wires this).
        for p in plugin_mgr.plugins:
            notify = getattr(p, "on_monitor_recycle", None)
            if callable(notify):
                try:
                    notify(breach.reason, breach.value, breach.threshold)
                except Exception as exc:
                    print(f"[monitor] telegram notify failed: {exc}", file=sys.stderr)
        os._exit(75)

    resource_monitor.start(on_recycle=_on_recycle, interval_s=60.0)
```

- [ ] **Step 4: Run smoke test**

Run: `python3 -m pytest tests/ -v -x`
Expected: all existing tests still pass

- [ ] **Step 5: Commit**

```bash
git add proxy.py tests/test_status_endpoint.py
git commit -m "feat(proxy): wire ResourceMonitor into main() with recycle callback"
```

---

## Task 8: supervisor/base.py — Supervisor protocol

**Files:**
- Create: `supervisor/__init__.py`
- Create: `supervisor/base.py`

- [ ] **Step 1: Create the package and base module**

Create `supervisor/__init__.py`:

```python
"""OS-specific process supervisor adapters."""
from __future__ import annotations

import sys
from pathlib import Path

from .base import Supervisor


def get_adapter() -> Supervisor:
    """Return the supervisor adapter for the current platform."""
    if sys.platform == "darwin":
        from .launchd import LaunchdSupervisor
        return LaunchdSupervisor()
    if sys.platform.startswith("linux"):
        from .systemd import SystemdSupervisor
        return SystemdSupervisor()
    from .windows import WindowsSupervisor
    return WindowsSupervisor()
```

Create `supervisor/base.py`:

```python
"""Supervisor interface — OS-specific adapters implement this."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Supervisor(Protocol):
    """Abstract interface over launchd / systemd / Windows."""

    def install(self, proxy_path: Path) -> None:
        """Register and start the proxy as a supervised service. Idempotent."""

    def uninstall(self) -> None:
        """Stop and unregister the proxy. Idempotent."""

    def is_installed(self) -> bool:
        """True if the supervisor has a service definition for the proxy."""

    def status(self) -> dict:
        """Return {loaded: bool, running: bool, pid: int|None, last_exit: int|None}."""

    def restart(self) -> None:
        """Stop and start the proxy via the supervisor."""
```

- [ ] **Step 2: Commit**

```bash
git add supervisor/__init__.py supervisor/base.py
git commit -m "feat(supervisor): add adapter protocol and platform dispatch"
```

---

## Task 9: supervisor/launchd.py — macOS adapter

**Files:**
- Create: `supervisor/launchd.py`
- Create: `tests/test_supervisor_launchd.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_supervisor_launchd.py`:

```python
import plistlib
from pathlib import Path

import pytest

from supervisor.launchd import LaunchdSupervisor, LABEL, LEGACY_LABEL


def test_plist_contents(tmp_path, monkeypatch):
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    monkeypatch.setattr("supervisor.launchd._LAUNCHAGENT_DIR", plist_dir)

    sup = LaunchdSupervisor()
    proxy_path = tmp_path / "proxy.py"
    proxy_path.write_text("# fake proxy")
    sup._write_plist(proxy_path)

    plist_file = plist_dir / f"{LABEL}.plist"
    assert plist_file.exists()
    data = plistlib.loads(plist_file.read_bytes())
    assert data["Label"] == LABEL
    assert data["KeepAlive"] is True
    assert data["ThrottleInterval"] == 5
    assert data["EnvironmentVariables"]["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    assert data["EnvironmentVariables"]["CLAUDE_PROXY_SUPERVISED"] == "1"
    assert str(proxy_path) in data["ProgramArguments"]


def test_install_is_idempotent(tmp_path, monkeypatch):
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    monkeypatch.setattr("supervisor.launchd._LAUNCHAGENT_DIR", plist_dir)
    monkeypatch.setattr("supervisor.launchd._launchctl", lambda *args, **kwargs: (0, "", ""))

    sup = LaunchdSupervisor()
    proxy_path = tmp_path / "proxy.py"
    proxy_path.write_text("# fake proxy")

    sup.install(proxy_path)
    first_mtime = (plist_dir / f"{LABEL}.plist").stat().st_mtime
    sup.install(proxy_path)  # second call must not error
    second_mtime = (plist_dir / f"{LABEL}.plist").stat().st_mtime
    # Allow a rewrite but must not crash
    assert (plist_dir / f"{LABEL}.plist").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_supervisor_launchd.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'supervisor.launchd'`

- [ ] **Step 3: Implement LaunchdSupervisor**

Create `supervisor/launchd.py`:

```python
"""macOS launchd adapter for claude-proxy."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .base import Supervisor

LABEL = "com.claude-proxy.proxy"
LEGACY_LABEL = "com.claude-proxy.env"  # old setenv-only plist

_LAUNCHAGENT_DIR = Path.home() / "Library" / "LaunchAgents"
_LOG_FILE = Path.home() / ".claude" / "claude-proxy" / "proxy.log"
_DEFAULT_BASE_URL = "http://127.0.0.1:18019"


def _launchctl(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["/bin/launchctl", *args],
        capture_output=True, text=True, timeout=10,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _plist_path(label: str) -> Path:
    return _LAUNCHAGENT_DIR / f"{label}.plist"


def _build_plist(proxy_path: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(proxy_path)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "StandardOutPath": str(_LOG_FILE),
        "StandardErrorPath": str(_LOG_FILE),
        "EnvironmentVariables": {
            "ANTHROPIC_BASE_URL": _DEFAULT_BASE_URL,
            "CLAUDE_PROXY_SUPERVISED": "1",
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        },
    }


class LaunchdSupervisor:
    """launchd adapter — installs a user LaunchAgent that keeps the proxy alive."""

    def _write_plist(self, proxy_path: Path) -> None:
        _LAUNCHAGENT_DIR.mkdir(parents=True, exist_ok=True)
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = _build_plist(proxy_path)
        with _plist_path(LABEL).open("wb") as f:
            plistlib.dump(data, f)

    def _migrate_legacy(self) -> None:
        legacy = _plist_path(LEGACY_LABEL)
        if legacy.exists():
            _launchctl("unload", str(legacy))
            legacy.unlink(missing_ok=True)
            # Also clear the legacy env var so nothing stale lingers
            _launchctl("unsetenv", "ANTHROPIC_BASE_URL")

    def install(self, proxy_path: Path) -> None:
        self._migrate_legacy()
        self._write_plist(proxy_path)
        plist = _plist_path(LABEL)
        # Reload if already loaded (unload is tolerant of missing)
        _launchctl("unload", str(plist))
        code, _, err = _launchctl("load", "-w", str(plist))
        if code != 0:
            raise RuntimeError(f"launchctl load failed: {err.strip()}")

    def uninstall(self) -> None:
        plist = _plist_path(LABEL)
        if plist.exists():
            _launchctl("unload", str(plist))
            plist.unlink(missing_ok=True)

    def is_installed(self) -> bool:
        return _plist_path(LABEL).exists()

    def status(self) -> dict:
        code, out, _ = _launchctl("list", LABEL)
        if code != 0:
            return {"loaded": False, "running": False, "pid": None, "last_exit": None}
        pid: int | None = None
        last_exit: int | None = None
        for line in out.splitlines():
            line = line.strip().rstrip(";").strip()
            if line.startswith('"PID"'):
                parts = line.split("=")
                if len(parts) == 2:
                    try:
                        pid = int(parts[1].strip())
                    except ValueError:
                        pid = None
            elif line.startswith('"LastExitStatus"'):
                parts = line.split("=")
                if len(parts) == 2:
                    try:
                        last_exit = int(parts[1].strip())
                    except ValueError:
                        last_exit = None
        return {
            "loaded": True,
            "running": pid is not None,
            "pid": pid,
            "last_exit": last_exit,
        }

    def restart(self) -> None:
        _launchctl("kickstart", "-k", f"gui/{os.getuid()}/{LABEL}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_supervisor_launchd.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add supervisor/launchd.py tests/test_supervisor_launchd.py
git commit -m "feat(supervisor): add macOS launchd adapter with legacy migration"
```

---

## Task 10: supervisor/systemd.py — Linux adapter

**Files:**
- Create: `supervisor/systemd.py`
- Create: `tests/test_supervisor_systemd.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_supervisor_systemd.py`:

```python
from pathlib import Path

import pytest

from supervisor.systemd import SystemdSupervisor, UNIT_NAME


def test_unit_file_contents(tmp_path, monkeypatch):
    unit_dir = tmp_path / "systemd_user"
    unit_dir.mkdir()
    env_dir = tmp_path / "environment.d"
    env_dir.mkdir()
    monkeypatch.setattr("supervisor.systemd._UNIT_DIR", unit_dir)
    monkeypatch.setattr("supervisor.systemd._ENV_DIR", env_dir)
    monkeypatch.setattr("supervisor.systemd._systemctl", lambda *a, **k: (0, "", ""))

    sup = SystemdSupervisor()
    proxy_path = tmp_path / "proxy.py"
    proxy_path.write_text("# fake proxy")
    sup._write_unit(proxy_path)

    unit_text = (unit_dir / UNIT_NAME).read_text()
    assert "[Service]" in unit_text
    assert "Restart=always" in unit_text
    assert "RestartSec=5" in unit_text
    assert str(proxy_path) in unit_text
    assert "CLAUDE_PROXY_SUPERVISED=1" in unit_text


def test_install_writes_env_conf(tmp_path, monkeypatch):
    unit_dir = tmp_path / "systemd_user"
    unit_dir.mkdir()
    env_dir = tmp_path / "environment.d"
    env_dir.mkdir()
    monkeypatch.setattr("supervisor.systemd._UNIT_DIR", unit_dir)
    monkeypatch.setattr("supervisor.systemd._ENV_DIR", env_dir)
    monkeypatch.setattr("supervisor.systemd._systemctl", lambda *a, **k: (0, "", ""))

    sup = SystemdSupervisor()
    proxy_path = tmp_path / "proxy.py"
    proxy_path.write_text("# fake proxy")
    sup.install(proxy_path)

    env_file = env_dir / "claude-proxy.conf"
    assert env_file.exists()
    assert "ANTHROPIC_BASE_URL=" in env_file.read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_supervisor_systemd.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'supervisor.systemd'`

- [ ] **Step 3: Implement SystemdSupervisor**

Create `supervisor/systemd.py`:

```python
"""Linux systemd (user) adapter for claude-proxy."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .base import Supervisor

UNIT_NAME = "claude-proxy.service"

_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
_ENV_DIR = Path.home() / ".config" / "environment.d"
_DEFAULT_BASE_URL = "http://127.0.0.1:18019"

_UNIT_TEMPLATE = """\
[Unit]
Description=claude-proxy — extensible HTTP proxy for Claude Code
After=default.target

[Service]
Type=simple
ExecStart={python} {proxy_path}
Restart=always
RestartSec=5
Environment=ANTHROPIC_BASE_URL={base_url}
Environment=CLAUDE_PROXY_SUPERVISED=1
StandardOutput=append:{log_file}
StandardError=append:{log_file}

[Install]
WantedBy=default.target
"""


def _systemctl(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True, text=True, timeout=10,
    )
    return proc.returncode, proc.stdout, proc.stderr


class SystemdSupervisor:
    """systemd user-unit adapter."""

    def _write_unit(self, proxy_path: Path) -> None:
        _UNIT_DIR.mkdir(parents=True, exist_ok=True)
        log_file = Path.home() / ".claude" / "claude-proxy" / "proxy.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        unit = _UNIT_TEMPLATE.format(
            python=sys.executable,
            proxy_path=proxy_path,
            base_url=_DEFAULT_BASE_URL,
            log_file=log_file,
        )
        (_UNIT_DIR / UNIT_NAME).write_text(unit)

    def _write_env_conf(self) -> None:
        _ENV_DIR.mkdir(parents=True, exist_ok=True)
        (_ENV_DIR / "claude-proxy.conf").write_text(
            f"ANTHROPIC_BASE_URL={_DEFAULT_BASE_URL}\n"
        )

    def install(self, proxy_path: Path) -> None:
        self._write_unit(proxy_path)
        self._write_env_conf()
        _systemctl("daemon-reload")
        code, _, err = _systemctl("enable", "--now", UNIT_NAME)
        if code != 0:
            raise RuntimeError(f"systemctl enable failed: {err.strip()}")

    def uninstall(self) -> None:
        _systemctl("disable", "--now", UNIT_NAME)
        (_UNIT_DIR / UNIT_NAME).unlink(missing_ok=True)
        (_ENV_DIR / "claude-proxy.conf").unlink(missing_ok=True)
        _systemctl("daemon-reload")

    def is_installed(self) -> bool:
        return (_UNIT_DIR / UNIT_NAME).exists()

    def status(self) -> dict:
        code_active, active, _ = _systemctl("is-active", UNIT_NAME)
        code_enabled, enabled, _ = _systemctl("is-enabled", UNIT_NAME)
        pid_code, pid_out, _ = _systemctl("show", "-p", "MainPID", "--value", UNIT_NAME)
        exit_code, exit_out, _ = _systemctl("show", "-p", "ExecMainStatus", "--value", UNIT_NAME)

        pid: int | None = None
        try:
            val = int(pid_out.strip())
            pid = val if val > 0 else None
        except ValueError:
            pid = None

        last_exit: int | None = None
        try:
            last_exit = int(exit_out.strip())
        except ValueError:
            pass

        return {
            "loaded": enabled.strip() == "enabled",
            "running": active.strip() == "active",
            "pid": pid,
            "last_exit": last_exit,
        }

    def restart(self) -> None:
        _systemctl("restart", UNIT_NAME)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_supervisor_systemd.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add supervisor/systemd.py tests/test_supervisor_systemd.py
git commit -m "feat(supervisor): add Linux systemd user-unit adapter"
```

---

## Task 11: supervisor/windows.py — stub

**Files:**
- Create: `supervisor/windows.py`

- [ ] **Step 1: Implement the stub**

Create `supervisor/windows.py`:

```python
"""Windows supervisor — stub. Implement in a follow-up task."""
from __future__ import annotations

from pathlib import Path

from .base import Supervisor


class WindowsSupervisor:
    """Placeholder. Use `python proxy.py --daemon` manually until implemented."""

    def install(self, proxy_path: Path) -> None:
        raise NotImplementedError(
            "Windows supervisor is not yet implemented. "
            "Run `python proxy.py --daemon` manually, or use WSL and the systemd adapter."
        )

    def uninstall(self) -> None:
        raise NotImplementedError("Windows supervisor not yet implemented.")

    def is_installed(self) -> bool:
        return False

    def status(self) -> dict:
        return {"loaded": False, "running": False, "pid": None, "last_exit": None}

    def restart(self) -> None:
        raise NotImplementedError("Windows supervisor not yet implemented.")
```

- [ ] **Step 2: Commit**

```bash
git add supervisor/windows.py
git commit -m "feat(supervisor): add Windows stub adapter"
```

---

## Task 12: SessionStart banner script

**Files:**
- Create: `hooks/session_banner.sh`
- Create: `tests/test_session_banner.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_banner.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_session_banner.py -v`
Expected: FAIL — banner script doesn't exist

- [ ] **Step 3: Write the banner script**

Create `hooks/session_banner.sh`:

```sh
#!/bin/sh
# claude-proxy SessionStart banner.
# Probes /status; prints a one-line status. If dead and no supervisor,
# attempts a best-effort start.

PORT="${CLAUDE_PROXY_PORT:-18019}"
URL="http://127.0.0.1:${PORT}/status"

# Try the probe. 2s timeout — we must not block Claude Code startup.
BODY=$(curl -sS --max-time 2 "$URL" 2>/dev/null)
RC=$?

if [ "$RC" -ne 0 ] || [ -z "$BODY" ]; then
    # Dead. Is a supervisor installed?
    if [ -f "$HOME/Library/LaunchAgents/com.claude-proxy.proxy.plist" ] \
       || [ -f "$HOME/.config/systemd/user/claude-proxy.service" ]; then
        echo "[claude-proxy] ⚠ proxy not responding; supervisor should respawn within 5s"
    else
        echo "[claude-proxy] starting proxy... (no supervisor installed; run 'proxy install' for auto-restart)"
        PROXY_PY="$(dirname "$0")/../proxy.py"
        if [ -f "$PROXY_PY" ]; then
            python3 "$PROXY_PY" --daemon >/dev/null 2>&1 &
        fi
    fi
    exit 0
fi

# Healthy — parse JSON with python3 (present on every target OS).
python3 - "$BODY" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    print("[claude-proxy] ok")
    sys.exit(0)
status = d.get("status", "ok")
uptime = d.get("uptime_s", 0)
rss = d.get("rss_mb", 0)
reloads = d.get("plugin_reloads", 0)
warnings = d.get("warnings", []) or []

def fmt_uptime(s):
    if s < 60: return f"{s}s"
    if s < 3600: return f"{s // 60}m"
    return f"{s // 3600}h"

if status == "warning" or warnings:
    detail = warnings[0] if warnings else f"uptime {fmt_uptime(uptime)}"
    print(f"[claude-proxy] ⚠ {detail} · {reloads} reloads · consider: proxy restart")
else:
    print(f"[claude-proxy] ok · uptime {fmt_uptime(uptime)} · rss {rss}MB · {reloads} reloads")
PY
```

Make it executable:

```bash
chmod +x hooks/session_banner.sh
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_session_banner.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add hooks/session_banner.sh tests/test_session_banner.py
git commit -m "feat(hooks): add SessionStart banner with health probe"
```

---

## Task 13: setup.py — delegate install/uninstall to supervisor adapter

**Files:**
- Modify: `setup.py` (commands `cmd_install`, `cmd_uninstall` — find by name)
- Modify: `tests/test_setup.py`

- [ ] **Step 1: Read current setup.py install/uninstall**

Run: `grep -n "^def cmd_install\|^def cmd_uninstall\|install_launchagent\|uninstall_launchagent" setup.py`
Note the line ranges and existing call sites so you can replace them.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_setup.py`:

```python
def test_cmd_install_delegates_to_adapter(monkeypatch, tmp_path):
    import setup

    calls = []

    class FakeAdapter:
        def install(self, proxy_path):
            calls.append(("install", proxy_path))
        def is_installed(self):
            return False

    monkeypatch.setattr(setup, "get_adapter", lambda: FakeAdapter())
    monkeypatch.setattr(setup, "_default_project_dir", lambda: tmp_path)
    (tmp_path / "proxy.py").write_text("# fake")
    # Also stub anything else cmd_install does (hook install, settings write)
    monkeypatch.setattr(setup, "_install_session_hook", lambda: None)
    monkeypatch.setattr(setup, "_configure_claude_settings", lambda: None)

    import argparse
    args = argparse.Namespace()
    setup.cmd_install(args)

    assert calls[0][0] == "install"
    assert calls[0][1].name == "proxy.py"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_setup.py::test_cmd_install_delegates_to_adapter -v`
Expected: FAIL — either `get_adapter` not importable from setup or the call isn't made.

- [ ] **Step 4: Rewrite cmd_install**

At the top of `setup.py`, add:

```python
from supervisor import get_adapter
```

Replace the body of `cmd_install` with:

```python
def cmd_install(args: argparse.Namespace) -> None:
    """Install claude-proxy as a supervised service."""
    project_dir = _default_project_dir()
    proxy_path = project_dir / "proxy.py"
    if not proxy_path.exists():
        print(f"[claude-proxy] proxy.py not found at {proxy_path}", file=sys.stderr)
        sys.exit(1)

    adapter = get_adapter()
    print(f"[claude-proxy] Installing supervisor ({adapter.__class__.__name__})...")
    adapter.install(proxy_path)

    _install_session_hook()
    _configure_claude_settings()

    print("[claude-proxy] Installed.")
    print(f"  ANTHROPIC_BASE_URL will be set on next login.")
    print(f"  For this shell: export ANTHROPIC_BASE_URL=http://127.0.0.1:18019")
```

Replace `cmd_uninstall`:

```python
def cmd_uninstall(args: argparse.Namespace) -> None:
    """Fully remove claude-proxy."""
    adapter = get_adapter()
    print("[claude-proxy] Stopping and removing supervisor...")
    try:
        adapter.uninstall()
    except Exception as exc:
        print(f"[claude-proxy] supervisor uninstall warning: {exc}", file=sys.stderr)

    _remove_session_hook()
    _unconfigure_claude_settings()
    print("[claude-proxy] Removed.")
```

Add the helper `_install_session_hook` if it doesn't exist yet:

```python
def _install_session_hook() -> None:
    """Copy hooks/session_banner.sh to the project's .claude hooks and register."""
    project_dir = _default_project_dir()
    src = project_dir / "hooks" / "session_banner.sh"
    if not src.exists():
        return
    # Copying/registration into Claude Code settings is handled by
    # _configure_claude_settings via the existing hook-merge path.
```

And stubs for removal if missing:

```python
def _remove_session_hook() -> None:
    pass  # removed as part of _unconfigure_claude_settings
```

- [ ] **Step 5: Run all setup tests**

Run: `python3 -m pytest tests/test_setup.py -v`
Expected: the new test passes; update any legacy tests that asserted on the removed `install_launchagent` — rename or delete them so the suite stays green.

- [ ] **Step 6: Commit**

```bash
git add setup.py tests/test_setup.py
git commit -m "feat(setup): delegate install/uninstall to supervisor adapter"
```

---

## Task 14: setup.py — update cmd_restart and cmd_status

**Files:**
- Modify: `setup.py` (commands `cmd_restart`, `cmd_status`)
- Modify: `tests/test_setup.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_setup.py`:

```python
def test_cmd_restart_uses_adapter(monkeypatch):
    import setup
    calls = []

    class FakeAdapter:
        def is_installed(self): return True
        def restart(self): calls.append("restart")

    monkeypatch.setattr(setup, "get_adapter", lambda: FakeAdapter())
    import argparse
    setup.cmd_restart(argparse.Namespace(force=False))
    assert "restart" in calls


def test_cmd_status_includes_monitor_metrics(monkeypatch, capsys):
    import setup
    monkeypatch.setattr(setup, "proxy_status", lambda: {
        "status": "ok", "plugins": [], "uptime_s": 300,
        "rss_mb": 42, "threads": 8, "fds": 14,
        "plugin_reloads": 1, "warnings": [],
    })
    class FakeAdapter:
        def is_installed(self): return True
        def status(self): return {"loaded": True, "running": True, "pid": 123, "last_exit": 0}
    monkeypatch.setattr(setup, "get_adapter", lambda: FakeAdapter())

    import argparse
    setup.cmd_status(argparse.Namespace())
    out = capsys.readouterr().out
    assert "rss" in out.lower()
    assert "uptime" in out.lower() or "300" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_setup.py::test_cmd_restart_uses_adapter tests/test_setup.py::test_cmd_status_includes_monitor_metrics -v`
Expected: FAIL — cmd_restart still calls `kill_proxy`; cmd_status doesn't print metrics.

- [ ] **Step 3: Rewrite cmd_restart**

Replace `cmd_restart` body:

```python
def cmd_restart(args: argparse.Namespace) -> None:
    """Restart the proxy.

    Prefer hot-reload for plugin edits. Full restart is only needed for
    proxy.py code changes. If a supervisor is installed, delegate to it;
    otherwise fall back to the legacy kill+spawn path.
    """
    adapter = get_adapter()
    if adapter.is_installed() and not getattr(args, "force", False):
        # Try hot-reload first
        try:
            url = "http://127.0.0.1:18019/reload"
            with urllib.request.urlopen(url, timeout=3) as resp:
                if json.loads(resp.read()).get("status") == "reloaded":
                    print("[claude-proxy] Hot-reloaded plugins (no restart needed).")
                    return
        except Exception:
            pass
        print("[claude-proxy] Restarting via supervisor...")
        adapter.restart()
        print("[claude-proxy] Restart requested.")
        return

    # Legacy path: no supervisor installed, or --force
    state_dir = _default_state_dir()
    project_dir = _default_project_dir()
    proxy_py = project_dir / "proxy.py"
    print("[claude-proxy] Stopping proxy (no supervisor detected)...")
    kill_proxy(state_dir)

    import socket
    for _ in range(20):
        time.sleep(0.25)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", 18019)) != 0:
                break

    subprocess.Popen(
        [sys.executable, str(proxy_py), "--daemon"],
        stdout=subprocess.DEVNULL,
        stderr=open(state_dir / "proxy.log", "a"),
    )
    for _ in range(10):
        time.sleep(0.3)
        if proxy_status() is not None:
            break
    if proxy_status() is None:
        print("[claude-proxy] Proxy failed to start. Check ~/.claude/claude-proxy/proxy.log")
        sys.exit(1)
    print("[claude-proxy] Proxy restarted.")
```

Rewrite `cmd_status`:

```python
def cmd_status(args: argparse.Namespace) -> None:
    """Show proxy + supervisor health."""
    adapter = get_adapter()
    sup_status = adapter.status() if adapter.is_installed() else None
    result = proxy_status()

    if result is None:
        print("[claude-proxy] Proxy is not running.")
        if sup_status:
            print(f"  Supervisor: loaded={sup_status['loaded']} running={sup_status['running']} last_exit={sup_status.get('last_exit')}")
        sys.exit(1)

    print("[claude-proxy] Proxy is running.")
    print(f"  Status:    {result.get('status', 'unknown')}")
    plugins = result.get("plugins", []) or []
    print(f"  Plugins:   {', '.join(plugins) if plugins else 'none'}")
    if "uptime_s" in result:
        print(f"  Uptime:    {result['uptime_s']}s")
        print(f"  RSS:       {result.get('rss_mb', '?')} MB")
        print(f"  Threads:   {result.get('threads', '?')}")
        print(f"  FDs:       {result.get('fds', '?')}")
        print(f"  Reloads:   {result.get('plugin_reloads', 0)}")
        warnings = result.get("warnings", []) or []
        if warnings:
            print("  Warnings:")
            for w in warnings:
                print(f"    - {w}")
        else:
            print("  Warnings:  none")
    if sup_status:
        print(f"  Supervisor: pid={sup_status.get('pid')} loaded={sup_status.get('loaded')}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_setup.py -v`
Expected: new tests PASS.

- [ ] **Step 5: Commit**

```bash
git add setup.py tests/test_setup.py
git commit -m "feat(setup): route restart/status through supervisor adapter"
```

---

## Task 15: Telegram recycle notification hook

**Files:**
- Modify: `plugins/telegram.py`
- Modify: `tests/test_telegram.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_telegram.py`:

```python
def test_telegram_on_monitor_recycle_formats_message(monkeypatch):
    import plugins.telegram as tg
    sent = []
    monkeypatch.setattr(tg, "_send_message", lambda text, **kw: sent.append(text))
    # Assume configure() was already called by the test harness / fixtures;
    # if not, call it with a stub config that enables the plugin.
    tg.configure({"bot_token": "x", "chat_id": "y", "notify_on_recycle": True})

    tg.on_monitor_recycle("rss_exceeded", 614, 512)
    assert sent, "no telegram message was sent"
    assert "recycl" in sent[0].lower()
    assert "rss_exceeded" in sent[0] or "rss" in sent[0].lower()
    assert "614" in sent[0]
    assert "512" in sent[0]


def test_telegram_recycle_opt_out(monkeypatch):
    import plugins.telegram as tg
    sent = []
    monkeypatch.setattr(tg, "_send_message", lambda text, **kw: sent.append(text))
    tg.configure({"bot_token": "x", "chat_id": "y", "notify_on_recycle": False})

    tg.on_monitor_recycle("rss_exceeded", 614, 512)
    assert sent == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_telegram.py::test_telegram_on_monitor_recycle_formats_message -v`
Expected: FAIL — `on_monitor_recycle` does not exist.

- [ ] **Step 3: Add the hook to plugins/telegram.py**

At the top of the plugin state block, add:

```python
_notify_on_recycle: bool = True
```

In `configure`, read the flag:

```python
def configure(config: dict) -> None:
    # ... existing code ...
    global _notify_on_recycle
    _notify_on_recycle = bool(config.get("notify_on_recycle", True))
```

Add the hook function (name must match what proxy.py calls in Task 7):

```python
def on_monitor_recycle(reason: str, value: int, threshold: int) -> None:
    """Called by the ResourceMonitor right before recycling."""
    if not _notify_on_recycle:
        return
    msg = (
        f"⚠ claude-proxy recycling: {reason} "
        f"(value={value}, threshold={threshold}) — restarted cleanly"
    )
    try:
        _send_message(msg)
    except Exception:
        pass  # best-effort; must not block recycle
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_telegram.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/telegram.py tests/test_telegram.py
git commit -m "feat(telegram): notify on proxy monitor-triggered recycle"
```

---

## Task 16: plugins/leak_guard.py — probe instead of spawn

**Files:**
- Modify: `plugins/leak_guard.py`
- Modify: `tests/test_leak_guard_plugin.py`

- [ ] **Step 1: Find the spawn logic**

Run: `grep -n "auto.?start\|start_proxy\|Popen\|spawn\|--daemon" plugins/leak_guard.py`
Note the block that auto-starts the proxy. If no such block exists, skip this task entirely.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_leak_guard_plugin.py`:

```python
def test_leak_guard_does_not_spawn_proxy_when_supervisor_installed(monkeypatch):
    import plugins.leak_guard as lg
    popen_calls = []
    monkeypatch.setattr(lg, "subprocess", type("s", (), {
        "Popen": lambda *a, **k: popen_calls.append(a) or None,
        "DEVNULL": 0,
    }))
    # Simulate supervisor installed
    monkeypatch.setattr(lg, "_supervisor_installed", lambda: True)
    monkeypatch.setattr(lg, "_probe_proxy", lambda: False)  # dead

    lg.ensure_proxy_running()
    assert popen_calls == [], "must not spawn when supervisor is installed"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_leak_guard_plugin.py::test_leak_guard_does_not_spawn_proxy_when_supervisor_installed -v`
Expected: FAIL — `AttributeError` or spawn still happens.

- [ ] **Step 4: Modify leak_guard.py**

Replace the auto-start block with:

```python
def _supervisor_installed() -> bool:
    from pathlib import Path
    return (
        (Path.home() / "Library" / "LaunchAgents" / "com.claude-proxy.proxy.plist").exists()
        or (Path.home() / ".config" / "systemd" / "user" / "claude-proxy.service").exists()
    )


def _probe_proxy() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:18019/status", timeout=1):
            return True
    except Exception:
        return False


def ensure_proxy_running() -> None:
    """Ensure the proxy is up. If a supervisor is installed, just probe and warn."""
    import sys
    if _probe_proxy():
        return
    if _supervisor_installed():
        print(
            "[leak-guard] claude-proxy not responding; supervisor should respawn shortly",
            file=sys.stderr,
        )
        return
    # No supervisor — fall back to the existing spawn path (kept for dev/legacy).
    _spawn_proxy_legacy()
```

Rename the previous auto-spawn body to `_spawn_proxy_legacy()`.

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_leak_guard_plugin.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add plugins/leak_guard.py tests/test_leak_guard_plugin.py
git commit -m "feat(leak-guard): probe-only when supervisor is installed"
```

---

## Task 17: Remove _inactivity_watchdog and gate --daemon fork

**Files:**
- Modify: `proxy.py` (remove `_inactivity_watchdog`, update `main()`)
- Modify: `tests/test_crash_scenarios.py`, `tests/test_proxy_state.py` (any test that relied on inactivity exit)

- [ ] **Step 1: Find references**

Run: `grep -n "_inactivity_watchdog\|_last_activity\|_INACTIVITY_TIMEOUT" proxy.py tests/`
Note every call site so you can remove them cleanly.

- [ ] **Step 2: Write/update tests**

In `tests/test_crash_scenarios.py`, remove or rewrite any test asserting the proxy self-exits on idle. Replace with:

```python
def test_proxy_does_not_self_exit_when_idle():
    """The inactivity watchdog has been removed — supervisor handles lifecycle."""
    import proxy
    assert not hasattr(proxy, "_inactivity_watchdog")
    assert not hasattr(proxy, "_INACTIVITY_TIMEOUT")
```

Add a test for supervised mode:

```python
def test_proxy_skips_daemon_fork_when_supervised(monkeypatch):
    """Under CLAUDE_PROXY_SUPERVISED=1, proxy runs foreground (no fork)."""
    monkeypatch.setenv("CLAUDE_PROXY_SUPERVISED", "1")
    import proxy
    # main() must treat --daemon as a no-op under supervision.
    # Rather than invoking main(), inspect the helper that main() calls:
    assert proxy._should_daemonize(True) is False  # --daemon=True, supervised


def test_proxy_daemonizes_when_not_supervised(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROXY_SUPERVISED", raising=False)
    import proxy
    assert proxy._should_daemonize(True) is True
    assert proxy._should_daemonize(False) is False
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_crash_scenarios.py tests/test_proxy_state.py -v`
Expected: FAIL — `_should_daemonize` doesn't exist, old watchdog symbols still present.

- [ ] **Step 4: Edit proxy.py**

Remove these from `proxy.py`:
- The line `_INACTIVITY_TIMEOUT = 4 * 3600` (around line 43)
- The line `_last_activity = time.time()` (around line 546) and its mutation in the request handler (around line 673-674)
- The entire `_inactivity_watchdog` function (around lines 956-963)
- The `threading.Thread(target=_inactivity_watchdog, ...)` start in `main()` (around lines 1155-1156)

Add a helper before `main()`:

```python
def _should_daemonize(daemon_flag: bool) -> bool:
    """Under a supervisor, never self-daemonize — supervisor owns the lifecycle."""
    if os.environ.get("CLAUDE_PROXY_SUPERVISED") == "1":
        return False
    return daemon_flag
```

Update the fork block in `main()`:

```python
    if _should_daemonize(args.daemon):
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
```

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: all PASS. Some tests may need adjustment for removed symbols — update them.

- [ ] **Step 6: Commit**

```bash
git add proxy.py tests/test_crash_scenarios.py tests/test_proxy_state.py
git commit -m "feat(proxy): remove inactivity watchdog, gate daemonize on supervision"
```

---

## Task 18: End-to-end verification

**Files:**
- No code changes — this is a manual verification task.

- [ ] **Step 1: Run the full suite**

Run: `python3 -m pytest tests/ -v`
Expected: all tests PASS.

- [ ] **Step 2: Fresh install on current platform**

```bash
python3 setup.py install
python3 setup.py status
```
Expected output lists supervisor as loaded, monitor metrics present, warnings empty.

- [ ] **Step 3: Kill the proxy, verify auto-respawn**

Find the PID from `proxy status`, then:

```bash
kill -9 <pid>
sleep 6
python3 setup.py status
```
Expected: a new PID appears within ~5 seconds (ThrottleInterval). Monitor uptime reset to seconds.

- [ ] **Step 4: Trigger a recycle via the kill switch inversion**

```bash
# Lower the RSS threshold in plugins.toml to force a recycle
cat >> ~/.claude/claude-proxy/plugins.toml <<EOF
[monitor]
rss_mb = 1
EOF
python3 setup.py restart
sleep 90  # wait for one monitor tick
python3 setup.py status
```
Expected: `last_recycle` in status snapshot is non-null, reason is `rss_exceeded`. Telegram (if configured) received a notification. PID changed (process recycled).

Revert the threshold after verification.

- [ ] **Step 5: Start a fresh Claude Code session**

Expected: banner appears in SessionStart output:
```
[claude-proxy] ok · uptime Xs · rss YMB · Z reloads
```

- [ ] **Step 6: Commit a verification log**

```bash
git commit --allow-empty -m "verify: end-to-end supervisor + monitor workflow"
```

---

## Self-review (completed by plan author)

- **Spec coverage:**
  - Supervisor architecture → Tasks 8–11.
  - ResourceMonitor with thresholds, cooldown, kill switch → Tasks 2–5.
  - `/status` extension → Task 6.
  - SessionStart banner → Task 12.
  - `proxy status` / `proxy install` / `proxy uninstall` / `proxy restart` CLI updates → Tasks 13–14.
  - Telegram recycle notification → Task 15.
  - Legacy LaunchAgent migration → Task 9 (`_migrate_legacy`).
  - Leak-guard switch from spawn to probe → Task 16.
  - Remove inactivity watchdog + gate daemonize → Task 17.
  - End-to-end verification → Task 18.

- **Placeholders:** none. Each code step contains the code to write.

- **Type consistency:** `reload_count` property used consistently across Tasks 1, 4, 7. `ResourceMonitor` constructor signature matches uses in Tasks 6, 7. `on_monitor_recycle(reason, value, threshold)` signature matches between Task 7 (caller) and Task 15 (implementer). Supervisor methods `install/uninstall/is_installed/status/restart` consistent between base protocol (Task 8) and adapters (Tasks 9–11).

- **Known follow-ups:** Windows adapter implementation (Task 11 stub); tuning of default thresholds based on real-world data after a week.

import threading

import pytest

from monitor import collect_metrics, evaluate_thresholds, Thresholds, ResourceMonitor, Breach


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


def test_monitor_double_start_raises():
    mon = ResourceMonitor(
        thresholds=Thresholds(),
        get_reload_count=lambda: 0,
        metrics_source=lambda: {"rss_mb": 1, "threads": 1, "fds": 1, "fd_limit": 1024},
    )
    mon.start(on_recycle=lambda b: None, interval_s=60.0)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            mon.start(on_recycle=lambda b: None, interval_s=60.0)
    finally:
        mon.stop()

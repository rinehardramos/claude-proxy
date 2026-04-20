from monitor import collect_metrics, evaluate_thresholds, Thresholds


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

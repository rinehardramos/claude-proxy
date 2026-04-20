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

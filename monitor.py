"""ResourceMonitor — detect accumulated weirdness and recycle the proxy.

Collects RSS, thread count, and open fd count. Uses psutil when available,
falls back to platform-specific stdlib probes otherwise.
"""
from __future__ import annotations

import os
import resource
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

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

    def start(self, on_recycle: Callable[[Breach], None], interval_s: float = 60.0) -> None:
        if hasattr(self, "_stop_event") and not self._stop_event.is_set():
            raise RuntimeError("ResourceMonitor is already running; call stop() first")
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

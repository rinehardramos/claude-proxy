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

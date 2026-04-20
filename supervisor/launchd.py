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

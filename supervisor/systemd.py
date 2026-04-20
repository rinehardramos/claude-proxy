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

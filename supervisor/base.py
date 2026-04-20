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

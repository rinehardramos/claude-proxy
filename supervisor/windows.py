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

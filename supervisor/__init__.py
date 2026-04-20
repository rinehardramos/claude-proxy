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

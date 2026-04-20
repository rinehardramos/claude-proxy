import plistlib
from pathlib import Path

import pytest

from supervisor.launchd import LaunchdSupervisor, LABEL, LEGACY_LABEL


def test_plist_contents(tmp_path, monkeypatch):
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    monkeypatch.setattr("supervisor.launchd._LAUNCHAGENT_DIR", plist_dir)

    sup = LaunchdSupervisor()
    proxy_path = tmp_path / "proxy.py"
    proxy_path.write_text("# fake proxy")
    sup._write_plist(proxy_path)

    plist_file = plist_dir / f"{LABEL}.plist"
    assert plist_file.exists()
    data = plistlib.loads(plist_file.read_bytes())
    assert data["Label"] == LABEL
    assert data["KeepAlive"] is True
    assert data["ThrottleInterval"] == 5
    assert data["EnvironmentVariables"]["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    assert data["EnvironmentVariables"]["CLAUDE_PROXY_SUPERVISED"] == "1"
    assert str(proxy_path) in data["ProgramArguments"]


def test_install_is_idempotent(tmp_path, monkeypatch):
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    monkeypatch.setattr("supervisor.launchd._LAUNCHAGENT_DIR", plist_dir)
    monkeypatch.setattr("supervisor.launchd._launchctl", lambda *args, **kwargs: (0, "", ""))

    sup = LaunchdSupervisor()
    proxy_path = tmp_path / "proxy.py"
    proxy_path.write_text("# fake proxy")

    sup.install(proxy_path)
    first_mtime = (plist_dir / f"{LABEL}.plist").stat().st_mtime
    sup.install(proxy_path)  # second call must not error
    second_mtime = (plist_dir / f"{LABEL}.plist").stat().st_mtime
    # Allow a rewrite but must not crash
    assert (plist_dir / f"{LABEL}.plist").exists()

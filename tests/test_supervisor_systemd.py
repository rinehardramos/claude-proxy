from pathlib import Path

import pytest

from supervisor.systemd import SystemdSupervisor, UNIT_NAME


def test_unit_file_contents(tmp_path, monkeypatch):
    unit_dir = tmp_path / "systemd_user"
    unit_dir.mkdir()
    env_dir = tmp_path / "environment.d"
    env_dir.mkdir()
    monkeypatch.setattr("supervisor.systemd._UNIT_DIR", unit_dir)
    monkeypatch.setattr("supervisor.systemd._ENV_DIR", env_dir)
    monkeypatch.setattr("supervisor.systemd._systemctl", lambda *a, **k: (0, "", ""))

    sup = SystemdSupervisor()
    proxy_path = tmp_path / "proxy.py"
    proxy_path.write_text("# fake proxy")
    sup._write_unit(proxy_path)

    unit_text = (unit_dir / UNIT_NAME).read_text()
    assert "[Service]" in unit_text
    assert "Restart=always" in unit_text
    assert "RestartSec=5" in unit_text
    assert str(proxy_path) in unit_text
    assert "CLAUDE_PROXY_SUPERVISED=1" in unit_text


def test_install_writes_env_conf(tmp_path, monkeypatch):
    unit_dir = tmp_path / "systemd_user"
    unit_dir.mkdir()
    env_dir = tmp_path / "environment.d"
    env_dir.mkdir()
    monkeypatch.setattr("supervisor.systemd._UNIT_DIR", unit_dir)
    monkeypatch.setattr("supervisor.systemd._ENV_DIR", env_dir)
    monkeypatch.setattr("supervisor.systemd._systemctl", lambda *a, **k: (0, "", ""))

    sup = SystemdSupervisor()
    proxy_path = tmp_path / "proxy.py"
    proxy_path.write_text("# fake proxy")
    sup.install(proxy_path)

    env_file = env_dir / "claude-proxy.conf"
    assert env_file.exists()
    assert "ANTHROPIC_BASE_URL=" in env_file.read_text()

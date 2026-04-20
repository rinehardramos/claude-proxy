"""Tests for setup.py -- library functions and CLI."""
from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from io import StringIO
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, str(Path(__file__).parent.parent))
from setup import (
    create_runtime_dirs,
    install_plugins,
    write_plugins_toml,
    patch_settings_json,
    unpatch_settings_json,
    patch_shell_profile,
    unpatch_shell_profile,
    kill_proxy,
    detect_shell_profile,
    build_parser,
    main,
    _COMMANDS,
    HOOK_MARKER,
    SESSION_START_HOOK_COMMAND,
)


# ── Helpers ────────────────────────────────────────────────────────────────

def _minimal_settings() -> dict:
    return {"permissions": {"allow": []}, "hooks": {}}


def _settings_with_hook(command: str) -> dict:
    return {
        "permissions": {"allow": []},
        "hooks": {
            "SessionStart": [
                {"matcher": "startup", "hooks": [{"type": "command", "command": command}]}
            ]
        },
    }


# ── create_runtime_dirs ────────────────────────────────────────────────────

class TestCreateRuntimeDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_dir = Path(self.tmp) / "claude-proxy"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_state_dir(self):
        create_runtime_dirs(self.state_dir)
        self.assertTrue(self.state_dir.exists())

    def test_creates_plugins_subdir(self):
        create_runtime_dirs(self.state_dir)
        self.assertTrue((self.state_dir / "plugins").is_dir())

    def test_creates_sideload_inbound(self):
        create_runtime_dirs(self.state_dir)
        self.assertTrue((self.state_dir / "sideload" / "inbound").is_dir())

    def test_creates_sideload_outbound(self):
        create_runtime_dirs(self.state_dir)
        self.assertTrue((self.state_dir / "sideload" / "outbound").is_dir())

    def test_idempotent(self):
        create_runtime_dirs(self.state_dir)
        create_runtime_dirs(self.state_dir)  # should not raise
        self.assertTrue(self.state_dir.exists())


# ── install_plugins ────────────────────────────────────────────────────────

class TestInstallPlugins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src_dir = Path(self.tmp) / "src_plugins"
        self.dst_dir = Path(self.tmp) / "dst_plugins"
        self.src_dir.mkdir()
        self.dst_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_copies_py_files(self):
        (self.src_dir / "telegram.py").write_text("# telegram")
        install_plugins(self.src_dir, self.dst_dir)
        self.assertTrue((self.dst_dir / "telegram.py").exists())

    def test_copies_file_contents(self):
        (self.src_dir / "telegram.py").write_text("CONTENT = 42")
        install_plugins(self.src_dir, self.dst_dir)
        self.assertEqual((self.dst_dir / "telegram.py").read_text(), "CONTENT = 42")

    def test_copies_multiple_plugins(self):
        (self.src_dir / "a.py").write_text("a")
        (self.src_dir / "b.py").write_text("b")
        install_plugins(self.src_dir, self.dst_dir)
        self.assertTrue((self.dst_dir / "a.py").exists())
        self.assertTrue((self.dst_dir / "b.py").exists())

    def test_does_not_copy_non_py_files(self):
        (self.src_dir / "readme.txt").write_text("readme")
        install_plugins(self.src_dir, self.dst_dir)
        self.assertFalse((self.dst_dir / "readme.txt").exists())

    def test_overwrites_existing(self):
        (self.src_dir / "telegram.py").write_text("new content")
        (self.dst_dir / "telegram.py").write_text("old content")
        install_plugins(self.src_dir, self.dst_dir)
        self.assertEqual((self.dst_dir / "telegram.py").read_text(), "new content")

    def test_copies_toml_files(self):
        (self.src_dir / "telegram.toml").write_text("enabled = false")
        install_plugins(self.src_dir, self.dst_dir)
        self.assertTrue((self.dst_dir / "telegram.toml").exists())

    def test_does_not_overwrite_existing_toml(self):
        (self.src_dir / "telegram.toml").write_text("new defaults")
        (self.dst_dir / "telegram.toml").write_text("user config")
        install_plugins(self.src_dir, self.dst_dir)
        self.assertEqual((self.dst_dir / "telegram.toml").read_text(), "user config")


# ── write_plugins_toml ─────────────────────────────────────────────────────

class TestWritePluginsToml(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.toml_path = Path(self.tmp) / "plugins.toml"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_file_when_missing(self):
        write_plugins_toml(self.toml_path)
        self.assertTrue(self.toml_path.exists())

    def test_file_contains_enabled_key(self):
        write_plugins_toml(self.toml_path)
        self.assertIn("enabled", self.toml_path.read_text())

    def test_does_not_overwrite_existing(self):
        self.toml_path.write_text("my custom config")
        write_plugins_toml(self.toml_path)
        self.assertEqual(self.toml_path.read_text(), "my custom config")


# ── patch_settings_json ────────────────────────────────────────────────────

class TestPatchSettingsJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.settings = Path(self.tmp) / "settings.json"
        self.proxy_py = Path(self.tmp) / "proxy.py"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, data: dict):
        self.settings.write_text(json.dumps(data, indent=2))

    def _read(self) -> dict:
        return json.loads(self.settings.read_text())

    def test_creates_session_start_hook(self):
        self._write(_minimal_settings())
        patch_settings_json(self.settings, self.proxy_py)
        data = self._read()
        hooks = data.get("hooks", {}).get("SessionStart", [])
        self.assertTrue(len(hooks) > 0)

    def test_hook_has_startup_matcher(self):
        self._write(_minimal_settings())
        patch_settings_json(self.settings, self.proxy_py)
        hooks = self._read()["hooks"]["SessionStart"]
        matchers = [h.get("matcher") for h in hooks]
        self.assertIn("startup", matchers)

    def test_hook_command_references_proxy_py(self):
        self._write(_minimal_settings())
        patch_settings_json(self.settings, self.proxy_py)
        hooks = self._read()["hooks"]["SessionStart"]
        startup = next(h for h in hooks if h.get("matcher") == "startup")
        cmd = startup["hooks"][0]["command"]
        self.assertIn(str(self.proxy_py), cmd)

    def test_hook_command_has_daemon_flag(self):
        self._write(_minimal_settings())
        patch_settings_json(self.settings, self.proxy_py)
        hooks = self._read()["hooks"]["SessionStart"]
        startup = next(h for h in hooks if h.get("matcher") == "startup")
        cmd = startup["hooks"][0]["command"]
        self.assertIn("--daemon", cmd)

    def test_idempotent_does_not_add_duplicate(self):
        self._write(_minimal_settings())
        patch_settings_json(self.settings, self.proxy_py)
        patch_settings_json(self.settings, self.proxy_py)
        hooks = self._read()["hooks"]["SessionStart"]
        startup_hooks = [h for h in hooks if h.get("matcher") == "startup"
                         and HOOK_MARKER in json.dumps(h)]
        self.assertEqual(len(startup_hooks), 1)

    def test_preserves_existing_session_start_hooks(self):
        self._write(_settings_with_hook("echo existing"))
        patch_settings_json(self.settings, self.proxy_py)
        hooks = self._read()["hooks"]["SessionStart"]
        cmds = [h["hooks"][0]["command"] for h in hooks]
        self.assertIn("echo existing", cmds)

    def test_preserves_other_hook_types(self):
        data = _minimal_settings()
        data["hooks"]["PreToolUse"] = [{"hooks": [{"type": "command", "command": "pre"}]}]
        self._write(data)
        patch_settings_json(self.settings, self.proxy_py)
        self.assertIn("PreToolUse", self._read()["hooks"])

    def test_creates_settings_file_if_missing(self):
        patch_settings_json(self.settings, self.proxy_py)
        self.assertTrue(self.settings.exists())

    def test_hook_command_has_marker_comment(self):
        self._write(_minimal_settings())
        patch_settings_json(self.settings, self.proxy_py)
        hooks = self._read()["hooks"]["SessionStart"]
        startup = next(h for h in hooks if h.get("matcher") == "startup"
                       and HOOK_MARKER in json.dumps(h))
        self.assertIn(HOOK_MARKER, json.dumps(startup))


# ── unpatch_settings_json ──────────────────────────────────────────────────

class TestUnpatchSettingsJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.settings = Path(self.tmp) / "settings.json"
        self.proxy_py = Path(self.tmp) / "proxy.py"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, data: dict):
        self.settings.write_text(json.dumps(data, indent=2))

    def _read(self) -> dict:
        return json.loads(self.settings.read_text())

    def test_removes_claude_proxy_hook(self):
        self._write(_minimal_settings())
        patch_settings_json(self.settings, self.proxy_py)
        unpatch_settings_json(self.settings)
        hooks = self._read().get("hooks", {}).get("SessionStart", [])
        marker_hooks = [h for h in hooks if HOOK_MARKER in json.dumps(h)]
        self.assertEqual(len(marker_hooks), 0)

    def test_preserves_other_session_start_hooks(self):
        self._write(_settings_with_hook("echo keep"))
        patch_settings_json(self.settings, self.proxy_py)
        unpatch_settings_json(self.settings)
        hooks = self._read().get("hooks", {}).get("SessionStart", [])
        cmds = [h["hooks"][0]["command"] for h in hooks]
        self.assertIn("echo keep", cmds)

    def test_noop_when_not_installed(self):
        self._write(_minimal_settings())
        unpatch_settings_json(self.settings)  # should not raise
        self.assertTrue(self.settings.exists())

    def test_noop_when_settings_missing(self):
        unpatch_settings_json(self.settings)  # should not raise


# ── patch_shell_profile ────────────────────────────────────────────────────

class TestPatchShellProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.profile = Path(self.tmp) / ".zshrc"
        self.proxy_py = Path(self.tmp) / "proxy.py"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_profile_if_missing(self):
        patch_shell_profile(self.profile, self.proxy_py)
        self.assertTrue(self.profile.exists())

    def test_appends_anthropic_base_url_export(self):
        patch_shell_profile(self.profile, self.proxy_py)
        self.assertIn("ANTHROPIC_BASE_URL", self.profile.read_text())

    def test_appends_correct_url(self):
        patch_shell_profile(self.profile, self.proxy_py)
        self.assertIn("http://127.0.0.1:18019", self.profile.read_text())

    def test_idempotent_does_not_duplicate(self):
        patch_shell_profile(self.profile, self.proxy_py)
        patch_shell_profile(self.profile, self.proxy_py)
        count = self.profile.read_text().count("ANTHROPIC_BASE_URL")
        self.assertEqual(count, 1)

    def test_preserves_existing_content(self):
        self.profile.write_text("export EDITOR=vim\n")
        patch_shell_profile(self.profile, self.proxy_py)
        self.assertIn("EDITOR=vim", self.profile.read_text())

    def test_has_marker_to_identify_block(self):
        patch_shell_profile(self.profile, self.proxy_py)
        self.assertIn(HOOK_MARKER, self.profile.read_text())

    def test_block_starts_proxy_daemon(self):
        patch_shell_profile(self.profile, self.proxy_py)
        text = self.profile.read_text()
        self.assertIn(str(self.proxy_py), text)
        self.assertIn("--daemon", text)

    def test_url_is_conditional_on_health_check(self):
        """ANTHROPIC_BASE_URL must only be exported if the proxy is healthy."""
        patch_shell_profile(self.profile, self.proxy_py)
        text = self.profile.read_text()
        # The export line must follow a health check, not be unconditional
        export_pos = text.find("export ANTHROPIC_BASE_URL")
        curl_pos = text.find("/status")
        self.assertGreater(export_pos, 0)
        self.assertGreater(curl_pos, 0)
        self.assertLess(curl_pos, export_pos)  # health check comes before export


# ── detect_shell_profile ───────────────────────────────────────────────────

class TestDetectShellProfile(unittest.TestCase):
    def test_returns_a_path(self):
        self.assertIsInstance(detect_shell_profile(), Path)

    def test_returns_zshrc_when_zsh(self):
        with patch.dict("os.environ", {"SHELL": "/bin/zsh"}):
            p = detect_shell_profile()
        self.assertTrue(str(p).endswith(".zshrc"))

    def test_returns_bash_profile_when_bash(self):
        with patch.dict("os.environ", {"SHELL": "/bin/bash"}):
            p = detect_shell_profile()
        self.assertTrue(str(p).endswith(".bash_profile") or str(p).endswith(".bashrc"))

    def test_defaults_to_zshrc_when_shell_unknown(self):
        with patch.dict("os.environ", {"SHELL": "/bin/fish"}, clear=False):
            p = detect_shell_profile()
        self.assertTrue(str(p).endswith(".zshrc"))


# ── unpatch_shell_profile ──────────────────────────────────────────────────

class TestUnpatchShellProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.profile = Path(self.tmp) / ".zshrc"
        self.proxy_py = Path(self.tmp) / "proxy.py"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _install(self):
        patch_shell_profile(self.profile, self.proxy_py)

    def test_removes_marker_block(self):
        self._install()
        unpatch_shell_profile(self.profile)
        self.assertNotIn(HOOK_MARKER, self.profile.read_text())

    def test_removes_anthropic_base_url(self):
        self._install()
        unpatch_shell_profile(self.profile)
        self.assertNotIn("ANTHROPIC_BASE_URL", self.profile.read_text())

    def test_preserves_content_before_block(self):
        self.profile.write_text("export EDITOR=vim\n")
        self._install()
        unpatch_shell_profile(self.profile)
        self.assertIn("EDITOR=vim", self.profile.read_text())

    def test_preserves_content_after_block(self):
        self._install()
        self.profile.write_text(self.profile.read_text() + "export FOO=bar\n")
        unpatch_shell_profile(self.profile)
        self.assertIn("FOO=bar", self.profile.read_text())

    def test_noop_when_marker_absent(self):
        self.profile.write_text("export EDITOR=vim\n")
        unpatch_shell_profile(self.profile)  # should not raise
        self.assertIn("EDITOR=vim", self.profile.read_text())

    def test_noop_when_profile_missing(self):
        unpatch_shell_profile(self.profile)  # should not raise

    def test_idempotent(self):
        self._install()
        unpatch_shell_profile(self.profile)
        unpatch_shell_profile(self.profile)  # second call should not raise or corrupt


# ── kill_proxy ─────────────────────────────────────────────────────────────

class TestKillProxy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_dir = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_pid(self, pid: int):
        (self.state_dir / "proxy.pid").write_text(str(pid))

    def test_sends_sigterm_to_pid_from_file(self):
        self._write_pid(99999)
        with patch("os.kill") as mock_kill:
            # SIGTERM succeeds, then kill(pid, 0) → ProcessLookupError (exited)
            mock_kill.side_effect = [None, ProcessLookupError]
            with patch("time.sleep"):
                kill_proxy(self.state_dir)
        self.assertEqual(mock_kill.call_args_list[0], call(99999, signal.SIGTERM))

    def test_removes_pid_file_after_kill(self):
        self._write_pid(99999)
        with patch("os.kill", side_effect=[None, ProcessLookupError]), \
             patch("time.sleep"):
            kill_proxy(self.state_dir)
        self.assertFalse((self.state_dir / "proxy.pid").exists())

    def test_noop_when_no_pid_file(self):
        kill_proxy(self.state_dir)  # should not raise

    def test_handles_dead_process_gracefully(self):
        self._write_pid(99999)
        with patch("os.kill", side_effect=ProcessLookupError):
            kill_proxy(self.state_dir)  # should not raise
        self.assertFalse((self.state_dir / "proxy.pid").exists())

    def test_handles_permission_error_gracefully(self):
        self._write_pid(99999)
        with patch("os.kill", side_effect=PermissionError):
            kill_proxy(self.state_dir)  # should not raise


# ── uninstall (full) ───────────────────────────────────────────────────────

class TestUninstallFull(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_dir = Path(self.tmp) / "claude-proxy"
        self.state_dir.mkdir()
        self.settings = Path(self.tmp) / "settings.json"
        self.profile = Path(self.tmp) / ".zshrc"
        self.proxy_py = Path(self.tmp) / "proxy.py"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _full_install(self):
        patch_settings_json(self.settings, self.proxy_py)
        patch_shell_profile(self.profile, self.proxy_py)

    def _uninstall(self):
        from setup import uninstall
        uninstall(
            state_dir=self.state_dir,
            settings_path=self.settings,
            shell_profile=self.profile,
        )

    def test_removes_state_dir(self):
        self._full_install()
        self._uninstall()
        self.assertFalse(self.state_dir.exists())

    def test_removes_settings_hook(self):
        self._full_install()
        self._uninstall()
        data = json.loads(self.settings.read_text())
        hooks = data.get("hooks", {}).get("SessionStart", [])
        self.assertEqual(len([h for h in hooks if HOOK_MARKER in json.dumps(h)]), 0)

    def test_removes_shell_profile_block(self):
        self._full_install()
        self._uninstall()
        self.assertNotIn(HOOK_MARKER, self.profile.read_text())

    def test_tolerates_missing_state_dir(self):
        self._full_install()
        shutil.rmtree(self.state_dir)
        self._uninstall()  # should not raise

    def test_tolerates_missing_settings(self):
        self._full_install()
        self.settings.unlink()
        self._uninstall()  # should not raise

    def test_tolerates_missing_profile(self):
        self._full_install()
        self.profile.unlink()
        self._uninstall()  # should not raise


# ── supervisor adapter delegation ────────────────────────────────────────


def test_install_delegates_to_supervisor_adapter(monkeypatch, tmp_path):
    """install() must call get_adapter().install(proxy_py) instead of the old LaunchAgent helper."""
    import setup

    calls = []

    class FakeAdapter:
        def install(self, proxy_path):
            calls.append(("install", proxy_path))

    # Stub the adapter
    monkeypatch.setattr(setup, "get_adapter", lambda: FakeAdapter())

    # Stub out every other side-effecty thing install() does
    monkeypatch.setattr(setup, "write_plugins_toml", lambda p: None)
    monkeypatch.setattr(setup, "patch_settings_json", lambda *a, **k: None)
    monkeypatch.setattr(setup, "patch_pretooluse_hook", lambda *a, **k: None)
    monkeypatch.setattr(setup, "patch_shell_profile", lambda *a, **k: None)
    monkeypatch.setattr(setup, "detect_shell_profile", lambda: tmp_path / "rc")

    # Set up a fake project_dir with a proxy.py
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "proxy.py").write_text("# fake")
    state_dir = tmp_path / "state"
    settings = tmp_path / "settings.json"

    setup.install(
        project_dir=project_dir,
        state_dir=state_dir,
        settings_path=settings,
        shell_profile=tmp_path / "rc",
    )

    assert calls, "adapter.install was never called"
    assert calls[0][0] == "install"
    assert calls[0][1].name == "proxy.py"


def test_uninstall_delegates_to_supervisor_adapter(monkeypatch, tmp_path):
    import setup

    calls = []

    class FakeAdapter:
        def uninstall(self):
            calls.append("uninstall")

    monkeypatch.setattr(setup, "get_adapter", lambda: FakeAdapter())
    monkeypatch.setattr(setup, "kill_proxy", lambda sd: None)
    monkeypatch.setattr(setup, "unpatch_pretooluse_hook", lambda *a, **k: None)
    monkeypatch.setattr(setup, "unpatch_settings_json", lambda *a, **k: None)
    monkeypatch.setattr(setup, "unpatch_shell_profile", lambda *a, **k: None)
    monkeypatch.setattr(setup, "detect_shell_profile", lambda: tmp_path / "rc")

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    setup.uninstall(
        state_dir=state_dir,
        settings_path=tmp_path / "settings.json",
        shell_profile=tmp_path / "rc",
    )

    assert calls == ["uninstall"]


# ── enable_plugin ─────────────────────────────────────────────────────────

class TestEnablePlugin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_dir = Path(self.tmp) / "claude-proxy"
        self.state_dir.mkdir()
        (self.state_dir / "plugins").mkdir()
        self.project_dir = Path(self.tmp) / "project"
        self.project_dir.mkdir()
        (self.project_dir / "plugins").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_copies_plugin_py_to_runtime(self):
        (self.project_dir / "plugins" / "telegram.py").write_text("# plugin")
        (self.project_dir / "plugins" / "telegram.toml").write_text("enabled = false")
        from setup import enable_plugin
        enable_plugin(self.state_dir, self.project_dir, "telegram")
        self.assertTrue((self.state_dir / "plugins" / "telegram.py").exists())

    def test_copies_plugin_toml_to_runtime(self):
        (self.project_dir / "plugins" / "telegram.py").write_text("# plugin")
        (self.project_dir / "plugins" / "telegram.toml").write_text("enabled = false")
        from setup import enable_plugin
        enable_plugin(self.state_dir, self.project_dir, "telegram")
        self.assertTrue((self.state_dir / "plugins" / "telegram.toml").exists())

    def test_sets_enabled_true_in_toml(self):
        (self.project_dir / "plugins" / "telegram.py").write_text("# plugin")
        (self.project_dir / "plugins" / "telegram.toml").write_text("enabled = false")
        from setup import enable_plugin
        enable_plugin(self.state_dir, self.project_dir, "telegram")
        content = (self.state_dir / "plugins" / "telegram.toml").read_text()
        self.assertIn("enabled = true", content)
        self.assertNotIn("enabled = false", content)

    def test_preserves_other_toml_config(self):
        (self.project_dir / "plugins" / "telegram.py").write_text("# plugin")
        toml = 'enabled = false\nbot_token = "tok123"\nchat_id = "42"'
        (self.project_dir / "plugins" / "telegram.toml").write_text(toml)
        from setup import enable_plugin
        enable_plugin(self.state_dir, self.project_dir, "telegram")
        content = (self.state_dir / "plugins" / "telegram.toml").read_text()
        self.assertIn('bot_token = "tok123"', content)
        self.assertIn('chat_id = "42"', content)

    def test_does_not_overwrite_existing_toml(self):
        """User's existing config should not be lost — only enabled flag changes."""
        (self.project_dir / "plugins" / "telegram.py").write_text("# plugin")
        (self.project_dir / "plugins" / "telegram.toml").write_text("enabled = false")
        (self.state_dir / "plugins" / "telegram.toml").write_text(
            'enabled = false\nbot_token = "my-secret"'
        )
        from setup import enable_plugin
        enable_plugin(self.state_dir, self.project_dir, "telegram")
        content = (self.state_dir / "plugins" / "telegram.toml").read_text()
        self.assertIn("enabled = true", content)
        self.assertIn('bot_token = "my-secret"', content)

    def test_raises_for_unknown_plugin(self):
        from setup import enable_plugin
        with self.assertRaises(FileNotFoundError):
            enable_plugin(self.state_dir, self.project_dir, "nonexistent")

    def test_adds_enabled_line_if_missing(self):
        (self.project_dir / "plugins" / "myplugin.py").write_text("# plugin")
        (self.project_dir / "plugins" / "myplugin.toml").write_text("# no enabled line\n")
        from setup import enable_plugin
        enable_plugin(self.state_dir, self.project_dir, "myplugin")
        content = (self.state_dir / "plugins" / "myplugin.toml").read_text()
        self.assertIn("enabled = true", content)

    def test_plugin_without_toml_gets_one_created(self):
        (self.project_dir / "plugins" / "bare.py").write_text("# plugin")
        from setup import enable_plugin
        enable_plugin(self.state_dir, self.project_dir, "bare")
        toml_path = self.state_dir / "plugins" / "bare.toml"
        self.assertTrue(toml_path.exists())
        self.assertIn("enabled = true", toml_path.read_text())


# ── disable_plugin ────────────────────────────────────────────────────────

class TestDisablePlugin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_dir = Path(self.tmp) / "claude-proxy"
        self.state_dir.mkdir()
        (self.state_dir / "plugins").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sets_enabled_false_in_toml(self):
        (self.state_dir / "plugins" / "telegram.toml").write_text("enabled = true")
        from setup import disable_plugin
        disable_plugin(self.state_dir, "telegram")
        content = (self.state_dir / "plugins" / "telegram.toml").read_text()
        self.assertIn("enabled = false", content)
        self.assertNotIn("enabled = true", content)

    def test_preserves_other_config(self):
        toml = 'enabled = true\nbot_token = "tok"\nchat_id = "42"'
        (self.state_dir / "plugins" / "telegram.toml").write_text(toml)
        from setup import disable_plugin
        disable_plugin(self.state_dir, "telegram")
        content = (self.state_dir / "plugins" / "telegram.toml").read_text()
        self.assertIn('bot_token = "tok"', content)

    def test_noop_when_already_disabled(self):
        toml = "enabled = false\n"
        (self.state_dir / "plugins" / "telegram.toml").write_text(toml)
        from setup import disable_plugin
        disable_plugin(self.state_dir, "telegram")
        content = (self.state_dir / "plugins" / "telegram.toml").read_text()
        self.assertIn("enabled = false", content)

    def test_raises_for_unknown_plugin(self):
        from setup import disable_plugin
        with self.assertRaises(FileNotFoundError):
            disable_plugin(self.state_dir, "nonexistent")

    def test_does_not_delete_plugin_files(self):
        (self.state_dir / "plugins" / "telegram.py").write_text("# plugin")
        (self.state_dir / "plugins" / "telegram.toml").write_text("enabled = true")
        from setup import disable_plugin
        disable_plugin(self.state_dir, "telegram")
        self.assertTrue((self.state_dir / "plugins" / "telegram.py").exists())


# ── list_plugins ──────────────────────────────────────────────────────────

class TestListPlugins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_dir = Path(self.tmp) / "claude-proxy"
        self.state_dir.mkdir()
        (self.state_dir / "plugins").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_empty_list_when_no_plugins(self):
        from setup import list_plugins
        result = list_plugins(self.state_dir)
        self.assertEqual(result, [])

    def test_returns_plugin_with_enabled_status(self):
        (self.state_dir / "plugins" / "telegram.py").write_text("# plugin")
        (self.state_dir / "plugins" / "telegram.toml").write_text("enabled = true")
        from setup import list_plugins
        result = list_plugins(self.state_dir)
        self.assertEqual(result, [("telegram", True)])

    def test_returns_disabled_plugin(self):
        (self.state_dir / "plugins" / "telegram.py").write_text("# plugin")
        (self.state_dir / "plugins" / "telegram.toml").write_text("enabled = false")
        from setup import list_plugins
        result = list_plugins(self.state_dir)
        self.assertEqual(result, [("telegram", False)])

    def test_plugin_without_toml_is_disabled(self):
        (self.state_dir / "plugins" / "bare.py").write_text("# plugin")
        from setup import list_plugins
        result = list_plugins(self.state_dir)
        self.assertEqual(result, [("bare", False)])

    def test_returns_multiple_plugins_sorted(self):
        (self.state_dir / "plugins" / "beta.py").write_text("# b")
        (self.state_dir / "plugins" / "beta.toml").write_text("enabled = true")
        (self.state_dir / "plugins" / "alpha.py").write_text("# a")
        (self.state_dir / "plugins" / "alpha.toml").write_text("enabled = false")
        from setup import list_plugins
        result = list_plugins(self.state_dir)
        self.assertEqual(result, [("alpha", False), ("beta", True)])

    def test_ignores_init_py(self):
        (self.state_dir / "plugins" / "__init__.py").write_text("")
        from setup import list_plugins
        result = list_plugins(self.state_dir)
        self.assertEqual(result, [])


# ── proxy_status ──────────────────────────────────────────────────────────

class TestProxyStatus(unittest.TestCase):
    def test_returns_none_when_proxy_not_running(self):
        from setup import proxy_status
        # Use a port that's definitely not running
        result = proxy_status(port=19999)
        self.assertIsNone(result)

    def test_returns_dict_on_healthy_proxy(self):
        from setup import proxy_status
        with patch("setup.urllib.request.urlopen") as mock_open:
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"status": "ok", "plugins": []}'
            mock_response.__enter__ = lambda s: s
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_response
            result = proxy_status(port=18019)
        self.assertEqual(result, {"status": "ok", "plugins": []})

    def test_returns_none_on_connection_error(self):
        from setup import proxy_status
        with patch("setup.urllib.request.urlopen", side_effect=Exception("refused")):
            result = proxy_status(port=18019)
        self.assertIsNone(result)


# ======================================================================
#  CLI tests
# ======================================================================

class TestBuildParser(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_recognizes_install(self):
        args = self.parser.parse_args(["install"])
        self.assertEqual(args.command, "install")

    def test_recognizes_uninstall(self):
        args = self.parser.parse_args(["uninstall"])
        self.assertEqual(args.command, "uninstall")

    def test_recognizes_add_plugin(self):
        args = self.parser.parse_args(["add-plugin", "telegram"])
        self.assertEqual(args.command, "add-plugin")
        self.assertEqual(args.name, "telegram")

    def test_recognizes_remove_plugin(self):
        args = self.parser.parse_args(["remove-plugin", "telegram"])
        self.assertEqual(args.command, "remove-plugin")
        self.assertEqual(args.name, "telegram")

    def test_recognizes_list_plugins(self):
        args = self.parser.parse_args(["list-plugins"])
        self.assertEqual(args.command, "list-plugins")

    def test_recognizes_status(self):
        args = self.parser.parse_args(["status"])
        self.assertEqual(args.command, "status")

    def test_add_plugin_requires_name(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["add-plugin"])

    def test_remove_plugin_requires_name(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["remove-plugin"])

    def test_no_command_sets_none(self):
        args = self.parser.parse_args([])
        self.assertIsNone(args.command)


# ── Command dispatch tests ────────────────────────────────────────────────

class TestCommandDispatch(unittest.TestCase):
    def test_all_commands_mapped(self):
        expected = {"install", "uninstall", "add-plugin", "remove-plugin",
                    "list-plugins", "restart", "status"}
        self.assertEqual(set(_COMMANDS.keys()), expected)

    def test_no_command_prints_help_and_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            main([])
        self.assertEqual(ctx.exception.code, 1)


# ── Integration: add-plugin / remove-plugin ───────────────────────────────

class TestAddPluginCmd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_dir = Path(self.tmp) / "claude-proxy"
        self.state_dir.mkdir()
        (self.state_dir / "plugins").mkdir()
        self.project_dir = Path(self.tmp) / "project"
        self.project_dir.mkdir()
        (self.project_dir / "plugins").mkdir()
        (self.project_dir / "plugins" / "telegram.py").write_text("# plugin")
        (self.project_dir / "plugins" / "telegram.toml").write_text("enabled = false")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch("setup._default_state_dir")
    @patch("setup._default_project_dir")
    def test_add_plugin_enables_it(self, mock_proj, mock_state):
        mock_state.return_value = self.state_dir
        mock_proj.return_value = self.project_dir
        main(["add-plugin", "telegram"])
        content = (self.state_dir / "plugins" / "telegram.toml").read_text()
        self.assertIn("enabled = true", content)

    @patch("setup._default_state_dir")
    @patch("setup._default_project_dir")
    def test_add_unknown_plugin_exits_1(self, mock_proj, mock_state):
        mock_state.return_value = self.state_dir
        mock_proj.return_value = self.project_dir
        with self.assertRaises(SystemExit) as ctx:
            main(["add-plugin", "nonexistent"])
        self.assertEqual(ctx.exception.code, 1)


class TestRemovePluginCmd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_dir = Path(self.tmp) / "claude-proxy"
        self.state_dir.mkdir()
        (self.state_dir / "plugins").mkdir()
        (self.state_dir / "plugins" / "telegram.toml").write_text("enabled = true")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch("setup._default_state_dir")
    def test_remove_plugin_disables_it(self, mock_state):
        mock_state.return_value = self.state_dir
        main(["remove-plugin", "telegram"])
        content = (self.state_dir / "plugins" / "telegram.toml").read_text()
        self.assertIn("enabled = false", content)

    @patch("setup._default_state_dir")
    def test_remove_unknown_plugin_exits_1(self, mock_state):
        mock_state.return_value = self.state_dir
        with self.assertRaises(SystemExit) as ctx:
            main(["remove-plugin", "nonexistent"])
        self.assertEqual(ctx.exception.code, 1)


# ── Integration: list-plugins ─────────────────────────────────────────────

class TestListPluginsCmd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_dir = Path(self.tmp) / "claude-proxy"
        self.state_dir.mkdir()
        (self.state_dir / "plugins").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch("setup._default_state_dir")
    def test_prints_no_plugins_message(self, mock_state):
        mock_state.return_value = self.state_dir
        captured = StringIO()
        with patch("sys.stdout", captured):
            main(["list-plugins"])
        self.assertIn("No plugins", captured.getvalue())

    @patch("setup._default_state_dir")
    def test_prints_enabled_plugin(self, mock_state):
        mock_state.return_value = self.state_dir
        (self.state_dir / "plugins" / "telegram.py").write_text("# p")
        (self.state_dir / "plugins" / "telegram.toml").write_text("enabled = true")
        captured = StringIO()
        with patch("sys.stdout", captured):
            main(["list-plugins"])
        output = captured.getvalue()
        self.assertIn("telegram", output)
        self.assertIn("enabled", output)


# ── Integration: status ───────────────────────────────────────────────────

class TestStatusCmd(unittest.TestCase):
    @patch("setup.proxy_status")
    def test_status_running(self, mock_status):
        mock_status.return_value = {"status": "ok", "plugins": ["telegram"], "sideload_pending": 0}
        captured = StringIO()
        with patch("sys.stdout", captured):
            main(["status"])
        output = captured.getvalue()
        self.assertIn("running", output)
        self.assertIn("telegram", output)

    @patch("setup.proxy_status")
    def test_status_not_running_exits_1(self, mock_status):
        mock_status.return_value = None
        with self.assertRaises(SystemExit) as ctx:
            main(["status"])
        self.assertEqual(ctx.exception.code, 1)


# ── Integration: install / uninstall ──────────────────────────────────────

class TestInstallCmd(unittest.TestCase):
    @patch("setup.install")
    def test_install_calls_install_function(self, mock_install):
        main(["install"])
        mock_install.assert_called_once()

    @patch("setup.uninstall")
    def test_uninstall_calls_uninstall_function(self, mock_uninstall):
        main(["uninstall"])
        mock_uninstall.assert_called_once()


def test_cmd_restart_uses_adapter(monkeypatch):
    import setup
    calls = []

    class FakeAdapter:
        def is_installed(self): return True
        def restart(self): calls.append("restart")

    monkeypatch.setattr(setup, "get_adapter", lambda: FakeAdapter())
    # Prevent the hot-reload branch from returning early
    monkeypatch.setattr(setup, "proxy_status", lambda: None)
    import argparse
    setup.cmd_restart(argparse.Namespace(force=False))
    assert "restart" in calls


def test_cmd_status_includes_monitor_metrics(monkeypatch, capsys):
    import setup
    monkeypatch.setattr(setup, "proxy_status", lambda: {
        "status": "ok", "plugins": [], "uptime_s": 300,
        "rss_mb": 42, "threads": 8, "fds": 14,
        "plugin_reloads": 1, "warnings": [],
    })
    class FakeAdapter:
        def is_installed(self): return True
        def status(self): return {"loaded": True, "running": True, "pid": 123, "last_exit": 0}
    monkeypatch.setattr(setup, "get_adapter", lambda: FakeAdapter())

    import argparse
    setup.cmd_status(argparse.Namespace())
    out = capsys.readouterr().out
    assert "rss" in out.lower()
    assert "uptime" in out.lower() or "300" in out


if __name__ == "__main__":
    unittest.main()

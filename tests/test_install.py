"""Tests for the claude-proxy global installer."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
from install import (
    create_runtime_dirs,
    install_plugins,
    write_plugins_toml,
    patch_settings_json,
    unpatch_settings_json,
    patch_shell_profile,
    detect_shell_profile,
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

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_profile_if_missing(self):
        patch_shell_profile(self.profile)
        self.assertTrue(self.profile.exists())

    def test_appends_anthropic_base_url_export(self):
        patch_shell_profile(self.profile)
        self.assertIn("ANTHROPIC_BASE_URL", self.profile.read_text())

    def test_appends_correct_url(self):
        patch_shell_profile(self.profile)
        self.assertIn("http://127.0.0.1:18019", self.profile.read_text())

    def test_idempotent_does_not_duplicate(self):
        patch_shell_profile(self.profile)
        patch_shell_profile(self.profile)
        count = self.profile.read_text().count("ANTHROPIC_BASE_URL")
        self.assertEqual(count, 1)

    def test_preserves_existing_content(self):
        self.profile.write_text("export EDITOR=vim\n")
        patch_shell_profile(self.profile)
        self.assertIn("EDITOR=vim", self.profile.read_text())

    def test_has_marker_to_identify_block(self):
        patch_shell_profile(self.profile)
        self.assertIn(HOOK_MARKER, self.profile.read_text())


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


if __name__ == "__main__":
    unittest.main()

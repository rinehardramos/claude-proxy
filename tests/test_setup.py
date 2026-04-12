"""Tests for the claude-proxy CLI (setup.py)."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))
from setup import build_parser, main, _COMMANDS


# ── Parser tests ──────────────────────────────────────────────────────────

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
                    "list-plugins", "status"}
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
    @patch("install.proxy_status")
    def test_status_running(self, mock_status):
        mock_status.return_value = {"status": "ok", "plugins": ["telegram"], "sideload_pending": 0}
        captured = StringIO()
        with patch("sys.stdout", captured):
            main(["status"])
        output = captured.getvalue()
        self.assertIn("running", output)
        self.assertIn("telegram", output)

    @patch("install.proxy_status")
    def test_status_not_running_exits_1(self, mock_status):
        mock_status.return_value = None
        with self.assertRaises(SystemExit) as ctx:
            main(["status"])
        self.assertEqual(ctx.exception.code, 1)


# ── Integration: install / uninstall ──────────────────────────────────────

class TestInstallCmd(unittest.TestCase):
    @patch("install.install")
    def test_install_calls_install_function(self, mock_install):
        main(["install"])
        mock_install.assert_called_once()

    @patch("install.uninstall")
    def test_uninstall_calls_uninstall_function(self, mock_uninstall):
        main(["uninstall"])
        mock_uninstall.assert_called_once()


if __name__ == "__main__":
    unittest.main()

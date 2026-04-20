"""Tests for hooks/telegram_approve.py — the PreToolUse hook script."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
import telegram_approve as hook


class TestParseToml(unittest.TestCase):
    def test_parses_key_value_pairs(self):
        text = 'bot_token = "abc"\nchat_id = "123"'
        result = hook._parse_toml(text)
        self.assertEqual(result["bot_token"], "abc")
        self.assertEqual(result["chat_id"], "123")

    def test_ignores_comments_and_sections(self):
        text = '# comment\n[section]\nkey = "val"'
        result = hook._parse_toml(text)
        self.assertEqual(result["key"], "val")

    def test_unquoted_values(self):
        text = "enabled = true"
        result = hook._parse_toml(text)
        self.assertEqual(result["enabled"], "true")


class TestFormatToolSummary(unittest.TestCase):
    def test_bash_command(self):
        s = hook._format_tool_summary("Bash", {"command": "ls -la"})
        self.assertEqual(s, "ls -la")

    def test_write_file(self):
        s = hook._format_tool_summary("Write", {"file_path": "/tmp/x.py", "content": "abc"})
        self.assertIn("/tmp/x.py", s)
        self.assertIn("3 chars", s)

    def test_edit_file(self):
        s = hook._format_tool_summary("Edit", {
            "file_path": "/tmp/x.py",
            "old_string": "old",
            "new_string": "new",
        })
        self.assertIn("/tmp/x.py", s)
        self.assertIn("-old", s)
        self.assertIn("+new", s)

    def test_read_file(self):
        s = hook._format_tool_summary("Read", {"file_path": "/tmp/x.py"})
        self.assertEqual(s, "/tmp/x.py")

    def test_unknown_tool_shows_json(self):
        s = hook._format_tool_summary("Glob", {"pattern": "*.py"})
        self.assertIn("*.py", s)


class TestRunScanner(unittest.TestCase):
    def test_none_scanner_returns_none(self):
        self.assertIsNone(hook._run_scanner({"command": "ls"}, "none"))

    def test_always_scanner_returns_findings(self):
        result = hook._run_scanner({"command": "ls"}, "always")
        self.assertIsNotNone(result)
        self.assertIn("Manual", result)


class TestOutputDecision(unittest.TestCase):
    def test_allow_output(self):
        with self.assertRaises(SystemExit):
            # Capture stdout
            from io import StringIO
            old_stdout = sys.stdout
            sys.stdout = StringIO()
            try:
                hook._output_decision("allow")
            except SystemExit:
                output = sys.stdout.getvalue()
                sys.stdout = old_stdout
                data = json.loads(output)
                self.assertEqual(
                    data["hookSpecificOutput"]["permissionDecision"], "allow"
                )
                raise

    def test_deny_with_reason(self):
        from io import StringIO
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            hook._output_decision("deny", "PII found")
        except SystemExit:
            output = sys.stdout.getvalue()
            sys.stdout = old_stdout
            data = json.loads(output)
            self.assertEqual(
                data["hookSpecificOutput"]["permissionDecision"], "deny"
            )
            self.assertIn("PII", data["hookSpecificOutput"]["permissionDecisionReason"])


class TestDecisionPolling(unittest.TestCase):
    """Test the file-based IPC for pending/decided decisions."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_hook_dir = hook.HOOK_DIR
        self.orig_pending = hook.PENDING_DIR
        self.orig_decided = hook.DECIDED_DIR
        hook.HOOK_DIR = Path(self.tmpdir)
        hook.PENDING_DIR = Path(self.tmpdir) / "pending"
        hook.DECIDED_DIR = Path(self.tmpdir) / "decided"
        hook.PENDING_DIR.mkdir()
        hook.DECIDED_DIR.mkdir()

    def tearDown(self):
        hook.HOOK_DIR = self.orig_hook_dir
        hook.PENDING_DIR = self.orig_pending
        hook.DECIDED_DIR = self.orig_decided
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pending_file_created_and_decided_file_read(self):
        """Simulate the hook writing pending and finding a decided file."""
        decision_id = "test123"
        pending = hook.PENDING_DIR / f"{decision_id}.json"
        decided = hook.DECIDED_DIR / f"{decision_id}.json"

        # Write pending (simulates what the hook does)
        pending.write_text(json.dumps({
            "message_id": 999,
            "project_cwd": "/tmp/proj",
            "tool_name": "Bash",
            "created_at": time.time(),
        }))
        self.assertTrue(pending.exists())

        # Write decided (simulates what the poller does)
        decided.write_text(json.dumps({
            "decision": "allow",
            "decided_at": time.time(),
        }))
        self.assertTrue(decided.exists())

        # Read decision (simulates what the hook does)
        result = json.loads(decided.read_text())
        self.assertEqual(result["decision"], "allow")


class TestSetupHookRegistration(unittest.TestCase):
    """Test setup.py hook registration functions."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).parent.parent))

    def test_patch_pretooluse_hook_adds_entry(self):
        import setup as setup_mod
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Path(tmpdir) / "settings.json"
            proxy_py = Path(tmpdir) / "proxy.py"
            proxy_py.write_text("# stub")

            settings.write_text("{}")
            setup_mod.patch_pretooluse_hook(settings, proxy_py)

            data = json.loads(settings.read_text())
            self.assertIn("PreToolUse", data["hooks"])
            entries = data["hooks"]["PreToolUse"]
            self.assertEqual(len(entries), 1)
            self.assertIn("--hook pre-tool", entries[0]["hooks"][0]["command"])
            self.assertIn("claude-proxy-hook", entries[0]["hooks"][0]["command"])

    def test_patch_pretooluse_hook_idempotent(self):
        import setup as setup_mod
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Path(tmpdir) / "settings.json"
            proxy_py = Path(tmpdir) / "proxy.py"
            proxy_py.write_text("# stub")

            settings.write_text("{}")
            setup_mod.patch_pretooluse_hook(settings, proxy_py)
            setup_mod.patch_pretooluse_hook(settings, proxy_py)

            data = json.loads(settings.read_text())
            self.assertEqual(len(data["hooks"]["PreToolUse"]), 1)

    def test_unpatch_pretooluse_hook_removes_entry(self):
        import setup as setup_mod
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Path(tmpdir) / "settings.json"
            proxy_py = Path(tmpdir) / "proxy.py"
            proxy_py.write_text("# stub")

            settings.write_text("{}")
            setup_mod.patch_pretooluse_hook(settings, proxy_py)
            setup_mod.unpatch_pretooluse_hook(settings)

            data = json.loads(settings.read_text())
            self.assertNotIn("PreToolUse", data.get("hooks", {}))

    def test_install_hooks_copies_scripts(self):
        import setup as setup_mod
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "src"
            dst = Path(tmpdir) / "dst"
            (src / "hooks").mkdir(parents=True)
            dst.mkdir()
            (src / "hooks" / "test_hook.py").write_text("# test")

            setup_mod.install_hooks(src, dst)

            installed = dst / "hooks" / "test_hook.py"
            self.assertTrue(installed.exists())
            self.assertEqual(installed.read_text(), "# test")


if __name__ == "__main__":
    unittest.main()

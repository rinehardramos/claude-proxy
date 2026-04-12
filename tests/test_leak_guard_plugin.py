"""Tests for the leak-guard claude-proxy plugin."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

_PLUGIN_PATH = Path(__file__).parent.parent / "plugins" / "leak_guard.py"


def _fresh_mod():
    """Load a fresh copy of the plugin module (avoids shared global state)."""
    spec = importlib.util.spec_from_file_location("leak_guard_fresh", _PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_fake_scanner(directory: Path, findings: list[dict]) -> Path:
    """Write a scanner.py stub that returns pre-canned findings for known inputs."""
    code = textwrap.dedent(f"""\
        from dataclasses import dataclass, field

        @dataclass
        class Finding:
            rule_id: str
            category: str
            description: str
            line: int
            preview: str
            severity: str = "medium"
            source: str = ""
            raw_match: str = field(default="", repr=False)

        _CANNED = {findings!r}

        def scan_all(text=None, path=None, source_label=""):
            if not text:
                return []
            return [
                Finding(**f) for f in _CANNED
                if f.get("raw_match") and f["raw_match"] in text
            ]
    """)
    p = directory / "scanner.py"
    p.write_text(code)
    return p


# Shared canned finding used across most tests
_CANNED_FINDING = {
    "rule_id": "test-secret",
    "category": "secret",
    "description": "Test secret",
    "line": 1,
    "preview": "[REDACTED]",
    "severity": "high",
    "source": "",
    "raw_match": "FAKESECRETVALUE001",
}

_SECRET = "FAKESECRETVALUE001"


class TestPluginInfo(unittest.TestCase):
    def test_returns_required_keys(self):
        mod = _fresh_mod()
        info = mod.plugin_info()
        self.assertIn("name", info)
        self.assertIn("version", info)
        self.assertIn("description", info)
        self.assertEqual(info["name"], "leak-guard")


class TestConfigure(unittest.TestCase):
    def test_explicit_scanner_path_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner = _write_fake_scanner(Path(tmp), [])
            mod = _fresh_mod()
            mod.configure({"scanner_path": str(scanner)})
            # Plugin is active — on_outbound must not raise
            result = mod.on_outbound({"messages": []})
            self.assertIsNotNone(result)

    def test_missing_scanner_path_disables_gracefully(self):
        mod = _fresh_mod()
        mod.configure({"scanner_path": "/nonexistent/scanner.py"})
        payload = {"system": "hello"}
        # Returns payload unchanged, no exception
        self.assertEqual(mod.on_outbound(payload), payload)

    def test_no_scanner_available_disables_gracefully(self):
        mod = _fresh_mod()
        with patch.object(mod, "_discover_scanner", return_value=None):
            mod.configure({})
        self.assertEqual(mod.on_outbound({"system": "test"}), {"system": "test"})


class TestOnOutbound(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        scanner = _write_fake_scanner(Path(self.tmp.name), [_CANNED_FINDING])
        self.mod = _fresh_mod()
        self.mod.configure({"scanner_path": str(scanner)})

    def tearDown(self):
        self.tmp.cleanup()

    def test_redacts_secret_in_system_string(self):
        payload = {"system": f"Use {_SECRET} for auth"}
        result = self.mod.on_outbound(payload)
        self.assertNotIn(_SECRET, result["system"])
        self.assertIn("[REDACTED:test-secret:", result["system"])

    def test_redacts_secret_in_system_block_list(self):
        payload = {"system": [{"type": "text", "text": f"key={_SECRET}"}]}
        result = self.mod.on_outbound(payload)
        self.assertNotIn(_SECRET, result["system"][0]["text"])

    def test_redacts_secret_in_user_message_string(self):
        payload = {"messages": [{"role": "user", "content": f"my key is {_SECRET}"}]}
        result = self.mod.on_outbound(payload)
        self.assertNotIn(_SECRET, result["messages"][0]["content"])

    def test_redacts_secret_in_user_message_block_list(self):
        payload = {
            "messages": [{
                "role": "user",
                "content": [{"type": "text", "text": f"key {_SECRET} here"}],
            }]
        }
        result = self.mod.on_outbound(payload)
        self.assertNotIn(_SECRET, result["messages"][0]["content"][0]["text"])

    def test_assistant_messages_not_scanned(self):
        # Assistant content is inbound — must not be modified
        payload = {"messages": [{"role": "assistant", "content": _SECRET}]}
        result = self.mod.on_outbound(payload)
        self.assertEqual(result["messages"][0]["content"], _SECRET)

    def test_clean_payload_returned_unchanged(self):
        payload = {"system": "no secrets here", "messages": []}
        result = self.mod.on_outbound(payload)
        self.assertEqual(result["system"], "no secrets here")

    def test_does_not_mutate_original_payload(self):
        original = f"Use {_SECRET}"
        payload = {"system": original}
        self.mod.on_outbound(payload)
        self.assertEqual(payload["system"], original)

    def test_finding_with_empty_raw_match_skipped(self):
        with tempfile.TemporaryDirectory() as tmp2:
            scanner = _write_fake_scanner(Path(tmp2), [
                {"rule_id": "x", "category": "pii", "description": "d",
                 "line": 1, "preview": "[REDACTED]", "severity": "low",
                 "source": "", "raw_match": ""},
            ])
            mod = _fresh_mod()
            mod.configure({"scanner_path": str(scanner)})
            payload = {"system": "hello world"}
            self.assertEqual(mod.on_outbound(payload)["system"], "hello world")

    def test_non_text_blocks_in_content_list_passed_through(self):
        payload = {
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"data": "abc123"}},
                    {"type": "text", "text": "describe this"},
                ],
            }]
        }
        result = self.mod.on_outbound(payload)
        # Image block untouched
        self.assertEqual(result["messages"][0]["content"][0]["source"]["data"], "abc123")


if __name__ == "__main__":
    unittest.main()

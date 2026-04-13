"""Live integration test for leak-guard proxy plugin.

Sends crafted payloads through the proxy and verifies redaction occurs
by inspecting what the upstream receives. Since we can't intercept the
upstream call directly, we test the plugin's _redact_text function
and on_outbound hook in isolation.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_PLUGIN_PATH = Path(__file__).parent.parent / "plugins" / "leak_guard.py"


def _load():
    spec = importlib.util.spec_from_file_location("leak_guard_plugin", _PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeFinding:
    """Mimics a scanner Finding object."""
    def __init__(self, rule_id: str, raw_match: str):
        self.rule_id = rule_id
        self.raw_match = raw_match


class _FakeScanner:
    """Mock scanner that detects specific patterns."""
    @staticmethod
    def scan_all(text: str = ""):
        findings = []
        # Simulate detecting a generic secret pattern
        if "SECRET_TOKEN_" in text:
            start = text.index("SECRET_TOKEN_")
            match = text[start:start + 30]
            findings.append(_FakeFinding("test-secret", match))
        if "password=" in text:
            start = text.index("password=")
            match = text[start:start + 20]
            findings.append(_FakeFinding("password-in-text", match))
        if "@example.com" in text:
            # Find email-like pattern
            words = text.split()
            for w in words:
                if "@example.com" in w:
                    findings.append(_FakeFinding("email-pii", w))
        return findings


class TestRedactText(unittest.TestCase):
    def setUp(self):
        self.t = _load()
        self.t._scanner = _FakeScanner()

    def test_redacts_secret_token(self):
        text = "Here is my SECRET_TOKEN_abcdef1234567890 in the message"
        result = self.t._redact_text(text)
        self.assertNotIn("SECRET_TOKEN_", result)
        self.assertIn("[REDACTED:test-secret:", result)

    def test_redacts_password(self):
        text = "my password=hunter2isnotgood"
        result = self.t._redact_text(text)
        self.assertNotIn("password=hunter2", result)
        self.assertIn("[REDACTED:password-in-text:", result)

    def test_redacts_email(self):
        text = "contact me at user@example.com please"
        result = self.t._redact_text(text)
        self.assertNotIn("user@example.com", result)
        self.assertIn("[REDACTED:email-pii:", result)

    def test_clean_text_unchanged(self):
        text = "This is a perfectly clean message with no secrets"
        result = self.t._redact_text(text)
        self.assertEqual(result, text)

    def test_redaction_token_format(self):
        text = "SECRET_TOKEN_abcdef1234567890xxxx"
        result = self.t._redact_text(text)
        # Format: [REDACTED:{rule_id}:{len}ch:hash={8hex}]
        self.assertRegex(result, r"\[REDACTED:test-secret:\d+ch:hash=[a-f0-9]{8}\]")

    def test_multiple_findings_all_redacted(self):
        text = "SECRET_TOKEN_abcdef1234567890 and password=mysecretpasswd and user@example.com"
        result = self.t._redact_text(text)
        self.assertNotIn("SECRET_TOKEN_", result)
        self.assertNotIn("password=", result)
        self.assertNotIn("@example.com", result)
        self.assertEqual(result.count("[REDACTED:"), 3)


class TestOnOutbound(unittest.TestCase):
    def setUp(self):
        self.t = _load()
        self.t._scanner = _FakeScanner()

    def test_redacts_user_message_string(self):
        payload = {
            "messages": [
                {"role": "user", "content": "my SECRET_TOKEN_abcdef1234567890 is here"}
            ]
        }
        result = self.t.on_outbound(payload)
        self.assertNotIn("SECRET_TOKEN_", result["messages"][0]["content"])

    def test_redacts_user_message_blocks(self):
        payload = {
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "my SECRET_TOKEN_abcdef1234567890 is here"}
                ]}
            ]
        }
        result = self.t.on_outbound(payload)
        self.assertNotIn("SECRET_TOKEN_", result["messages"][0]["content"][0]["text"])

    def test_preserves_assistant_messages(self):
        payload = {
            "messages": [
                {"role": "assistant", "content": "SECRET_TOKEN_abcdef1234567890 stays"}
            ]
        }
        result = self.t.on_outbound(payload)
        self.assertIn("SECRET_TOKEN_", result["messages"][0]["content"])

    def test_redacts_system_prompt(self):
        payload = {
            "system": "You have SECRET_TOKEN_abcdef1234567890 access",
            "messages": []
        }
        result = self.t.on_outbound(payload)
        self.assertNotIn("SECRET_TOKEN_", result["system"])

    def test_does_not_mutate_original(self):
        payload = {
            "messages": [
                {"role": "user", "content": "my SECRET_TOKEN_abcdef1234567890 here"}
            ]
        }
        original_content = payload["messages"][0]["content"]
        self.t.on_outbound(payload)
        self.assertEqual(payload["messages"][0]["content"], original_content)

    def test_passthrough_when_no_scanner(self):
        self.t._scanner = None
        payload = {"messages": [{"role": "user", "content": "SECRET_TOKEN_abcdef1234567890"}]}
        result = self.t.on_outbound(payload)
        self.assertIn("SECRET_TOKEN_", result["messages"][0]["content"])


class TestDiscoverScanner(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_finds_scanner_with_recursive_glob(self):
        """Verify the glob pattern matches the actual directory structure."""
        cache = Path("~/.claude/plugins/cache/leak-guard").expanduser()
        if not cache.exists():
            self.skipTest("leak-guard cache not present")
        candidates = list(cache.glob("**/hooks/scanner.py"))
        self.assertGreater(len(candidates), 0, "Should find scanner.py in cache")

    def test_discover_returns_newest(self):
        result = self.t._discover_scanner()
        if result is None:
            self.skipTest("No scanner available")
        self.assertTrue(result.endswith("scanner.py"))
        self.assertIn("leak-guard", result)


if __name__ == "__main__":
    unittest.main()

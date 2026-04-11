"""Tests for the Telegram notification plugin."""
from __future__ import annotations

import importlib.util
import json
import os
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

_PLUGIN_PATH = Path(__file__).parent.parent / "plugins" / "telegram.py"


def _load():
    """Load a fresh telegram module per test class — prevents state bleed between tests."""
    spec = importlib.util.spec_from_file_location("telegram_plugin", _PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPluginInfo(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_returns_name(self):
        self.assertEqual(self.t.plugin_info()["name"], "telegram")

    def test_returns_version_string(self):
        info = self.t.plugin_info()
        self.assertIsInstance(info["version"], str)

    def test_returns_description_string(self):
        info = self.t.plugin_info()
        self.assertIsInstance(info["description"], str)


class TestConfigure(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def _clean_env(self):
        env = os.environ.copy()
        env.pop("TELEGRAM_BOT_TOKEN", None)
        env.pop("TELEGRAM_CHAT_ID", None)
        return env

    def test_reads_default_env_var_names(self):
        env = self._clean_env()
        env["TELEGRAM_BOT_TOKEN"] = "tok123"
        env["TELEGRAM_CHAT_ID"] = "chat456"
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({})
        self.assertEqual(self.t._bot_token, "tok123")
        self.assertEqual(self.t._chat_id, "chat456")

    def test_reads_custom_env_var_names(self):
        env = self._clean_env()
        env["MY_TOKEN"] = "custom_tok"
        env["MY_CHAT"] = "custom_chat"
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({"bot_token_env": "MY_TOKEN", "chat_id_env": "MY_CHAT"})
        self.assertEqual(self.t._bot_token, "custom_tok")
        self.assertEqual(self.t._chat_id, "custom_chat")

    def test_missing_token_disables_plugin(self):
        env = self._clean_env()
        env["TELEGRAM_CHAT_ID"] = "chat456"
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({})
        self.assertIsNone(self.t._bot_token)
        self.assertIsNone(self.t._chat_id)

    def test_missing_chat_id_disables_plugin(self):
        env = self._clean_env()
        env["TELEGRAM_BOT_TOKEN"] = "tok123"
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({})
        self.assertIsNone(self.t._bot_token)
        self.assertIsNone(self.t._chat_id)

    def test_both_missing_becomes_noop(self):
        env = self._clean_env()
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({})
        self.assertIsNone(self.t._bot_token)
        self.assertIsNone(self.t._chat_id)

    def test_reads_direct_bot_token_from_config(self):
        env = self._clean_env()
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({"bot_token": "direct-tok", "chat_id": "direct-chat"})
        self.assertEqual(self.t._bot_token, "direct-tok")
        self.assertEqual(self.t._chat_id, "direct-chat")

    def test_direct_config_takes_priority_over_env(self):
        env = self._clean_env()
        env["TELEGRAM_BOT_TOKEN"] = "env-tok"
        env["TELEGRAM_CHAT_ID"] = "env-chat"
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({"bot_token": "config-tok", "chat_id": "config-chat"})
        self.assertEqual(self.t._bot_token, "config-tok")
        self.assertEqual(self.t._chat_id, "config-chat")

    def test_missing_chat_id_in_direct_config_disables_plugin(self):
        env = self._clean_env()
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({"bot_token": "direct-tok"})
        self.assertIsNone(self.t._bot_token)
        self.assertIsNone(self.t._chat_id)

    def test_missing_bot_token_in_direct_config_disables_plugin(self):
        env = self._clean_env()
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({"chat_id": "direct-chat"})
        self.assertIsNone(self.t._bot_token)
        self.assertIsNone(self.t._chat_id)


class TestOnInbound(unittest.TestCase):
    def setUp(self):
        self.t = _load()
        env = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "99999"}
        with patch.dict(os.environ, env, clear=False):
            self.t.configure({})

    def _call_and_capture(self, response_text, request_summary):
        """Call on_inbound and block until the daemon thread finishes its send."""
        done = threading.Event()
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(req)
            done.set()

        with patch.object(urllib.request, "urlopen", side_effect=mock_urlopen):
            result = self.t.on_inbound(response_text, request_summary)
            done.wait(timeout=2)

        return result, captured

    def test_returns_none(self):
        result, _ = self._call_and_capture("hello", {"user_text": "hi"})
        self.assertIsNone(result)

    def test_sends_to_telegram_api(self):
        _, captured = self._call_and_capture("response", {"user_text": "test"})
        self.assertEqual(len(captured), 1)
        url = captured[0].full_url
        self.assertIn("api.telegram.org", url)
        self.assertIn("sendMessage", url)
        self.assertIn("test-token", url)

    def test_message_contains_user_text(self):
        _, captured = self._call_and_capture("response", {"user_text": "What is 2+2?"})
        body = json.loads(captured[0].data.decode())
        self.assertIn("What is 2+2?", body["text"])

    def test_message_contains_response_char_count(self):
        _, captured = self._call_and_capture("A" * 500, {"user_text": "q"})
        body = json.loads(captured[0].data.decode())
        self.assertIn("500 chars", body["text"])

    def test_message_format_prefix(self):
        _, captured = self._call_and_capture("hello", {"user_text": "Say hi"})
        body = json.loads(captured[0].data.decode())
        self.assertTrue(body["text"].startswith('Claude responded to: "Say hi"'))

    def test_user_text_truncated_at_100_with_ellipsis(self):
        long_text = "x" * 200
        _, captured = self._call_and_capture("resp", {"user_text": long_text})
        body = json.loads(captured[0].data.decode())
        self.assertIn("x" * 100 + "...", body["text"])

    def test_short_user_text_not_truncated(self):
        _, captured = self._call_and_capture("resp", {"user_text": "short"})
        body = json.loads(captured[0].data.decode())
        self.assertNotIn("...", body["text"])

    def test_sends_to_correct_chat_id(self):
        _, captured = self._call_and_capture("resp", {"user_text": "test"})
        body = json.loads(captured[0].data.decode())
        self.assertEqual(body["chat_id"], "99999")

    def test_noop_when_not_configured(self):
        self.t._bot_token = None
        self.t._chat_id = None
        with patch.object(urllib.request, "urlopen") as mock_open:
            result = self.t.on_inbound("response", {"user_text": "test"})
        mock_open.assert_not_called()
        self.assertIsNone(result)

    def test_urlopen_error_handled_silently(self):
        done = threading.Event()

        def mock_urlopen(req, timeout=None):
            done.set()
            raise Exception("network error")

        with patch.object(urllib.request, "urlopen", side_effect=mock_urlopen):
            result = self.t.on_inbound("response", {"user_text": "test"})
            done.wait(timeout=2)

        self.assertIsNone(result)

    def test_missing_user_text_key_handled(self):
        _, captured = self._call_and_capture("response", {"model": "claude"})
        self.assertEqual(len(captured), 1)


if __name__ == "__main__":
    unittest.main()

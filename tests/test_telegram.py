"""Tests for the Telegram notification plugin."""
from __future__ import annotations

import importlib.util
import json
import os
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch, MagicMock

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
        env.pop("OPENAI_API_KEY", None)
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

    def test_reads_project_name_from_config(self):
        env = self._clean_env()
        env["TELEGRAM_BOT_TOKEN"] = "tok"
        env["TELEGRAM_CHAT_ID"] = "chat"
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({"project_name": "my-project"})
        self.assertEqual(self.t._project_name, "my-project")

    def test_project_name_empty_when_unset(self):
        env = self._clean_env()
        env["TELEGRAM_BOT_TOKEN"] = "tok"
        env["TELEGRAM_CHAT_ID"] = "chat"
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({})
        self.assertEqual(self.t._project_name, "")

    def test_reads_audio_threshold_from_config(self):
        env = self._clean_env()
        env["TELEGRAM_BOT_TOKEN"] = "tok"
        env["TELEGRAM_CHAT_ID"] = "chat"
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({"audio_threshold": 16384})
        self.assertEqual(self.t._audio_threshold, 16384)

    def test_audio_threshold_defaults_to_8192(self):
        env = self._clean_env()
        env["TELEGRAM_BOT_TOKEN"] = "tok"
        env["TELEGRAM_CHAT_ID"] = "chat"
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({})
        self.assertEqual(self.t._audio_threshold, 8192)

    def test_reads_tts_engine_from_config(self):
        env = self._clean_env()
        env["TELEGRAM_BOT_TOKEN"] = "tok"
        env["TELEGRAM_CHAT_ID"] = "chat"
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({"tts_engine": "openai"})
        self.assertEqual(self.t._tts_engine, "openai")

    def test_tts_engine_defaults_to_say(self):
        env = self._clean_env()
        env["TELEGRAM_BOT_TOKEN"] = "tok"
        env["TELEGRAM_CHAT_ID"] = "chat"
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({})
        self.assertEqual(self.t._tts_engine, "say")

    def test_reads_openai_tts_config(self):
        env = self._clean_env()
        env["TELEGRAM_BOT_TOKEN"] = "tok"
        env["TELEGRAM_CHAT_ID"] = "chat"
        env["OPENAI_API_KEY"] = "sk-test"
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({"tts_openai_model": "tts-1-hd", "tts_openai_voice": "nova"})
        self.assertEqual(self.t._tts_openai_model, "tts-1-hd")
        self.assertEqual(self.t._tts_openai_voice, "nova")
        self.assertEqual(self.t._tts_openai_api_key, "sk-test")

    def test_openai_api_key_from_custom_env(self):
        env = self._clean_env()
        env["TELEGRAM_BOT_TOKEN"] = "tok"
        env["TELEGRAM_CHAT_ID"] = "chat"
        env["MY_OAI_KEY"] = "sk-custom"
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({"tts_openai_api_key_env": "MY_OAI_KEY"})
        self.assertEqual(self.t._tts_openai_api_key, "sk-custom")


# ── Dynamic Timeout ──────────────────────────────────────────────────────

class TestEstimateTimeout(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_short_text_returns_minimum(self):
        result = self.t._estimate_timeout(500)
        self.assertEqual(result, 60)  # min is 60

    def test_scales_with_text_length(self):
        result = self.t._estimate_timeout(12000)  # 12K chars
        # base=30 + 15*12 = 210
        self.assertEqual(result, 210)

    def test_very_long_text(self):
        result = self.t._estimate_timeout(50000)  # 50K chars
        # base=30 + 15*50 = 780
        self.assertEqual(result, 780)

    def test_zero_length(self):
        result = self.t._estimate_timeout(0)
        self.assertEqual(result, 60)


# ── TTS Status Tracking ─────────────────────────────────────────────────

class TestTTSStatus(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_tts_status_returns_none_when_idle(self):
        self.t._clear_status()
        self.assertIsNone(self.t.tts_status())

    def test_update_status_sets_fields(self):
        self.t._tts_status = {"start_mono": __import__("time").monotonic()}
        self.t._update_status("abc123", "encoding", engine="say")
        status = self.t.tts_status()
        self.assertEqual(status["uid"], "abc123")
        self.assertEqual(status["stage"], "encoding")
        self.assertEqual(status["engine"], "say")
        self.assertIn("elapsed", status)

    def test_clear_status_resets(self):
        self.t._tts_status = {"uid": "x", "stage": "done"}
        self.t._clear_status()
        self.assertIsNone(self.t.tts_status())

    def test_tts_status_returns_copy(self):
        self.t._tts_status = {"uid": "x", "stage": "encoding", "start_mono": 0}
        status = self.t.tts_status()
        status["stage"] = "tampered"
        self.assertEqual(self.t._tts_status["stage"], "encoding")

    def test_status_tracks_through_tts_to_ogg(self):
        """_tts_to_ogg sets status stages as it runs."""
        self.t._TTS_REGISTRY.clear()
        self.t._register_tts("mock", lambda: None, lambda t, d, u: "/tmp/mock.ogg")
        ogg, _ = self.t._tts_to_ogg("hello", "mock")
        self.assertEqual(ogg, "/tmp/mock.ogg")
        # Status should have been set during the run
        status = self.t.tts_status()
        self.assertIsNotNone(status)
        self.assertIn("uid", status)

    def test_status_shows_failed_on_all_engines_fail(self):
        self.t._TTS_REGISTRY.clear()
        self.t._register_tts("bad", lambda: "broken", lambda t, d, u: None)
        ogg, diags = self.t._tts_to_ogg("hello", "bad")
        self.assertIsNone(ogg)
        # Status was set during the check phase
        status = self.t.tts_status()
        self.assertIsNotNone(status)


# ── TTS Registry ─────────────────────────────────────────────────────────

class TestTTSRegistry(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_builtin_engines_registered(self):
        names = [e["name"] for e in self.t._TTS_REGISTRY]
        self.assertIn("say", names)
        self.assertIn("openai", names)
        self.assertIn("pyttsx3", names)

    def test_register_custom_engine(self):
        self.t._register_tts("custom", lambda: None, lambda t, d, u: None)
        names = [e["name"] for e in self.t._TTS_REGISTRY]
        self.assertIn("custom", names)

    def test_preferred_engine_tried_first(self):
        order = self.t._get_engine_order("pyttsx3")
        self.assertEqual(order[0]["name"], "pyttsx3")

    def test_preferred_engine_unknown_puts_all_in_default_order(self):
        order = self.t._get_engine_order("nonexistent")
        names = [e["name"] for e in order]
        self.assertEqual(names, [e["name"] for e in self.t._TTS_REGISTRY])


class TestTTSChecks(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_check_say_missing_say_command(self):
        with patch("shutil.which", return_value=None):
            err = self.t._check_say()
        self.assertIn("say command not found", err)

    def test_check_say_missing_ffmpeg(self):
        def which(cmd):
            return "/usr/bin/say" if cmd == "say" else None
        with patch("shutil.which", side_effect=which):
            err = self.t._check_say()
        self.assertIn("ffmpeg", err)

    def test_check_say_all_present(self):
        with patch("shutil.which", return_value="/usr/bin/found"):
            err = self.t._check_say()
        self.assertIsNone(err)

    def test_check_openai_missing_key(self):
        self.t._tts_openai_api_key = None
        err = self.t._check_openai()
        self.assertIn("OPENAI_API_KEY", err)

    def test_check_openai_with_key(self):
        self.t._tts_openai_api_key = "sk-test"
        err = self.t._check_openai()
        self.assertIsNone(err)

    def test_check_pyttsx3_not_installed(self):
        with patch.dict("sys.modules", {"pyttsx3": None}), \
             patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            err = self.t._check_pyttsx3()
        self.assertIn("pyttsx3 not installed", err)

    def test_check_pyttsx3_missing_ffmpeg(self):
        mock_mod = MagicMock()
        with patch.dict("sys.modules", {"pyttsx3": mock_mod}), \
             patch("shutil.which", return_value=None):
            err = self.t._check_pyttsx3()
        self.assertIn("ffmpeg", err)


class TestTTSToOgg(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_returns_diagnostics_when_all_fail(self):
        # Make all checks fail
        for eng in self.t._TTS_REGISTRY:
            eng["check"] = lambda: "not available"
        ogg, diags = self.t._tts_to_ogg("hello", "say")
        self.assertIsNone(ogg)
        self.assertGreater(len(diags), 0)
        self.assertTrue(all("not available" in d for d in diags))

    def test_returns_path_on_first_success(self):
        # First engine fails check, second succeeds
        self.t._TTS_REGISTRY.clear()
        self.t._register_tts("fail", lambda: "broken", lambda t, d, u: None)
        self.t._register_tts("ok", lambda: None, lambda t, d, u: "/tmp/test.ogg")
        ogg, diags = self.t._tts_to_ogg("hello", "fail")
        self.assertEqual(ogg, "/tmp/test.ogg")
        self.assertEqual(len(diags), 1)
        self.assertIn("broken", diags[0])

    def test_skips_check_failure_tries_next(self):
        call_order = []
        self.t._TTS_REGISTRY.clear()
        self.t._register_tts("a", lambda: "nope", lambda t, d, u: None)

        def gen_b(t, d, u):
            call_order.append("b")
            return "/tmp/b.ogg"

        self.t._register_tts("b", lambda: None, gen_b)
        ogg, _ = self.t._tts_to_ogg("text", "a")
        self.assertEqual(ogg, "/tmp/b.ogg")
        self.assertIn("b", call_order)

    def test_generation_failure_adds_diagnostic(self):
        self.t._TTS_REGISTRY.clear()
        self.t._register_tts("flaky", lambda: None, lambda t, d, u: None)
        ogg, diags = self.t._tts_to_ogg("text", "flaky")
        self.assertIsNone(ogg)
        self.assertTrue(any("generation failed" in d for d in diags))


# ── Message splitting ─────────────────────────────────────────────────────

class TestSplitMessage(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_short_response_single_chunk(self):
        chunks = self.t._split_message("Hello world", "myproj", "What?")
        self.assertEqual(len(chunks), 1)
        self.assertIn("<b>myproj</b>", chunks[0])
        self.assertIn("<blockquote>What?</blockquote>", chunks[0])
        self.assertIn("Hello world", chunks[0])

    def test_first_chunk_has_full_prompt(self):
        long_prompt = "x" * 500
        chunks = self.t._split_message("short", "proj", long_prompt)
        self.assertIn(long_prompt, chunks[0])

    def test_prompt_not_truncated(self):
        long_prompt = "y" * 300
        chunks = self.t._split_message("body", "proj", long_prompt)
        self.assertIn(long_prompt, chunks[0])
        self.assertNotIn("...", chunks[0])

    def test_multi_chunk_has_numbered_headers(self):
        big = "A" * 10000
        chunks = self.t._split_message(big, "proj", "q")
        self.assertGreater(len(chunks), 1)
        self.assertIn("<blockquote>q</blockquote>", chunks[0])
        for i, chunk in enumerate(chunks[1:], start=2):
            self.assertIn(f"<b>proj [{i}/{len(chunks)}]</b>", chunk)

    def test_subsequent_chunks_no_prompt(self):
        big = "B" * 10000
        chunks = self.t._split_message(big, "proj", "question")
        for chunk in chunks[1:]:
            self.assertNotIn("<blockquote>question</blockquote>", chunk)

    def test_each_chunk_within_max_length(self):
        big = "C" * 15000
        chunks = self.t._split_message(big, "proj", "q")
        for chunk in chunks:
            self.assertLessEqual(len(chunk), self.t.MAX_TG_LENGTH)

    def test_all_content_preserved(self):
        response = "D" * 10000
        chunks = self.t._split_message(response, "proj", "q")
        # Extract response body from blockquote-wrapped chunks
        import re
        combined = ""
        for chunk in chunks:
            # Last blockquote in each chunk contains the response body
            matches = re.findall(r"<blockquote>(.*?)</blockquote>", chunk, re.DOTALL)
            if matches:
                combined += matches[-1]
        self.assertEqual(combined, response)

    def test_diagnostic_note_included_in_first_chunk(self):
        chunks = self.t._split_message("body", "proj", "q", "say: ffmpeg not found")
        self.assertIn("Audio unavailable: say: ffmpeg not found", chunks[0])
        self.assertIn("Sending as text.", chunks[0])

    def test_no_diagnostic_note_when_none(self):
        chunks = self.t._split_message("body", "proj", "q")
        self.assertNotIn("Audio unavailable", chunks[0])


# ── on_inbound integration ───────────────────────────────────────────────

class TestOnInbound(unittest.TestCase):
    def setUp(self):
        self.t = _load()
        env = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "99999"}
        with patch.dict(os.environ, env, clear=False):
            self.t.configure({"tts_engine": "none"})

    def _call_and_capture(self, response_text, request_summary):
        """Call on_inbound and block until the daemon thread finishes all sends."""
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(req)

        with patch.object(urllib.request, "urlopen", side_effect=mock_urlopen):
            result = self.t.on_inbound(response_text, request_summary)
            for t in threading.enumerate():
                if t.daemon and t is not threading.current_thread():
                    t.join(timeout=2)

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

    def test_message_contains_full_response(self):
        _, captured = self._call_and_capture("The answer is 4", {"user_text": "q"})
        body = json.loads(captured[0].data.decode())
        self.assertIn("The answer is 4", body["text"])

    def test_message_format_has_project_and_prompt(self):
        _, captured = self._call_and_capture("hello", {"user_text": "Say hi"})
        body = json.loads(captured[0].data.decode())
        self.assertIn("<b>", body["text"])
        self.assertIn("<blockquote>Say hi</blockquote>", body["text"])

    def test_on_inbound_uses_cwd_from_request(self):
        _, captured = self._call_and_capture(
            "hello",
            {"user_text": "hi", "cwd": "/some/where/project-1"},
        )
        body = json.loads(captured[0].data.decode())
        self.assertIn("<b>project-1</b>", body["text"])

    def test_on_inbound_shows_unknown_when_no_cwd_and_no_config(self):
        env = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "99999"}
        with patch.dict(os.environ, env, clear=False):
            self.t.configure({"tts_engine": "none"})
        _, captured = self._call_and_capture("hello", {"user_text": "hi"})
        body = json.loads(captured[0].data.decode())
        self.assertIn("(unknown project)", body["text"])

    def test_full_prompt_not_truncated(self):
        long_prompt = "z" * 300
        _, captured = self._call_and_capture("resp", {"user_text": long_prompt})
        body = json.loads(captured[0].data.decode())
        self.assertIn(long_prompt, body["text"])

    def test_long_response_split_into_multiple_messages(self):
        big_response = "A" * 5000
        _, captured = self._call_and_capture(big_response, {"user_text": "q"})
        self.assertGreater(len(captured), 1)
        full = "".join(json.loads(c.data.decode())["text"] for c in captured)
        self.assertEqual(full.count("A"), 5000)

    def test_short_response_sends_single_message(self):
        _, captured = self._call_and_capture("short reply", {"user_text": "q"})
        self.assertEqual(len(captured), 1)

    def test_each_chunk_within_max_length(self):
        big_response = "B" * 8000
        _, captured = self._call_and_capture(big_response, {"user_text": "q"})
        for req in captured:
            body = json.loads(req.data.decode())
            self.assertLessEqual(len(body["text"]), self.t.MAX_TG_LENGTH)

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


class TestTTSPathIntegration(unittest.TestCase):
    def setUp(self):
        self.t = _load()
        env = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "99999"}
        with patch.dict(os.environ, env, clear=False):
            self.t.configure({"tts_engine": "say", "audio_threshold": 100})

    def test_long_response_triggers_audio_path(self):
        fake_ogg = os.path.join(os.path.dirname(__file__), "_test_voice.ogg")
        with open(fake_ogg, "wb") as f:
            f.write(b"fake-ogg-data")

        voice_sent = []

        def mock_urlopen(req, timeout=None):
            voice_sent.append(req)

        try:
            with patch.object(self.t, "_tts_to_ogg", return_value=(fake_ogg, [])) as mock_tts, \
                 patch.object(urllib.request, "urlopen", side_effect=mock_urlopen):
                self.t.on_inbound("A" * 200, {"user_text": "long query"})
                for t in threading.enumerate():
                    if t.daemon and t is not threading.current_thread():
                        t.join(timeout=2)

            mock_tts.assert_called_once()
            self.assertEqual(len(voice_sent), 1)
            self.assertIn("sendVoice", voice_sent[0].full_url)
        finally:
            try:
                os.remove(fake_ogg)
            except OSError:
                pass

    def test_tts_failure_falls_back_to_text_with_diagnostic(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(req)

        with patch.object(self.t, "_tts_to_ogg", return_value=(None, ["say: ffmpeg not found"])), \
             patch.object(urllib.request, "urlopen", side_effect=mock_urlopen):
            self.t.on_inbound("A" * 200, {"user_text": "q"})
            for t in threading.enumerate():
                if t.daemon and t is not threading.current_thread():
                    t.join(timeout=2)

        self.assertGreater(len(captured), 0)
        self.assertIn("sendMessage", captured[0].full_url)
        body = json.loads(captured[0].data.decode())
        self.assertIn("Audio unavailable", body["text"])
        self.assertIn("ffmpeg not found", body["text"])

    def test_engine_none_skips_audio_no_diagnostic(self):
        self.t._tts_engine = "none"
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(req)

        with patch.object(urllib.request, "urlopen", side_effect=mock_urlopen):
            self.t.on_inbound("A" * 200, {"user_text": "q"})
            for t in threading.enumerate():
                if t.daemon and t is not threading.current_thread():
                    t.join(timeout=2)

        for req in captured:
            self.assertIn("sendMessage", req.full_url)
            body = json.loads(req.data.decode())
            self.assertNotIn("Audio unavailable", body["text"])


# ── TTS engine generate functions ────────────────────────────────────────

class TestGenerateSay(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_say_success_returns_ogg_path(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = self.t._generate_say("hello", "/tmp", "abc")
        self.assertIsNotNone(result)
        self.assertTrue(result.endswith(".ogg"))
        self.assertEqual(mock_run.call_count, 2)

    def test_say_failure_returns_none(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("say not found")):
            result = self.t._generate_say("hello", "/tmp", "abc")
        self.assertIsNone(result)

    def test_say_uses_dynamic_timeout(self):
        """say subprocess timeout scales with text length."""
        long_text = "x" * 20000  # 20K chars → base=30 + 15*20 = 330s
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            self.t._generate_say(long_text, "/tmp", "dyn")
        # First call is say, check its timeout
        say_call = mock_run.call_args_list[0]
        self.assertEqual(say_call.kwargs["timeout"], 330)


class TestGenerateOpenAI(unittest.TestCase):
    def setUp(self):
        self.t = _load()
        self.t._tts_openai_api_key = "sk-test"
        self.t._tts_openai_model = "tts-1"
        self.t._tts_openai_voice = "alloy"

    def test_openai_success_returns_ogg_path(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"fake-opus-audio"

        with patch.object(urllib.request, "urlopen", return_value=mock_resp):
            result = self.t._generate_openai("hello world", "/tmp", "oai1")

        self.assertIsNotNone(result)
        self.assertTrue(result.endswith(".ogg"))
        # Cleanup the file
        try:
            os.remove(result)
        except OSError:
            pass

    def test_openai_sends_correct_request(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"audio"
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(req)
            return mock_resp

        with patch.object(urllib.request, "urlopen", side_effect=mock_urlopen):
            result = self.t._generate_openai("test text", "/tmp", "oai2")

        self.assertEqual(len(captured), 1)
        req = captured[0]
        self.assertIn("api.openai.com", req.full_url)
        self.assertIn("audio/speech", req.full_url)
        body = json.loads(req.data.decode())
        self.assertEqual(body["model"], "tts-1")
        self.assertEqual(body["voice"], "alloy")
        self.assertEqual(body["input"], "test text")
        self.assertEqual(body["response_format"], "opus")
        self.assertIn("Bearer sk-test", req.get_header("Authorization"))
        # Cleanup
        try:
            os.remove(result)
        except OSError:
            pass

    def test_openai_failure_returns_none(self):
        with patch.object(urllib.request, "urlopen", side_effect=Exception("API error")):
            result = self.t._generate_openai("hello", "/tmp", "oai3")
        self.assertIsNone(result)


class TestGeneratePyttsx3(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_pyttsx3_import_failure_returns_none(self):
        with patch.dict("sys.modules", {"pyttsx3": None}):
            result = self.t._generate_pyttsx3("hello", "/tmp", "abc")
        self.assertIsNone(result)


# ── Telegram voice upload ────────────────────────────────────────────────

class TestSendVoice(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_sends_multipart_to_send_voice_endpoint(self):
        import tempfile
        ogg = os.path.join(tempfile.gettempdir(), "test_voice.ogg")
        with open(ogg, "wb") as f:
            f.write(b"fake-ogg")

        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(req)

        try:
            with patch.object(urllib.request, "urlopen", side_effect=mock_urlopen):
                self.t._send_voice("tok", "123", ogg, "caption here")
            self.assertEqual(len(captured), 1)
            self.assertIn("sendVoice", captured[0].full_url)
            self.assertIn(b"fake-ogg", captured[0].data)
            self.assertIn(b"caption here", captured[0].data)
            self.assertIn("multipart/form-data", captured[0].get_header("Content-type"))
        finally:
            os.remove(ogg)


# ── Helpers ──────────────────────────────────────────────────────────────

class TestCleanup(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_removes_existing_file(self):
        import tempfile
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.assertTrue(os.path.exists(path))
        self.t._cleanup(path)
        self.assertFalse(os.path.exists(path))

    def test_missing_file_no_error(self):
        self.t._cleanup("/nonexistent/path/file.ogg")


# ── Callback poller ──────────────────────────────────────────────────────

class TestCallbackPoller(unittest.TestCase):
    """Test the Telegram callback poller functions."""

    def setUp(self):
        self.t = _load()
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self._orig_hook_dir = self.t.HOOK_DIR
        self.t.HOOK_DIR = Path(self.tmpdir)
        (Path(self.tmpdir) / "pending").mkdir()
        (Path(self.tmpdir) / "decided").mkdir()

    def tearDown(self):
        self.t.HOOK_DIR = self._orig_hook_dir
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_handle_callback_approve(self):
        """Approve callback writes decided file and removes pending."""
        decision_id = "abc123"
        pending = Path(self.tmpdir) / "pending" / f"{decision_id}.json"
        decided = Path(self.tmpdir) / "decided" / f"{decision_id}.json"
        pending.write_text(json.dumps({"message_id": 1, "created_at": 0}))

        cb = {
            "id": "qid",
            "data": f"approve:{decision_id}",
            "message": {"message_id": 1},
        }

        with patch.object(urllib.request, "urlopen"):
            self.t._handle_callback(cb, "tok", "chat")

        self.assertTrue(decided.exists())
        result = json.loads(decided.read_text())
        self.assertEqual(result["decision"], "allow")
        self.assertFalse(pending.exists())

    def test_handle_callback_deny(self):
        """Deny callback writes 'deny' decision."""
        decision_id = "def456"
        pending = Path(self.tmpdir) / "pending" / f"{decision_id}.json"
        decided = Path(self.tmpdir) / "decided" / f"{decision_id}.json"
        pending.write_text(json.dumps({"message_id": 2, "created_at": 0}))

        cb = {
            "id": "qid2",
            "data": f"deny:{decision_id}",
            "message": {"message_id": 2},
        }

        with patch.object(urllib.request, "urlopen"):
            self.t._handle_callback(cb, "tok", "chat")

        self.assertTrue(decided.exists())
        result = json.loads(decided.read_text())
        self.assertEqual(result["decision"], "deny")

    def test_handle_callback_expired_pending(self):
        """Callback for expired/missing pending is handled gracefully."""
        cb = {
            "id": "qid3",
            "data": "approve:nonexistent",
            "message": {"message_id": 3},
        }

        with patch.object(urllib.request, "urlopen"):
            self.t._handle_callback(cb, "tok", "chat")

        decided = Path(self.tmpdir) / "decided" / "nonexistent.json"
        self.assertFalse(decided.exists())

    def test_handle_callback_invalid_format(self):
        """Callback with no colon is ignored."""
        cb = {"id": "qid4", "data": "invalid", "message": {"message_id": 4}}
        self.t._handle_callback(cb, "tok", "chat")

    def test_cleanup_stale_files(self):
        """Stale files older than max_age are removed."""
        import time as _time
        old = Path(self.tmpdir) / "pending" / "old.json"
        old.write_text(json.dumps({"message_id": 99}))
        os.utime(old, (_time.time() - 700, _time.time() - 700))

        fresh = Path(self.tmpdir) / "pending" / "fresh.json"
        fresh.write_text(json.dumps({"message_id": 100}))

        with patch.object(urllib.request, "urlopen"):
            self.t._cleanup_stale_hook_files("tok", "chat", max_age=600)

        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_cleanup_expires_telegram_message(self):
        """Stale pending files trigger Telegram message edit to expired."""
        import time as _time
        stale = Path(self.tmpdir) / "pending" / "stale.json"
        stale.write_text(json.dumps({"message_id": 42}))
        os.utime(stale, (_time.time() - 700, _time.time() - 700))

        calls = []
        def mock_urlopen(req, timeout=None):
            calls.append(req)

        with patch.object(urllib.request, "urlopen", side_effect=mock_urlopen):
            self.t._cleanup_stale_hook_files("tok", "chat", max_age=600)

        self.assertFalse(stale.exists())
        # Should have called editMessageText to mark as expired
        self.assertTrue(len(calls) > 0)
        body = json.loads(calls[0].data.decode())
        self.assertIn("Expired", body.get("reply_markup", ""))

    def test_handle_callback_noop_ignored(self):
        """Noop callback (re-tap on decided button) is handled gracefully."""
        cb = {"id": "qid5", "data": "noop:decided", "message": {"message_id": 5}}
        with patch.object(urllib.request, "urlopen"):
            self.t._handle_callback(cb, "tok", "chat")

    def test_poller_not_started_without_config_flag(self):
        """Poller should not start unless approval_poller is set."""
        env = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "chat"}
        with patch.dict(os.environ, env, clear=False):
            self.t.configure({})
        # _poller_thread should be None (not started)
        self.assertTrue(
            self.t._poller_thread is None or not self.t._poller_thread.is_alive()
        )


class TestExtractOptions(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_extracts_numbered_options_at_end(self):
        text = "Choose one:\n1. Yes\n2. No\n3. Maybe"
        result = self.t._extract_options(text)
        self.assertEqual(result, ["Yes", "No", "Maybe"])

    def test_returns_none_for_single_option(self):
        text = "1. Only one"
        self.assertIsNone(self.t._extract_options(text))

    def test_returns_none_for_no_options(self):
        text = "Just some regular text here."
        self.assertIsNone(self.t._extract_options(text))

    def test_extracts_indented_options(self):
        text = "Pick:\n  1. Alpha\n  2. Beta"
        result = self.t._extract_options(text)
        self.assertEqual(result, ["Alpha", "Beta"])

    def test_ignores_long_numbered_list(self):
        """Numbered lists with 5+ items are not interactive options."""
        text = (
            "Summary:\n"
            "1. First change was big\n"
            "2. Second change was medium\n"
            "3. Third change was small\n"
            "4. Fourth change was tiny\n"
            "5. Fifth change was trivial"
        )
        self.assertIsNone(self.t._extract_options(text))

    def test_ignores_numbered_list_not_at_end(self):
        """Numbered items buried in middle of text are not options."""
        text = (
            "1. Do this\n"
            "2. Do that\n"
            "\n"
            "And then we continued with a long explanation about "
            "how all of this works in detail with many more lines "
            "of text that follow after the numbered items.\n"
            "More text here.\n"
            "Even more text.\n"
            "Final paragraph."
        )
        self.assertIsNone(self.t._extract_options(text))

    def test_ignores_long_option_labels(self):
        """Options with very long labels (>80 chars) are not interactive."""
        text = (
            "1. " + "x" * 85 + "\n"
            "2. " + "y" * 85
        )
        self.assertIsNone(self.t._extract_options(text))


class TestOnOutbound(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_injects_pending_reply(self):
        self.t._pending_replies.append("hello from telegram")
        payload = {
            "messages": [
                {"role": "user", "content": "test prompt"},
            ],
        }
        result = self.t.on_outbound(payload)
        self.assertIsNotNone(result)
        self.assertIn("hello from telegram", result["messages"][0]["content"])
        self.assertIn("system-reminder", result["messages"][0]["content"])
        # Queue should be empty after injection
        self.assertEqual(len(self.t._pending_replies), 0)

    def test_injects_into_block_list_content(self):
        self.t._pending_replies.append("reply text")
        payload = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            ],
        }
        result = self.t.on_outbound(payload)
        self.assertIsNotNone(result)
        last_block = result["messages"][0]["content"][-1]
        self.assertEqual(last_block["type"], "text")
        self.assertIn("reply text", last_block["text"])

    def test_noop_when_no_replies(self):
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        result = self.t.on_outbound(payload)
        self.assertIsNone(result)

    def test_does_not_mutate_original(self):
        self.t._pending_replies.append("test")
        original_content = "original"
        payload = {"messages": [{"role": "user", "content": original_content}]}
        self.t.on_outbound(payload)
        self.assertEqual(payload["messages"][0]["content"], original_content)

    def test_injects_multiple_replies(self):
        self.t._pending_replies.extend(["first", "second"])
        payload = {"messages": [{"role": "user", "content": "prompt"}]}
        result = self.t.on_outbound(payload)
        self.assertIn("first", result["messages"][0]["content"])
        self.assertIn("second", result["messages"][0]["content"])


class TestMute(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_mute_suppresses_on_inbound(self):
        env = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "chat"}
        with patch.dict(os.environ, env, clear=False):
            self.t.configure({})
        self.t._muted = True
        result = self.t.on_inbound("response text", {"user_text": "hi"})
        self.assertIsNone(result)

    def test_unmuted_sends_notification(self):
        env = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "chat"}
        with patch.dict(os.environ, env, clear=False):
            self.t.configure({})
        self.t._muted = False
        with patch("urllib.request.urlopen"):
            # Should not return None (fires notification thread)
            result = self.t.on_inbound("response text", {"user_text": "hi"})
            # on_inbound always returns None (fire-and-forget), but it should start a thread
            self.assertIsNone(result)


class TestHandleTextMessage(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_mute_toggle(self):
        self.assertFalse(self.t._muted)
        with patch("urllib.request.urlopen"):
            self.t._handle_text_message({"text": "/mute"}, "tok", "chat")
        self.assertTrue(self.t._muted)
        with patch("urllib.request.urlopen"):
            self.t._handle_text_message({"text": "/mute"}, "tok", "chat")
        self.assertFalse(self.t._muted)

    def test_mute_on_off(self):
        with patch("urllib.request.urlopen"):
            self.t._handle_text_message({"text": "/mute_on"}, "tok", "chat")
        self.assertTrue(self.t._muted)
        with patch("urllib.request.urlopen"):
            self.t._handle_text_message({"text": "/mute_off"}, "tok", "chat")
        self.assertFalse(self.t._muted)

    def test_reply_queued_when_waiting(self):
        self.t._waiting_for_reply = True
        with patch("urllib.request.urlopen"):
            self.t._handle_text_message({"text": "my reply"}, "tok", "chat")
        self.assertFalse(self.t._waiting_for_reply)
        self.assertEqual(self.t._pending_replies, ["my reply"])

    def test_reply_to_bot_message_queued(self):
        msg = {
            "text": "comment text",
            "reply_to_message": {"from": {"is_bot": True}},
        }
        with patch("urllib.request.urlopen"):
            self.t._handle_text_message(msg, "tok", "chat")
        self.assertEqual(self.t._pending_replies, ["comment text"])

    def test_mode_set_auto_approve(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.t.HOOK_DIR = Path(tmp)
            self.t._MODE_FILE = Path(tmp) / "mode"
            with patch("urllib.request.urlopen"):
                self.t._handle_text_message({"text": "/mode auto-approve"}, "tok", "chat")
            self.assertEqual(self.t._approval_mode, "auto-approve")
            self.assertEqual((Path(tmp) / "mode").read_text(), "auto-approve")

    def test_mode_set_auto_deny(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.t.HOOK_DIR = Path(tmp)
            self.t._MODE_FILE = Path(tmp) / "mode"
            with patch("urllib.request.urlopen"):
                self.t._handle_text_message({"text": "/mode auto-deny"}, "tok", "chat")
            self.assertEqual(self.t._approval_mode, "auto-deny")

    def test_mode_set_ask(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.t.HOOK_DIR = Path(tmp)
            self.t._MODE_FILE = Path(tmp) / "mode"
            self.t._approval_mode = "auto-approve"
            with patch("urllib.request.urlopen"):
                self.t._handle_text_message({"text": "/mode ask"}, "tok", "chat")
            self.assertEqual(self.t._approval_mode, "ask")

    def test_mode_invalid_shows_usage(self):
        with patch("urllib.request.urlopen") as mock_url:
            self.t._handle_text_message({"text": "/mode invalid"}, "tok", "chat")
            # Should have been called (sends usage message)
            self.assertTrue(mock_url.called)
            # Mode unchanged
            self.assertEqual(self.t._approval_mode, "ask")

    def test_mode_no_arg_shows_current(self):
        with patch("urllib.request.urlopen") as mock_url:
            self.t._handle_text_message({"text": "/mode"}, "tok", "chat")
            self.assertTrue(mock_url.called)

    def test_mode_default_is_ask(self):
        self.assertEqual(self.t._approval_mode, "ask")


if __name__ == "__main__":
    unittest.main()

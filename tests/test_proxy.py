"""Tests for claude-proxy: plugin loader, sideload, SSE injection, health endpoint."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))
import proxy as _proxy_mod
from proxy import (
    _parse_plugins_toml,
    _extract_cwd,
    load_plugin_config,
    is_plugin_enabled,
    validate_plugin,
    PluginManager,
    load_plugins,
    load_sideload,
    inject_outbound,
    build_sse_content_block,
    process_sse_stream,
    ProxyHandler,
)


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_sse_lines(*events) -> list[bytes]:
    """Build a list of byte lines from (event_type, data_dict) pairs."""
    lines = []
    for event_type, data in events:
        lines.append(f"event: {event_type}\n".encode())
        lines.append(f"data: {json.dumps(data)}\n".encode())
        lines.append(b"\n")
    return lines


def _run_sse(events, plugins=None, inbound_sideload=None, user_text="test"):
    """Run process_sse_stream and return all written bytes as a decoded string."""
    written = []
    process_sse_stream(
        resp_lines=_make_sse_lines(*events),
        write_fn=written.append,
        plugins=plugins or [],
        request_summary={"user_text": user_text, "model": "claude", "path": "/v1/messages"},
        inbound_sideload=inbound_sideload or [],
    )
    return b"".join(written).decode()


def _data_jsons(output: str) -> list[dict]:
    """Parse all data: lines from SSE output into dicts."""
    return [
        json.loads(line[5:].strip())
        for line in output.split("\n")
        if line.startswith("data:") and line[5:].strip()
    ]


# ── _parse_plugins_toml ────────────────────────────────────────────────────

class TestParsePluginsToml(unittest.TestCase):
    def test_parses_enabled_list(self):
        text = 'enabled = ["telegram", "slack"]'
        result = _parse_plugins_toml(text)
        self.assertEqual(result["enabled"], ["telegram", "slack"])

    def test_parses_section_string_values(self):
        text = textwrap.dedent("""\
            enabled = ["telegram"]
            [telegram]
            bot_token_env = "TELEGRAM_BOT_TOKEN"
            chat_id_env = "TELEGRAM_CHAT_ID"
        """)
        result = _parse_plugins_toml(text)
        self.assertEqual(result["telegram"]["bot_token_env"], "TELEGRAM_BOT_TOKEN")
        self.assertEqual(result["telegram"]["chat_id_env"], "TELEGRAM_CHAT_ID")

    def test_ignores_comment_lines(self):
        text = textwrap.dedent("""\
            # top comment
            enabled = ["telegram"]
        """)
        result = _parse_plugins_toml(text)
        self.assertEqual(result["enabled"], ["telegram"])

    def test_empty_enabled_list(self):
        result = _parse_plugins_toml('enabled = []')
        self.assertEqual(result["enabled"], [])

    def test_empty_string_returns_empty_dict(self):
        self.assertEqual(_parse_plugins_toml(""), {})


# ── load_plugins ───────────────────────────────────────────────────────────

class TestLoadPlugins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugins_dir = Path(self.tmp.name) / "plugins"
        self.plugins_dir.mkdir()
        self.config_file = Path(self.tmp.name) / "plugins.toml"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_plugin(self, name: str, code: str):
        (self.plugins_dir / f"{name}.py").write_text(textwrap.dedent(code))

    def _write_config(self, enabled: list, sections: dict | None = None):
        lines = [f'enabled = [{", ".join(repr(e) for e in enabled)}]']
        if sections:
            for section, values in sections.items():
                lines.append(f"[{section}]")
                for k, v in values.items():
                    lines.append(f'{k} = "{v}"')
        self.config_file.write_text("\n".join(lines))

    def test_loads_enabled_plugin(self):
        self._write_plugin("myplugin", """\
            def plugin_info():
                return {"name": "myplugin", "version": "1.0", "description": "test"}
        """)
        self._write_config(["myplugin"])
        plugins = load_plugins(self.plugins_dir, self.config_file)
        self.assertEqual(len(plugins), 1)
        self.assertEqual(plugins[0].plugin_info()["name"], "myplugin")

    def test_skips_plugin_not_in_enabled_list(self):
        self._write_plugin("disabled", """\
            def plugin_info():
                return {"name": "disabled", "version": "1.0", "description": "d"}
        """)
        self._write_config([])
        plugins = load_plugins(self.plugins_dir, self.config_file)
        self.assertEqual(len(plugins), 0)

    def test_calls_configure_with_config_section(self):
        self._write_plugin("cfg", """\
            received = {}
            def plugin_info():
                return {"name": "cfg", "version": "1.0", "description": "c"}
            def configure(config):
                received.update(config)
        """)
        self._write_config(["cfg"], {"cfg": {"key": "value"}})
        plugins = load_plugins(self.plugins_dir, self.config_file)
        self.assertEqual(plugins[0].received, {"key": "value"})

    def test_calls_configure_with_empty_dict_when_no_section(self):
        self._write_plugin("cfg2", """\
            received = None
            def plugin_info():
                return {"name": "cfg2", "version": "1.0", "description": "c"}
            def configure(config):
                global received
                received = config
        """)
        self._write_config(["cfg2"])
        plugins = load_plugins(self.plugins_dir, self.config_file)
        self.assertEqual(plugins[0].received, {})

    def test_handles_import_error_gracefully(self):
        self._write_plugin("broken", "this is not valid python !!!")
        self._write_config(["broken"])
        plugins = load_plugins(self.plugins_dir, self.config_file)
        self.assertEqual(len(plugins), 0)

    def test_no_config_file_returns_empty(self):
        plugins = load_plugins(self.plugins_dir, Path(self.tmp.name) / "none.toml")
        self.assertEqual(plugins, [])

    def test_no_plugins_dir_returns_empty(self):
        plugins = load_plugins(Path(self.tmp.name) / "none", self.config_file)
        self.assertEqual(plugins, [])

    def test_loads_multiple_plugins(self):
        for name in ["alpha", "beta"]:
            self._write_plugin(name, f"""\
                def plugin_info():
                    return {{"name": "{name}", "version": "1", "description": ""}}
            """)
        self._write_config(["alpha", "beta"])
        plugins = load_plugins(self.plugins_dir, self.config_file)
        names = [p.plugin_info()["name"] for p in plugins]
        self.assertIn("alpha", names)
        self.assertIn("beta", names)

    def test_plugin_without_configure_loads_fine(self):
        self._write_plugin("nocfg", """\
            def plugin_info():
                return {"name": "nocfg", "version": "1.0", "description": ""}
        """)
        self._write_config(["nocfg"])
        plugins = load_plugins(self.plugins_dir, self.config_file)
        self.assertEqual(len(plugins), 1)


# ── load_sideload ──────────────────────────────────────────────────────────

class TestLoadSideload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, data: dict, age: float = 0):
        p = self.d / name
        p.write_text(json.dumps(data))
        if age > 0:
            t = time.time() - age
            os.utime(p, (t, t))

    def test_loads_json_file(self):
        self._write("001.json", {"target": "system", "content": "hello"})
        items = load_sideload(self.d)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["content"], "hello")

    def test_consumes_file_after_loading(self):
        self._write("001.json", {"content": "hello"})
        load_sideload(self.d)
        self.assertFalse((self.d / "001.json").exists())

    def test_skips_stale_files(self):
        self._write("001.json", {"content": "old"}, age=400)
        items = load_sideload(self.d, ttl=300)
        self.assertEqual(len(items), 0)

    def test_deletes_stale_files(self):
        self._write("001.json", {"content": "old"}, age=400)
        load_sideload(self.d, ttl=300)
        self.assertFalse((self.d / "001.json").exists())

    def test_returns_files_sorted_by_name(self):
        self._write("002_b.json", {"content": "second"})
        self._write("001_a.json", {"content": "first"})
        items = load_sideload(self.d)
        self.assertEqual(items[0]["content"], "first")
        self.assertEqual(items[1]["content"], "second")

    def test_returns_empty_for_missing_dir(self):
        self.assertEqual(load_sideload(self.d / "none"), [])

    def test_fresh_file_is_not_stale(self):
        self._write("001.json", {"content": "fresh"})
        items = load_sideload(self.d, ttl=300)
        self.assertEqual(len(items), 1)


# ── inject_outbound ────────────────────────────────────────────────────────

class TestInjectOutbound(unittest.TestCase):
    def test_no_items_returns_payload_unchanged(self):
        p = {"system": "hello"}
        self.assertEqual(inject_outbound(p, []), p)

    def test_target_system_appends_to_string(self):
        result = inject_outbound(
            {"system": "original"},
            [{"target": "system", "content": "injected"}],
        )
        self.assertEqual(result["system"], "original\n\ninjected")

    def test_target_system_creates_when_missing(self):
        result = inject_outbound({}, [{"target": "system", "content": "injected"}])
        self.assertEqual(result["system"], "injected")

    def test_target_system_appends_to_list(self):
        result = inject_outbound(
            {"system": [{"type": "text", "text": "original"}]},
            [{"target": "system", "content": "injected"}],
        )
        self.assertEqual(result["system"][-1], {"type": "text", "text": "injected"})

    def test_target_user_message_appends_block_to_last_user(self):
        result = inject_outbound(
            {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]},
            [{"target": "user_message", "content": "extra"}],
        )
        self.assertEqual(result["messages"][-1]["content"][-1], {"type": "text", "text": "extra"})

    def test_target_user_message_converts_string_content_to_blocks(self):
        result = inject_outbound(
            {"messages": [{"role": "user", "content": "hello"}]},
            [{"target": "user_message", "content": "extra"}],
        )
        content = result["messages"][-1]["content"]
        self.assertEqual(len(content), 2)
        self.assertEqual(content[1]["text"], "extra")

    def test_target_user_turn_appends_new_message(self):
        result = inject_outbound(
            {"messages": [{"role": "user", "content": "hello"}]},
            [{"target": "user_turn", "content": "context"}],
        )
        self.assertEqual(len(result["messages"]), 2)
        last = result["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertEqual(last["content"], "context")

    def test_does_not_mutate_original_payload(self):
        p = {"system": "original"}
        inject_outbound(p, [{"target": "system", "content": "injected"}])
        self.assertEqual(p["system"], "original")

    def test_default_target_is_system(self):
        result = inject_outbound({}, [{"content": "injected"}])
        self.assertEqual(result["system"], "injected")


# ── build_sse_content_block ────────────────────────────────────────────────

class TestBuildSSEContentBlock(unittest.TestCase):
    def setUp(self):
        self.raw = build_sse_content_block(3, "hello world")
        self.text = self.raw.decode()
        self.data_jsons = _data_jsons(self.text)

    def test_returns_bytes(self):
        self.assertIsInstance(self.raw, bytes)

    def test_contains_all_three_event_types(self):
        self.assertIn("event: content_block_start", self.text)
        self.assertIn("event: content_block_delta", self.text)
        self.assertIn("event: content_block_stop", self.text)

    def test_all_events_use_correct_index(self):
        for d in self.data_jsons:
            self.assertEqual(d["index"], 3)

    def test_delta_contains_injected_text(self):
        deltas = [d for d in self.data_jsons if d.get("type") == "content_block_delta"]
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["delta"]["text"], "hello world")

    def test_start_event_has_text_type_content_block(self):
        starts = [d for d in self.data_jsons if d.get("type") == "content_block_start"]
        self.assertEqual(starts[0]["content_block"]["type"], "text")


# ── process_sse_stream ─────────────────────────────────────────────────────

class TestProcessSSEStream(unittest.TestCase):
    def test_forwards_non_message_stop_events(self):
        output = _run_sse([
            ("message_start", {"type": "message_start", "message": {}}),
            ("message_stop", {"type": "message_stop"}),
        ])
        self.assertIn("message_start", output)
        self.assertIn("message_stop", output)

    def test_injects_sideload_before_message_stop(self):
        output = _run_sse([
            ("message_stop", {"type": "message_stop"}),
        ], inbound_sideload=["injected text"])
        injection_pos = output.find("injected text")
        stop_pos = output.rfind("message_stop")  # last occurrence = the real stop event
        self.assertGreater(injection_pos, 0)
        self.assertGreater(stop_pos, injection_pos)

    def test_injected_block_index_follows_last_content_block(self):
        output = _run_sse([
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_stop", {"type": "message_stop"}),
        ], inbound_sideload=["inject"])
        starts = [d for d in _data_jsons(output) if d.get("type") == "content_block_start"]
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[1]["index"], 1)

    def test_calls_on_inbound_with_assembled_response_text(self):
        calls = []

        class Plugin:
            def on_inbound(self, response_text, request_summary):
                calls.append(response_text)
                return None

        _run_sse([
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "part1 "}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "part2"}}),
            ("message_stop", {"type": "message_stop"}),
        ], plugins=[Plugin()])
        self.assertEqual(calls, ["part1 part2"])

    def test_injects_plugin_on_inbound_return_value(self):
        class Plugin:
            def on_inbound(self, response_text, request_summary):
                return "plugin content"

        output = _run_sse([
            ("message_stop", {"type": "message_stop"}),
        ], plugins=[Plugin()])
        self.assertIn("plugin content", output)

    def test_plugin_on_inbound_exception_does_not_crash(self):
        class BrokenPlugin:
            def on_inbound(self, response_text, request_summary):
                raise RuntimeError("boom")

        # Should not raise; message_stop should still be forwarded
        output = _run_sse([
            ("message_stop", {"type": "message_stop"}),
        ], plugins=[BrokenPlugin()])
        self.assertIn("message_stop", output)

    def test_plugin_without_on_inbound_is_ignored(self):
        class NoHookPlugin:
            pass

        output = _run_sse([
            ("message_stop", {"type": "message_stop"}),
        ], plugins=[NoHookPlugin()])
        self.assertIn("message_stop", output)

    def test_on_inbound_returning_none_injects_nothing(self):
        class Plugin:
            def on_inbound(self, response_text, request_summary):
                return None

        output = _run_sse([
            ("message_stop", {"type": "message_stop"}),
        ], plugins=[Plugin()])
        # Only message_stop data should be present — no extra content_block_start
        starts = [d for d in _data_jsons(output) if d.get("type") == "content_block_start"]
        self.assertEqual(len(starts), 0)

    def test_message_stop_always_forwarded(self):
        output = _run_sse([("message_stop", {"type": "message_stop"})])
        self.assertIn("message_stop", output)

    def test_multiple_inbound_items_get_sequential_indices(self):
        output = _run_sse([
            ("message_stop", {"type": "message_stop"}),
        ], inbound_sideload=["first", "second"])
        starts = [d for d in _data_jsons(output) if d.get("type") == "content_block_start"]
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[0]["index"], 0)
        self.assertEqual(starts[1]["index"], 1)

    def test_passes_request_summary_to_on_inbound(self):
        summaries = []

        class Plugin:
            def on_inbound(self, response_text, request_summary):
                summaries.append(request_summary)
                return None

        _run_sse([("message_stop", {"type": "message_stop"})],
                 plugins=[Plugin()], user_text="my question")
        self.assertEqual(summaries[0]["user_text"], "my question")


# ── Health endpoint ────────────────────────────────────────────────────────

class TestHealthEndpoint(unittest.TestCase):
    def _make_handler(self, plugins=None):
        """Build a minimal ProxyHandler with mocked HTTP plumbing."""
        handler = ProxyHandler.__new__(ProxyHandler)
        handler.plugins = plugins or []
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        return handler

    def _body(self, handler):
        handler.wfile.seek(0)
        return json.loads(handler.wfile.read())

    def test_status_is_ok(self):
        h = self._make_handler()
        h._health()
        self.assertEqual(self._body(h)["status"], "ok")

    def test_lists_plugin_names(self):
        class P:
            def plugin_info(self):
                return {"name": "myplugin"}

        h = self._make_handler(plugins=[P()])
        h._health()
        self.assertIn("myplugin", self._body(h)["plugins"])

    def test_empty_plugins_list(self):
        h = self._make_handler()
        h._health()
        self.assertEqual(self._body(h)["plugins"], [])

    def test_sideload_pending_key_present(self):
        h = self._make_handler()
        h._health()
        self.assertIn("sideload_pending", self._body(h))

    def test_sends_200_response(self):
        h = self._make_handler()
        h._health()
        h.send_response.assert_called_once_with(200)


# ── load_plugin_config ─────────────────────────────────────────────────────

class TestLoadPluginConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_returns_config_when_toml_exists(self):
        (self.d / "myplugin.toml").write_text('enabled = true\nkey = "val"')
        cfg = load_plugin_config(self.d, "myplugin")
        self.assertEqual(cfg["key"], "val")

    def test_returns_empty_when_toml_missing(self):
        self.assertEqual(load_plugin_config(self.d, "nope"), {})

    def test_parses_enabled_true(self):
        (self.d / "p.toml").write_text("enabled = true")
        cfg = load_plugin_config(self.d, "p")
        self.assertIn("enabled", cfg)

    def test_handles_malformed_toml(self):
        (self.d / "bad.toml").write_text("this is not valid @@@ toml")
        cfg = load_plugin_config(self.d, "bad")
        self.assertIsInstance(cfg, dict)

    def test_returns_empty_when_dir_missing(self):
        self.assertEqual(load_plugin_config(self.d / "nope", "x"), {})


# ── is_plugin_enabled ──────────────────────────────────────────────────────

class TestIsPluginEnabled(unittest.TestCase):
    def test_enabled_true_in_local_config(self):
        self.assertTrue(is_plugin_enabled("p", {"enabled": "true"}, {}))

    def test_enabled_false_in_local_config(self):
        self.assertFalse(is_plugin_enabled("p", {"enabled": "false"}, {}))

    def test_no_local_config_uses_global_enabled(self):
        self.assertTrue(is_plugin_enabled("p", {}, {"enabled": ["p"]}))

    def test_no_local_config_not_in_global(self):
        self.assertFalse(is_plugin_enabled("p", {}, {"enabled": ["other"]}))

    def test_local_config_overrides_global(self):
        self.assertFalse(is_plugin_enabled("p", {"enabled": "false"}, {"enabled": ["p"]}))

    def test_empty_everything_is_disabled(self):
        self.assertFalse(is_plugin_enabled("p", {}, {}))


# ── validate_plugin ────────────────────────────────────────────────────────

class TestValidatePlugin(unittest.TestCase):
    def _make_module(self, **attrs):
        mod = type(sys)("fake_plugin")
        for k, v in attrs.items():
            setattr(mod, k, v)
        return mod

    def test_valid_with_plugin_info(self):
        mod = self._make_module(plugin_info=lambda: {"name": "t", "version": "1"})
        self.assertTrue(validate_plugin(mod))

    def test_invalid_without_plugin_info(self):
        mod = self._make_module()
        self.assertFalse(validate_plugin(mod))

    def test_plugin_info_must_return_dict_with_name(self):
        mod = self._make_module(plugin_info=lambda: {"version": "1"})
        self.assertFalse(validate_plugin(mod))

    def test_health_check_true_passes(self):
        mod = self._make_module(
            plugin_info=lambda: {"name": "t"},
            health_check=lambda: True,
        )
        self.assertTrue(validate_plugin(mod))

    def test_health_check_false_fails(self):
        mod = self._make_module(
            plugin_info=lambda: {"name": "t"},
            health_check=lambda: False,
        )
        self.assertFalse(validate_plugin(mod))

    def test_health_check_exception_fails(self):
        def _boom():
            raise RuntimeError("boom")
        mod = self._make_module(plugin_info=lambda: {"name": "t"}, health_check=_boom)
        self.assertFalse(validate_plugin(mod))

    def test_no_health_check_passes(self):
        mod = self._make_module(plugin_info=lambda: {"name": "t"})
        self.assertTrue(validate_plugin(mod))


# ── PluginManager ──────────────────────────────────────────────────────────

class TestPluginManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugins_dir = Path(self.tmp.name) / "plugins"
        self.plugins_dir.mkdir()
        self.config_file = Path(self.tmp.name) / "plugins.toml"
        self.config_file.write_text('enabled = []')

    def tearDown(self):
        self.tmp.cleanup()

    def _write_plugin(self, name, code):
        (self.plugins_dir / f"{name}.py").write_text(textwrap.dedent(code))

    def _write_plugin_config(self, name, text):
        (self.plugins_dir / f"{name}.toml").write_text(text)

    def _mgr(self, **kw):
        m = PluginManager(self.plugins_dir, self.config_file, poll_interval=0.1, **kw)
        m.initial_load()
        return m

    def test_initial_load_picks_up_enabled_plugins(self):
        self._write_plugin("a", '''\
            def plugin_info():
                return {"name": "a", "version": "1"}
        ''')
        self._write_plugin_config("a", "enabled = true")
        m = self._mgr()
        self.assertEqual(len(m.plugins), 1)
        self.assertEqual(m.plugins[0].plugin_info()["name"], "a")

    def test_initial_load_skips_disabled(self):
        self._write_plugin("a", '''\
            def plugin_info():
                return {"name": "a", "version": "1"}
        ''')
        self._write_plugin_config("a", "enabled = false")
        m = self._mgr()
        self.assertEqual(len(m.plugins), 0)

    def test_detects_new_plugin_file(self):
        m = self._mgr()
        self.assertEqual(len(m.plugins), 0)
        self._write_plugin("b", '''\
            def plugin_info():
                return {"name": "b", "version": "1"}
        ''')
        self._write_plugin_config("b", "enabled = true")
        m.check_and_reload()
        self.assertEqual(len(m.plugins), 1)

    def test_detects_config_change(self):
        self._write_plugin("c", '''\
            def plugin_info():
                return {"name": "c", "version": "1"}
        ''')
        self._write_plugin_config("c", "enabled = false")
        m = self._mgr()
        self.assertEqual(len(m.plugins), 0)
        # Enable it
        time.sleep(0.05)  # ensure mtime differs
        self._write_plugin_config("c", "enabled = true")
        m.check_and_reload()
        self.assertEqual(len(m.plugins), 1)

    def test_skips_plugin_that_fails_health_check(self):
        self._write_plugin("sick", '''\
            def plugin_info():
                return {"name": "sick", "version": "1"}
            def health_check():
                return False
        ''')
        self._write_plugin_config("sick", "enabled = true")
        m = self._mgr()
        self.assertEqual(len(m.plugins), 0)

    def test_passes_local_config_to_configure(self):
        self._write_plugin("cfg", '''\
            received = {}
            def plugin_info():
                return {"name": "cfg", "version": "1"}
            def configure(config):
                received.update(config)
        ''')
        self._write_plugin_config("cfg", 'enabled = true\nmy_key = "my_val"')
        m = self._mgr()
        self.assertEqual(m.plugins[0].received, {"my_key": "my_val"})

    def test_local_config_does_not_pass_enabled_to_configure(self):
        self._write_plugin("cfg2", '''\
            received = {}
            def plugin_info():
                return {"name": "cfg2", "version": "1"}
            def configure(config):
                received.update(config)
        ''')
        self._write_plugin_config("cfg2", 'enabled = true\nfoo = "bar"')
        m = self._mgr()
        self.assertNotIn("enabled", m.plugins[0].received)

    def test_swap_immediate_when_idle(self):
        m = self._mgr()
        self._write_plugin("d", '''\
            def plugin_info():
                return {"name": "d", "version": "1"}
        ''')
        self._write_plugin_config("d", "enabled = true")
        m.check_and_reload()
        self.assertEqual(len(m.plugins), 1)

    def test_swap_deferred_when_busy(self):
        self._write_plugin("e", '''\
            def plugin_info():
                return {"name": "e", "version": "1"}
        ''')
        self._write_plugin_config("e", "enabled = true")
        m = self._mgr()
        self.assertEqual(len(m.plugins), 1)

        # Simulate in-flight request
        m.enter_request()

        # Modify plugin to verify reload happens
        self._write_plugin("f", '''\
            def plugin_info():
                return {"name": "f", "version": "1"}
        ''')
        self._write_plugin_config("f", "enabled = true")
        time.sleep(0.05)
        m.check_and_reload()

        # Should still have old plugins (pending swap)
        names = [p.plugin_info()["name"] for p in m.plugins]
        self.assertNotIn("f", names)
        self.assertTrue(m.has_pending_swap)

    def test_pending_swap_applied_on_exit_request(self):
        self._write_plugin("g", '''\
            def plugin_info():
                return {"name": "g", "version": "1"}
        ''')
        self._write_plugin_config("g", "enabled = true")
        m = self._mgr()
        m.enter_request()

        self._write_plugin("h", '''\
            def plugin_info():
                return {"name": "h", "version": "1"}
        ''')
        self._write_plugin_config("h", "enabled = true")
        time.sleep(0.05)
        m.check_and_reload()

        # Complete the in-flight request
        m.exit_request()

        names = [p.plugin_info()["name"] for p in m.plugins]
        self.assertIn("h", names)

    def test_forced_swap_after_timeout(self):
        self._write_plugin("i", '''\
            def plugin_info():
                return {"name": "i", "version": "1"}
        ''')
        self._write_plugin_config("i", "enabled = true")
        m = self._mgr(swap_timeout=0.1)
        m.enter_request()

        self._write_plugin("j", '''\
            def plugin_info():
                return {"name": "j", "version": "1"}
        ''')
        self._write_plugin_config("j", "enabled = true")
        time.sleep(0.05)
        m.check_and_reload()
        self.assertTrue(m.has_pending_swap)

        # Wait for timeout
        time.sleep(0.15)
        m.check_and_reload()

        names = [p.plugin_info()["name"] for p in m.plugins]
        self.assertIn("j", names)
        self.assertFalse(m.has_pending_swap)

    def test_backward_compat_global_enabled_list(self):
        """Plugins enabled via plugins.toml enabled=[] still work."""
        self._write_plugin("legacy", '''\
            def plugin_info():
                return {"name": "legacy", "version": "1"}
        ''')
        self.config_file.write_text('enabled = ["legacy"]')
        m = self._mgr()
        self.assertEqual(len(m.plugins), 1)
        self.assertEqual(m.plugins[0].plugin_info()["name"], "legacy")

    def test_global_section_merged_with_local_config(self):
        """Global plugins.toml [name] section merges with local .toml, local wins."""
        self._write_plugin("merge", '''\
            received = {}
            def plugin_info():
                return {"name": "merge", "version": "1"}
            def configure(config):
                received.update(config)
        ''')
        self.config_file.write_text(
            'enabled = []\n[merge]\nglobal_key = "from_global"\nshared = "global_val"'
        )
        self._write_plugin_config("merge", 'enabled = true\nshared = "local_val"\nlocal_key = "from_local"')
        m = self._mgr()
        self.assertEqual(m.plugins[0].received["global_key"], "from_global")
        self.assertEqual(m.plugins[0].received["shared"], "local_val")
        self.assertEqual(m.plugins[0].received["local_key"], "from_local")


# ── main() dedup guard ─────────────────────────────────────────────────────

class TestMainDedup(unittest.TestCase):
    def test_exits_0_silently_when_proxy_already_running(self):
        """main() must not start a second instance — exit 0 if already running."""
        with unittest.mock.patch.object(_proxy_mod, "is_proxy_running", return_value=True):
            with unittest.mock.patch("sys.argv", ["proxy.py"]):
                with self.assertRaises(SystemExit) as cm:
                    _proxy_mod.main()
        self.assertEqual(cm.exception.code, 0)

    def test_does_not_exit_when_proxy_not_running(self):
        """main() must proceed when no instance is running."""
        # We stop it after argparse so we don't actually bind to a port.
        class _StopEarly(Exception):
            pass

        def _fake_server(*a, **kw):
            raise _StopEarly

        with unittest.mock.patch.object(_proxy_mod, "is_proxy_running", return_value=False), \
             unittest.mock.patch.object(_proxy_mod, "_acquire_startup_lock", return_value=99), \
             unittest.mock.patch.object(_proxy_mod, "_port_in_use", return_value=False):
            with unittest.mock.patch("sys.argv", ["proxy.py"]):
                with unittest.mock.patch.object(_proxy_mod, "ThreadedHTTPServer", side_effect=_StopEarly):
                    with unittest.mock.patch.object(_proxy_mod, "_write_pid"):
                        with self.assertRaises(_StopEarly):
                            _proxy_mod.main()


if __name__ == "__main__":
    unittest.main()

# ── _extract_cwd ──────────────────────────────────────────────────────────

class TestExtractCwd(unittest.TestCase):
    """Verify cwd extraction from Claude Code system prompt formats."""

    REAL_SYSTEM = (
        "# Environment\n"
        "You have been invoked in the following environment:\n"
        " - Primary working directory: /Users/x/Projects/project-1\n"
        "  - Is a git repository: true\n"
        " - Platform: darwin\n"
        " - Shell: zsh\n"
    )

    def test_extracts_primary_working_directory(self):
        self.assertEqual(
            _extract_cwd({"system": self.REAL_SYSTEM}),
            "/Users/x/Projects/project-1",
        )

    def test_extracts_from_list_system(self):
        payload = {"system": [{"type": "text", "text": self.REAL_SYSTEM}]}
        self.assertEqual(_extract_cwd(payload), "/Users/x/Projects/project-1")

    def test_extracts_legacy_cwd_format(self):
        self.assertEqual(
            _extract_cwd({"system": "<env>\ncwd: /tmp/p2\n</env>"}),
            "/tmp/p2",
        )

    def test_extracts_working_directory_format(self):
        self.assertEqual(
            _extract_cwd({"system": "Working directory: /home/user/proj"}),
            "/home/user/proj",
        )

    def test_returns_empty_when_no_match(self):
        self.assertEqual(_extract_cwd({"system": "nothing here"}), "")

    def test_returns_empty_for_missing_system(self):
        self.assertEqual(_extract_cwd({}), "")

    def test_returns_empty_for_non_string_system(self):
        self.assertEqual(_extract_cwd({"system": 42}), "")

    def test_primary_wins_over_generic(self):
        """Primary working directory pattern takes priority."""
        mixed = (
            "Working directory: /generic\n"
            " - Primary working directory: /primary\n"
        )
        self.assertEqual(_extract_cwd({"system": mixed}), "/primary")

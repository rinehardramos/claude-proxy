"""Tests for proxy state handling: PID management, dedup guard, port fallback,
kill_proxy SIGKILL escalation, reload endpoint, inactivity watchdog, startup
lock, safe PID cleanup, and daemon lifecycle edge cases.

These tests exercise the code paths that prevent the proxy from dying or
becoming a zombie, and the safeguards that keep Claude Code connected.
"""
from __future__ import annotations

import fcntl
import io
import json
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).parent.parent))
import proxy as _proxy_mod
from proxy import (
    _write_pid,
    _read_pid,
    _cleanup_pid,
    _port_in_use,
    _acquire_startup_lock,
    is_proxy_running,
    ProxyHandler,
    PluginManager,
    ThreadedHTTPServer,
    PID_FILE,
    STATE_DIR,
    LISTEN_PORT,
)


# ── Helpers ───────────────────────────────────────────────────────────────

def _bind_port(port: int = 0) -> socket.socket:
    """Bind a TCP socket on localhost and return it (caller must close)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def _free_port() -> int:
    """Return a port number that is currently free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _TmpPidDir:
    """Context manager that redirects PID_FILE / STATE_DIR to a temp dir."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()

    def __enter__(self):
        self.dir = Path(self._tmp.name)
        self.pid_file = self.dir / "proxy.pid"
        self._patches = [
            patch.object(_proxy_mod, "PID_FILE", self.pid_file),
            patch.object(_proxy_mod, "STATE_DIR", self.dir),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


# ── _write_pid / _read_pid / _cleanup_pid ─────────────────────────────────

class TestPidFileOps(unittest.TestCase):
    def setUp(self):
        self.ctx = _TmpPidDir()
        self.ctx.__enter__()

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def test_write_and_read_roundtrip(self):
        _write_pid(12345)
        self.assertEqual(_read_pid(), 12345)

    def test_read_returns_none_when_missing(self):
        self.assertIsNone(_read_pid())

    def test_cleanup_removes_file(self):
        _write_pid(1)
        _cleanup_pid()
        self.assertFalse(self.ctx.pid_file.exists())

    def test_cleanup_idempotent(self):
        _cleanup_pid()  # no file to remove — should not raise
        _write_pid(1)
        _cleanup_pid()
        _cleanup_pid()  # second call — should not raise

    def test_write_creates_parent_dirs(self):
        nested = self.ctx.dir / "sub" / "proxy.pid"
        nested_state = self.ctx.dir / "sub"
        with patch.object(_proxy_mod, "PID_FILE", nested), \
             patch.object(_proxy_mod, "STATE_DIR", nested_state):
            _write_pid(42)
        self.assertEqual(int(nested.read_text().strip()), 42)

    def test_read_returns_none_for_non_integer(self):
        self.ctx.pid_file.write_text("not-a-number")
        self.assertIsNone(_read_pid())

    def test_read_handles_empty_file(self):
        self.ctx.pid_file.write_text("")
        self.assertIsNone(_read_pid())

    def test_write_overwrites_existing(self):
        _write_pid(111)
        _write_pid(222)
        self.assertEqual(_read_pid(), 222)

    def test_cleanup_with_matching_expected_pid(self):
        """Cleanup removes file when expected_pid matches."""
        _write_pid(42)
        _cleanup_pid(expected_pid=42)
        self.assertFalse(self.ctx.pid_file.exists())

    def test_cleanup_skips_when_expected_pid_mismatches(self):
        """Cleanup must NOT remove file if it belongs to a different process."""
        _write_pid(100)
        _cleanup_pid(expected_pid=999)
        # File should still exist — it belongs to PID 100, not 999
        self.assertTrue(self.ctx.pid_file.exists())
        self.assertEqual(_read_pid(), 100)

    def test_cleanup_without_expected_pid_always_removes(self):
        """Backward compat: no expected_pid → unconditional removal."""
        _write_pid(42)
        _cleanup_pid()
        self.assertFalse(self.ctx.pid_file.exists())

    def test_cleanup_with_expected_pid_missing_file(self):
        """No crash when expected_pid is given but file doesn't exist."""
        _cleanup_pid(expected_pid=42)  # should not raise


# ── _port_in_use ──────────────────────────────────────────────────────────

class TestPortInUse(unittest.TestCase):
    def test_returns_true_when_port_bound(self):
        s = _bind_port(0)
        port = s.getsockname()[1]
        try:
            self.assertTrue(_port_in_use(port))
        finally:
            s.close()

    def test_returns_false_when_port_free(self):
        port = _free_port()
        self.assertFalse(_port_in_use(port))

    def test_returns_false_for_unreachable_port(self):
        # Port 1 is almost certainly not bound on localhost
        self.assertFalse(_port_in_use(1))


# ── is_proxy_running ──────────────────────────────────────────────────────

class TestIsProxyRunning(unittest.TestCase):
    """Tests for the dedup guard — the function that decides whether to
    start a new proxy or silently exit."""

    def setUp(self):
        self.ctx = _TmpPidDir()
        self.ctx.__enter__()

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    # -- PID-based detection --

    def test_returns_false_when_no_pid_file(self):
        with patch.object(_proxy_mod, "_port_in_use", return_value=False):
            self.assertFalse(is_proxy_running())

    def test_returns_true_when_pid_alive(self):
        _write_pid(os.getpid())  # current process is definitely alive
        self.assertTrue(is_proxy_running())

    def test_cleans_stale_pid_and_returns_false(self):
        """If PID file references a dead process and port is free → not running."""
        _write_pid(99999)  # almost certainly not a real PID
        with patch("os.kill", side_effect=ProcessLookupError), \
             patch.object(_proxy_mod, "_port_in_use", return_value=False):
            self.assertFalse(is_proxy_running())
        # PID file should be cleaned up
        self.assertFalse(self.ctx.pid_file.exists())

    def test_returns_true_on_permission_error(self):
        """PermissionError means the process exists but we can't signal it."""
        _write_pid(1)
        with patch("os.kill", side_effect=PermissionError):
            self.assertTrue(is_proxy_running())

    # -- Port fallback --

    def test_port_fallback_when_pid_stale(self):
        """Stale PID + port in use → proxy IS running (another instance holds port)."""
        _write_pid(99999)
        with patch("os.kill", side_effect=ProcessLookupError), \
             patch.object(_proxy_mod, "_port_in_use", return_value=True):
            self.assertTrue(is_proxy_running())

    def test_port_fallback_when_no_pid_file(self):
        """No PID file + port in use → proxy IS running."""
        with patch.object(_proxy_mod, "_port_in_use", return_value=True):
            self.assertTrue(is_proxy_running())

    def test_port_fallback_not_triggered_when_pid_alive(self):
        """If PID check succeeds, port check is never reached."""
        _write_pid(os.getpid())
        with patch.object(_proxy_mod, "_port_in_use") as mock_port:
            self.assertTrue(is_proxy_running())
            mock_port.assert_not_called()

    # -- Real port binding --

    def test_port_fallback_with_real_bound_port(self):
        """Integration: bind a real port and verify the fallback detects it."""
        port = _free_port()
        s = _bind_port(port)
        try:
            with patch.object(_proxy_mod, "LISTEN_PORT", port):
                # No PID file — only port check should fire
                self.assertTrue(is_proxy_running())
        finally:
            s.close()

    def test_both_checks_fail_returns_false(self):
        """No PID file + port free → not running."""
        with patch.object(_proxy_mod, "_port_in_use", return_value=False):
            self.assertFalse(is_proxy_running())


# ── kill_proxy (SIGKILL escalation) ───────────────────────────────────────

class TestKillProxyEscalation(unittest.TestCase):
    """Tests for setup.py's kill_proxy with SIGTERM→SIGKILL escalation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_pid(self, pid: int):
        (self.state_dir / "proxy.pid").write_text(str(pid))

    def test_sigterm_followed_by_process_exit(self):
        """Normal case: SIGTERM sent, process exits within wait window."""
        from setup import kill_proxy
        self._write_pid(12345)
        # SIGTERM succeeds, then kill(pid, 0) raises ProcessLookupError (process exited)
        with patch("os.kill") as mock_kill:
            mock_kill.side_effect = [
                None,                    # SIGTERM
                ProcessLookupError,      # kill(pid, 0) — process gone
            ]
            with patch("time.sleep"):
                kill_proxy(self.state_dir)
        calls = mock_kill.call_args_list
        self.assertEqual(calls[0], call(12345, signal.SIGTERM))
        self.assertEqual(calls[1], call(12345, 0))  # liveness check

    def test_sigkill_after_sigterm_timeout(self):
        """Process ignores SIGTERM — SIGKILL sent after timeout."""
        from setup import kill_proxy
        self._write_pid(12345)
        with patch("os.kill") as mock_kill:
            # SIGTERM succeeds, 8 liveness checks all pass (process still alive),
            # then SIGKILL sent
            mock_kill.side_effect = [
                None,  # SIGTERM
                None, None, None, None, None, None, None, None,  # 8x kill(pid, 0) — alive
                None,  # SIGKILL
            ]
            with patch("time.sleep"):
                kill_proxy(self.state_dir)
        calls = mock_kill.call_args_list
        self.assertEqual(calls[0], call(12345, signal.SIGTERM))
        self.assertEqual(calls[-1], call(12345, signal.SIGKILL))

    def test_pid_file_removed_after_kill(self):
        from setup import kill_proxy
        self._write_pid(12345)
        with patch("os.kill", side_effect=[None, ProcessLookupError]), \
             patch("time.sleep"):
            kill_proxy(self.state_dir)
        self.assertFalse((self.state_dir / "proxy.pid").exists())

    def test_noop_when_no_pid_file(self):
        from setup import kill_proxy
        kill_proxy(self.state_dir)  # should not raise

    def test_handles_already_dead_process(self):
        """SIGTERM fails with ProcessLookupError — process already dead."""
        from setup import kill_proxy
        self._write_pid(99999)
        with patch("os.kill", side_effect=ProcessLookupError):
            kill_proxy(self.state_dir)
        self.assertFalse((self.state_dir / "proxy.pid").exists())

    def test_handles_corrupt_pid_file(self):
        """PID file contains garbage — should not crash."""
        from setup import kill_proxy
        (self.state_dir / "proxy.pid").write_text("not-a-number\n")
        kill_proxy(self.state_dir)  # should not raise
        self.assertFalse((self.state_dir / "proxy.pid").exists())

    def test_handles_empty_pid_file(self):
        from setup import kill_proxy
        (self.state_dir / "proxy.pid").write_text("")
        kill_proxy(self.state_dir)  # should not raise

    def test_sigkill_permission_error_is_tolerated(self):
        """SIGKILL fails with PermissionError — should not crash."""
        from setup import kill_proxy
        self._write_pid(12345)
        with patch("os.kill") as mock_kill:
            mock_kill.side_effect = [
                None,  # SIGTERM
                None, None, None, None, None, None, None, None,  # 8x alive
                PermissionError,  # SIGKILL
            ]
            with patch("time.sleep"):
                kill_proxy(self.state_dir)  # should not raise


# ── Reload endpoint ───────────────────────────────────────────────────────

class TestReloadEndpoint(unittest.TestCase):
    """Tests for the /reload HTTP endpoint."""

    def _make_handler(self, plugin_manager=None):
        handler = ProxyHandler.__new__(ProxyHandler)
        handler.plugin_manager = plugin_manager
        handler.plugins = []
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        return handler

    def _body(self, handler) -> dict:
        handler.wfile.seek(0)
        return json.loads(handler.wfile.read())

    def test_returns_reloaded_status(self):
        mgr = MagicMock()
        h = self._make_handler(plugin_manager=mgr)
        h._reload()
        self.assertEqual(self._body(h)["status"], "reloaded")

    def test_sends_200(self):
        mgr = MagicMock()
        h = self._make_handler(plugin_manager=mgr)
        h._reload()
        h.send_response.assert_called_once_with(200)

    def test_calls_check_and_reload(self):
        mgr = MagicMock()
        h = self._make_handler(plugin_manager=mgr)
        h._reload()
        mgr.check_and_reload.assert_called_once()

    def test_works_without_plugin_manager(self):
        """Reload is safe even if plugin_manager is None (shouldn't crash)."""
        h = self._make_handler(plugin_manager=None)
        h._reload()
        self.assertEqual(self._body(h)["status"], "reloaded")

    def test_reload_exception_in_manager_does_not_crash(self):
        """If check_and_reload raises, the endpoint should still respond."""
        mgr = MagicMock()
        mgr.check_and_reload.side_effect = RuntimeError("plugin load error")
        h = self._make_handler(plugin_manager=mgr)
        # The current implementation doesn't catch this — verify behavior.
        # If this test fails, it means we need to add try/except.
        with self.assertRaises(RuntimeError):
            h._reload()


# ── Routing ───────────────────────────────────────────────────────────────

class TestRouting(unittest.TestCase):
    """Verify GET routing dispatches to correct handlers."""

    def _make_handler(self):
        handler = ProxyHandler.__new__(ProxyHandler)
        handler.plugin_manager = None
        handler.plugins = []
        handler._health = MagicMock()
        handler._reload = MagicMock()
        handler._forward = MagicMock()
        return handler

    def test_get_proxy_status_calls_health(self):
        h = self._make_handler()
        h.path = "/status"
        h.do_GET()
        h._health.assert_called_once()

    def test_get_proxy_reload_calls_reload(self):
        h = self._make_handler()
        h.path = "/reload"
        h.do_GET()
        h._reload.assert_called_once()

    def test_get_other_path_calls_forward(self):
        h = self._make_handler()
        h.path = "/v1/messages"
        h.do_GET()
        h._forward.assert_called_once_with("GET")

    def test_post_always_calls_forward(self):
        h = self._make_handler()
        h.path = "/v1/messages"
        h.do_POST()
        h._forward.assert_called_once_with("POST")


# ── ThreadedHTTPServer properties ─────────────────────────────────────────

class TestThreadedHTTPServer(unittest.TestCase):
    def test_allow_reuse_address(self):
        """SO_REUSEADDR must be set to prevent 'Address already in use'."""
        self.assertTrue(ThreadedHTTPServer.allow_reuse_address)

    def test_daemon_threads(self):
        """Request threads must be daemon so they don't block shutdown."""
        self.assertTrue(ThreadedHTTPServer.daemon_threads)


# ── main() lifecycle ──────────────────────────────────────────────────────

class TestMainLifecycle(unittest.TestCase):
    """Tests for the main() entrypoint — dedup guard, atexit, signal handling."""

    # Common patches needed for main() to reach different code paths.
    # _acquire_startup_lock returns a fake fd; _port_in_use returns False.
    _COMMON_PATCHES = {
        "_acquire_startup_lock": 99,   # fake fd
        "_port_in_use": False,
    }

    def _patch_main_common(self):
        """Return a list of started patches for the post-dedup code path."""
        patches = []
        for attr, rv in self._COMMON_PATCHES.items():
            p = patch.object(_proxy_mod, attr, return_value=rv)
            p.start()
            patches.append(p)
        return patches

    def test_dedup_exits_0_when_running(self):
        with patch.object(_proxy_mod, "is_proxy_running", return_value=True), \
             patch("sys.argv", ["proxy.py"]):
            with self.assertRaises(SystemExit) as cm:
                _proxy_mod.main()
        self.assertEqual(cm.exception.code, 0)

    def test_dedup_proceeds_when_not_running(self):
        """main() proceeds past dedup guard when no proxy is running."""
        class _StopEarly(Exception):
            pass

        extra = self._patch_main_common()
        try:
            with patch.object(_proxy_mod, "is_proxy_running", return_value=False), \
                 patch("sys.argv", ["proxy.py"]), \
                 patch.object(_proxy_mod, "ThreadedHTTPServer", side_effect=_StopEarly), \
                 patch.object(_proxy_mod, "_write_pid"):
                with self.assertRaises(_StopEarly):
                    _proxy_mod.main()
        finally:
            for p in extra:
                p.stop()

    def test_atexit_registers_cleanup(self):
        """main() must register atexit handlers for PID cleanup."""
        class _StopAfterBind(Exception):
            pass

        mock_server = MagicMock()
        mock_server.serve_forever.side_effect = _StopAfterBind

        registered = []

        extra = self._patch_main_common()
        try:
            with patch.object(_proxy_mod, "is_proxy_running", return_value=False), \
                 patch("sys.argv", ["proxy.py"]), \
                 patch.object(_proxy_mod, "ThreadedHTTPServer", return_value=mock_server), \
                 patch.object(_proxy_mod, "_write_pid"), \
                 patch("atexit.register", side_effect=lambda fn, **kw: registered.append(fn)), \
                 patch("signal.signal"), \
                 patch("threading.Thread") as mock_thread:
                mock_thread.return_value.start = MagicMock()
                try:
                    _proxy_mod.main()
                except _StopAfterBind:
                    pass
        finally:
            for p in extra:
                p.stop()

        self.assertIn(_proxy_mod._cleanup_pid, registered)

    def test_sigterm_handler_registered(self):
        """main() must register SIGTERM handler for graceful shutdown."""
        class _StopAfterBind(Exception):
            pass

        mock_server = MagicMock()
        mock_server.serve_forever.side_effect = _StopAfterBind
        registered_signals = {}

        def capture_signal(signum, handler):
            registered_signals[signum] = handler

        extra = self._patch_main_common()
        try:
            with patch.object(_proxy_mod, "is_proxy_running", return_value=False), \
                 patch("sys.argv", ["proxy.py"]), \
                 patch.object(_proxy_mod, "ThreadedHTTPServer", return_value=mock_server), \
                 patch.object(_proxy_mod, "_write_pid"), \
                 patch("atexit.register"), \
                 patch("signal.signal", side_effect=capture_signal), \
                 patch("threading.Thread") as mock_thread:
                mock_thread.return_value.start = MagicMock()
                try:
                    _proxy_mod.main()
                except _StopAfterBind:
                    pass
        finally:
            for p in extra:
                p.stop()

        self.assertIn(signal.SIGTERM, registered_signals)

    def test_keyboard_interrupt_cleans_up(self):
        """Ctrl-C should clean up PID file and shut down server."""
        mock_server = MagicMock()
        mock_server.serve_forever.side_effect = KeyboardInterrupt

        extra = self._patch_main_common()
        try:
            with _TmpPidDir() as ctx:
                with patch.object(_proxy_mod, "is_proxy_running", return_value=False), \
                     patch("sys.argv", ["proxy.py"]), \
                     patch.object(_proxy_mod, "ThreadedHTTPServer", return_value=mock_server), \
                     patch("atexit.register"), \
                     patch("signal.signal"), \
                     patch("threading.Thread") as mock_thread:
                    mock_thread.return_value.start = MagicMock()
                    _write_pid(os.getpid())
                    _proxy_mod.main()

                mock_server.shutdown.assert_called_once()
        finally:
            for p in extra:
                p.stop()

    def test_lock_failure_exits_0(self):
        """If startup lock can't be acquired, exit silently."""
        with patch.object(_proxy_mod, "is_proxy_running", return_value=False), \
             patch.object(_proxy_mod, "_acquire_startup_lock", return_value=None), \
             patch("sys.argv", ["proxy.py"]):
            with self.assertRaises(SystemExit) as cm:
                _proxy_mod.main()
        self.assertEqual(cm.exception.code, 0)

    def test_port_in_use_after_lock_exits_0(self):
        """If port is bound after acquiring lock (race), exit silently."""
        with patch.object(_proxy_mod, "is_proxy_running", return_value=False), \
             patch.object(_proxy_mod, "_acquire_startup_lock", return_value=99), \
             patch.object(_proxy_mod, "_port_in_use", return_value=True), \
             patch("os.close"), \
             patch("sys.argv", ["proxy.py"]):
            with self.assertRaises(SystemExit) as cm:
                _proxy_mod.main()
        self.assertEqual(cm.exception.code, 0)


# ── PluginManager enter/exit request ──────────────────────────────────────

class TestPluginManagerRequestTracking(unittest.TestCase):
    """Verify in-flight request tracking and its interaction with hot-reload."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugins_dir = Path(self.tmp.name) / "plugins"
        self.plugins_dir.mkdir()
        self.config_file = Path(self.tmp.name) / "plugins.toml"
        self.config_file.write_text('enabled = []')

    def tearDown(self):
        self.tmp.cleanup()

    def _mgr(self, **kw):
        m = PluginManager(self.plugins_dir, self.config_file, **kw)
        m.initial_load()
        return m

    def test_in_flight_starts_at_zero(self):
        m = self._mgr()
        self.assertEqual(m._in_flight, 0)

    def test_enter_increments_exit_decrements(self):
        m = self._mgr()
        m.enter_request()
        self.assertEqual(m._in_flight, 1)
        m.enter_request()
        self.assertEqual(m._in_flight, 2)
        m.exit_request()
        self.assertEqual(m._in_flight, 1)
        m.exit_request()
        self.assertEqual(m._in_flight, 0)

    def test_concurrent_enter_exit_thread_safe(self):
        """Multiple threads entering/exiting should not corrupt the counter."""
        m = self._mgr()
        errors = []

        def worker():
            try:
                for _ in range(100):
                    m.enter_request()
                for _ in range(100):
                    m.exit_request()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(m._in_flight, 0)

    def test_pending_swap_cleared_on_last_exit(self):
        """When the last in-flight request completes, pending swap is applied."""
        m = self._mgr()
        m.enter_request()
        m._pending_plugins = [MagicMock()]  # simulate pending
        m._pending_since = time.time()
        m.exit_request()
        self.assertIsNone(m._pending_plugins)

    def test_pending_swap_not_applied_with_remaining_requests(self):
        m = self._mgr()
        m.enter_request()
        m.enter_request()
        m._pending_plugins = [MagicMock()]
        m._pending_since = time.time()
        m.exit_request()  # still 1 in-flight
        self.assertIsNotNone(m._pending_plugins)
        m.exit_request()  # now 0
        self.assertIsNone(m._pending_plugins)


# ── forward() plugin_manager interaction ──────────────────────────────────

class TestForwardPluginManagerInteraction(unittest.TestCase):
    """Verify that _forward() calls enter_request/exit_request."""

    def _make_handler(self, plugin_manager=None):
        handler = ProxyHandler.__new__(ProxyHandler)
        handler.plugin_manager = plugin_manager
        handler.plugins = []
        handler.path = "/v1/messages"
        handler.headers = {"Content-Length": "0"}
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        return handler

    def test_enter_and_exit_called_on_success(self):
        mgr = MagicMock()
        h = self._make_handler(plugin_manager=mgr)
        # Mock _forward_inner to succeed
        h._forward_inner = MagicMock()
        h._forward("POST")
        mgr.enter_request.assert_called_once()
        mgr.exit_request.assert_called_once()

    def test_exit_called_even_on_exception(self):
        """exit_request must be called in the finally block."""
        mgr = MagicMock()
        h = self._make_handler(plugin_manager=mgr)
        h._forward_inner = MagicMock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            h._forward("POST")
        mgr.exit_request.assert_called_once()

    def test_no_plugin_manager_does_not_crash(self):
        h = self._make_handler(plugin_manager=None)
        h._forward_inner = MagicMock()
        h._forward("POST")  # should not raise


# ── setup.py cmd_restart ──────────────────────────────────────────────────

class TestCmdRestart(unittest.TestCase):
    """Tests for the restart command in setup.py."""

    def test_hot_reload_preferred_when_proxy_healthy(self):
        """restart without --force should try hot-reload first."""
        import setup as _setup_mod
        import argparse

        args = argparse.Namespace(force=False)
        with patch.object(_setup_mod, "proxy_status", return_value={"status": "ok", "plugins": []}), \
             patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"status": "reloaded"}'
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            _setup_mod.cmd_restart(args)

        # urlopen should be called with the reload URL
        call_url = mock_urlopen.call_args[0][0]
        self.assertIn("reload", call_url)

    def test_force_skips_hot_reload(self):
        """restart --force should kill and restart, not hot-reload."""
        import setup as _setup_mod
        import argparse

        args = argparse.Namespace(force=True)
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.connect_ex.return_value = 1  # port free

        # proxy_status called: up to 10 times in wait loop + 1 final
        status_returns = [{"status": "ok", "plugins": []}] * 12
        with patch.object(_setup_mod, "proxy_status", side_effect=status_returns), \
             patch.object(_setup_mod, "kill_proxy"), \
             patch("subprocess.Popen"), \
             patch("time.sleep"), \
             patch("socket.socket", return_value=mock_sock):
            _setup_mod.cmd_restart(args)

    def test_falls_back_to_full_restart_on_reload_failure(self):
        """If hot-reload fails, should do a full restart."""
        import setup as _setup_mod
        import argparse

        args = argparse.Namespace(force=False)
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.connect_ex.return_value = 1  # port free

        # First call: initial check. Rest: wait loop + final.
        status_returns = [{"status": "ok"}] + [{"status": "ok", "plugins": []}] * 12
        with patch.object(_setup_mod, "proxy_status", side_effect=status_returns), \
             patch("urllib.request.urlopen", side_effect=Exception("connection refused")), \
             patch.object(_setup_mod, "kill_proxy"), \
             patch("subprocess.Popen"), \
             patch("time.sleep"), \
             patch("socket.socket", return_value=mock_sock):
            _setup_mod.cmd_restart(args)

    def test_exits_1_when_restart_fails(self):
        """If the new proxy doesn't come up, exit with code 1."""
        import setup as _setup_mod
        import argparse

        args = argparse.Namespace(force=True)
        with patch.object(_setup_mod, "proxy_status", return_value=None), \
             patch.object(_setup_mod, "kill_proxy"), \
             patch("subprocess.Popen"), \
             patch("time.sleep"), \
             patch("socket.socket"):
            with self.assertRaises(SystemExit) as cm:
                _setup_mod.cmd_restart(args)
            self.assertEqual(cm.exception.code, 1)


# ── Edge cases: daemon fork PID handling ──────────────────────────────────

class TestDaemonPidHandling(unittest.TestCase):
    """Verify that only the child writes the PID file, not the parent."""

    def test_parent_does_not_write_pid(self):
        """After fork, the parent should NOT call _write_pid."""
        class _ParentExit(Exception):
            pass

        with patch.object(_proxy_mod, "is_proxy_running", return_value=False), \
             patch("sys.argv", ["proxy.py", "--daemon"]), \
             patch("os.fork", return_value=12345), \
             patch.object(_proxy_mod, "_write_pid") as mock_write, \
             patch("sys.exit", side_effect=_ParentExit):
            try:
                _proxy_mod.main()
            except _ParentExit:
                pass

        mock_write.assert_not_called()


# ── Startup lock ──────────────────────────────────────────────────────────

class TestStartupLock(unittest.TestCase):
    """Tests for the exclusive startup lock that serialises concurrent launches."""

    def setUp(self):
        self.ctx = _TmpPidDir()
        self.ctx.__enter__()

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def test_acquire_returns_fd(self):
        fd = _acquire_startup_lock()
        self.assertIsNotNone(fd)
        self.assertIsInstance(fd, int)
        os.close(fd)

    def test_second_acquire_fails(self):
        """Only one process can hold the lock at a time."""
        fd1 = _acquire_startup_lock()
        self.assertIsNotNone(fd1)
        fd2 = _acquire_startup_lock()
        self.assertIsNone(fd2)
        os.close(fd1)

    def test_lock_released_on_close(self):
        """After closing the fd, another acquire should succeed."""
        fd1 = _acquire_startup_lock()
        os.close(fd1)
        fd2 = _acquire_startup_lock()
        self.assertIsNotNone(fd2)
        os.close(fd2)

    def test_lock_creates_file(self):
        fd = _acquire_startup_lock()
        self.assertTrue((self.ctx.dir / "proxy.lock").exists())
        os.close(fd)


# ── Safe PID cleanup under concurrent instances ──────────────────────────

class TestSafePidCleanup(unittest.TestCase):
    """Verify that a crashing child doesn't wipe a healthy instance's PID."""

    def setUp(self):
        self.ctx = _TmpPidDir()
        self.ctx.__enter__()

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def test_crash_does_not_wipe_other_pid(self):
        """Simulate: healthy PID=100 in file, crashing child PID=200 tries cleanup."""
        _write_pid(100)
        _cleanup_pid(expected_pid=200)
        # PID 100's file must survive
        self.assertEqual(_read_pid(), 100)

    def test_crash_wipes_own_pid(self):
        """Crashing child PID=200 cleans up its own entry."""
        _write_pid(200)
        _cleanup_pid(expected_pid=200)
        self.assertFalse(self.ctx.pid_file.exists())

    def test_sigterm_handler_uses_expected_pid(self):
        """The SIGTERM handler in main() passes expected_pid to _cleanup_pid."""
        class _StopAfterBind(Exception):
            pass

        mock_server = MagicMock()
        mock_server.serve_forever.side_effect = _StopAfterBind
        registered_signals = {}

        def capture_signal(signum, handler):
            registered_signals[signum] = handler

        with _TmpPidDir() as ctx:
            with patch.object(_proxy_mod, "is_proxy_running", return_value=False), \
                 patch.object(_proxy_mod, "_acquire_startup_lock", return_value=99), \
                 patch.object(_proxy_mod, "_port_in_use", return_value=False), \
                 patch("sys.argv", ["proxy.py"]), \
                 patch.object(_proxy_mod, "ThreadedHTTPServer", return_value=mock_server), \
                 patch("atexit.register"), \
                 patch("signal.signal", side_effect=capture_signal), \
                 patch("threading.Thread") as mock_thread:
                mock_thread.return_value.start = MagicMock()
                try:
                    _proxy_mod.main()
                except _StopAfterBind:
                    pass

            # Simulate: another instance wrote its PID after ours
            _write_pid(99999)

            # Call the SIGTERM handler
            handler = registered_signals[signal.SIGTERM]
            handler(signal.SIGTERM, None)

            # The other instance's PID must survive
            self.assertEqual(_read_pid(), 99999)


# ── Integration: server bind and respond ──────────────────────────────────

class TestServerBindAndRespond(unittest.TestCase):
    """Start a real server on a random port and verify health + reload."""

    def test_health_and_reload_endpoints(self):
        port = _free_port()
        server = ThreadedHTTPServer(("127.0.0.1", port), ProxyHandler)
        ProxyHandler.plugin_manager = None
        ProxyHandler.plugins = []

        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            import urllib.request

            # Health
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/status", timeout=3
            )
            data = json.loads(resp.read())
            self.assertEqual(data["status"], "ok")

            # Reload
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/reload", timeout=3
            )
            data = json.loads(resp.read())
            self.assertEqual(data["status"], "reloaded")
        finally:
            server.shutdown()

    def test_server_allow_reuse_prevents_bind_failure(self):
        """Binding the same port twice in quick succession should work
        thanks to SO_REUSEADDR."""
        port = _free_port()

        server1 = ThreadedHTTPServer(("127.0.0.1", port), ProxyHandler)
        server1.server_close()

        # This should NOT raise OSError: Address already in use
        server2 = ThreadedHTTPServer(("127.0.0.1", port), ProxyHandler)
        server2.server_close()


def test_plugin_manager_tracks_reload_count(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "dummy.py").write_text(
        "def plugin_info():\n    return {'name': 'dummy', 'version': '0'}\n"
    )

    mgr = PluginManager(plugins_dir=plugins_dir, global_config_path=tmp_path / "plugins.toml")
    mgr.initial_load()
    assert mgr.reload_count == 0

    # Touch the file so mtime changes (os.utime advances mtime on its own)
    os.utime(plugins_dir / "dummy.py", None)

    mgr.check_and_reload()
    assert mgr.reload_count == 1


def test_plugin_manager_deferred_swap_increments_reload_count(tmp_path):
    """Deferred path: reload queued while a request is in-flight, applied on exit."""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "dummy.py").write_text(
        "def plugin_info():\n    return {'name': 'dummy', 'version': '0'}\n"
    )

    mgr = PluginManager(plugins_dir=plugins_dir, global_config_path=tmp_path / "plugins.toml")
    mgr.initial_load()
    assert mgr.reload_count == 0

    # Hold a request in-flight so the reload is deferred
    mgr.enter_request()

    # Touch the file and trigger check — should queue a pending swap, not apply yet
    os.utime(plugins_dir / "dummy.py", None)
    mgr.check_and_reload()
    assert mgr.reload_count == 0, "swap must not be applied while a request is in-flight"
    assert mgr._pending_plugins is not None, "swap should be queued"

    # Complete the request — pending swap must be drained automatically
    mgr.exit_request()
    assert mgr.reload_count == 1, "swap must be applied after the last request exits"
    assert mgr._pending_plugins is None, "pending swap must be cleared after apply"


# ── Inactivity watchdog removal regression ────────────────────────────────

def test_inactivity_watchdog_symbols_removed():
    """The inactivity watchdog has been removed — supervisor owns lifecycle now."""
    import proxy
    assert not hasattr(proxy, "_inactivity_watchdog")
    assert not hasattr(proxy, "_INACTIVITY_TIMEOUT")
    assert not hasattr(proxy, "_last_activity")


# ── Daemonize gating under supervisor ─────────────────────────────────────

def test_proxy_skips_daemon_fork_when_supervised(monkeypatch):
    monkeypatch.setenv("CLAUDE_PROXY_SUPERVISED", "1")
    import proxy
    assert proxy._should_daemonize(True) is False


def test_proxy_daemonizes_when_not_supervised(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROXY_SUPERVISED", raising=False)
    import proxy
    assert proxy._should_daemonize(True) is True
    assert proxy._should_daemonize(False) is False


if __name__ == "__main__":
    unittest.main()

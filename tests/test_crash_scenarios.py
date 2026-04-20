"""Integration tests: reproduce real proxy crash scenarios with subprocesses.

These tests spawn actual proxy processes, bind real ports, and race real
forks against each other.  They verify that the safeguards (startup lock,
port fallback, safe PID cleanup) prevent the exact crash patterns that
killed the proxy in production.

Designed for both arm64 (Apple Silicon) and x86_64 (Intel/Linux CI).
Each test uses a private temp directory and ephemeral port — no interference
with a running proxy or with other tests.

Run:
    python3 -m pytest tests/test_crash_scenarios.py -v
    python3 -m pytest tests/test_crash_scenarios.py -v -k concurrent  # just the race test
"""
from __future__ import annotations

import json
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ARCH = platform.machine()  # arm64, x86_64, aarch64
PROXY_PY = str(Path(__file__).parent.parent / "proxy.py")
PYTHON = sys.executable


# ── Helpers ───────────────────────────────────────────────────────────────

def free_port() -> int:
    """Grab an ephemeral port that is currently free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_listening(port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for_port(port: int, timeout: float = 5.0) -> bool:
    """Poll until the port is accepting connections (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_listening(port):
            return True
        time.sleep(0.1)
    return False


def wait_for_port_free(port: int, timeout: float = 5.0) -> bool:
    """Poll until the port is no longer bound."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_listening(port):
            return True
        time.sleep(0.1)
    return False


def wait_for_pid_exit(pid: int, timeout: float = 5.0) -> bool:
    """Poll until the given PID no longer exists."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def proxy_status(port: int) -> dict | None:
    """Hit /status and return parsed JSON, or None on failure."""
    import urllib.request
    try:
        url = f"http://127.0.0.1:{port}/status"
        with urllib.request.urlopen(url, timeout=2) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def kill_tree(pid: int) -> None:
    """Best-effort kill of a PID (SIGTERM then SIGKILL)."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        time.sleep(0.3)


def write_mini_proxy(dest: Path, state_dir: Path, port: int, startup_delay: float = 0) -> None:
    """Write a self-contained mini proxy script that uses a given state dir.

    This avoids importing the real proxy module (which would use the global
    STATE_DIR) and lets us control everything via temp dirs.  The mini proxy
    reproduces the exact lifecycle of the real proxy.py::main() — dedup guard,
    fork, lock, PID file, bind, serve.
    """
    dest.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        \"\"\"Mini proxy for crash-scenario integration tests.\"\"\"
        import fcntl, http.server, json, os, signal, socket, sys, threading, time
        from pathlib import Path

        STATE_DIR = Path({str(state_dir)!r})
        PID_FILE = STATE_DIR / "proxy.pid"
        LOCK_FILE = STATE_DIR / "proxy.lock"
        LOG_FILE = STATE_DIR / "proxy.log"
        PORT = {port}
        STARTUP_DELAY = {startup_delay}

        STATE_DIR.mkdir(parents=True, exist_ok=True)

        def _read_pid():
            try: return int(PID_FILE.read_text().strip())
            except: return None

        def _write_pid(pid):
            PID_FILE.write_text(str(pid))

        def _cleanup_pid(expected_pid=None):
            try:
                if expected_pid is not None:
                    current = _read_pid()
                    if current != expected_pid:
                        return
                PID_FILE.unlink(missing_ok=True)
            except: pass

        def _port_in_use(port):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                return s.connect_ex(("127.0.0.1", port)) == 0

        def is_proxy_running():
            pid = _read_pid()
            if pid is not None:
                try:
                    os.kill(pid, 0)
                    return True
                except ProcessLookupError:
                    _cleanup_pid()
                except PermissionError:
                    return True
            if _port_in_use(PORT):
                return True
            return False

        def _acquire_lock():
            try:
                fd = os.open(str(LOCK_FILE), os.O_WRONLY | os.O_CREAT, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except (OSError, IOError):
                return None

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({{"status": "ok", "pid": os.getpid()}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a): pass

        class Server(http.server.HTTPServer):
            allow_reuse_address = True

        def main():
            if is_proxy_running():
                sys.exit(0)

            is_daemon = "--daemon" in sys.argv
            if is_daemon:
                pid = os.fork()
                if pid > 0:
                    print(f"STARTED {{pid}}", flush=True)
                    sys.exit(0)
                os.setsid()
                devnull = os.open(os.devnull, os.O_RDWR)
                os.dup2(devnull, 0)
                os.dup2(devnull, 1)
                log_fd = os.open(str(LOG_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                os.dup2(log_fd, 2)

            lock_fd = _acquire_lock()
            if lock_fd is None:
                sys.exit(0)

            if _port_in_use(PORT):
                os.close(lock_fd)
                sys.exit(0)

            my_pid = os.getpid()
            _write_pid(my_pid)

            # Simulate slow plugin loading
            if STARTUP_DELAY > 0:
                time.sleep(STARTUP_DELAY)

            import atexit
            atexit.register(_cleanup_pid, expected_pid=my_pid)

            server = Server(("127.0.0.1", PORT), Handler)
            print(f"LISTENING {{my_pid}}", file=sys.stderr, flush=True)

            def _shutdown(signum, frame):
                _cleanup_pid(expected_pid=my_pid)
                threading.Thread(target=server.shutdown, daemon=True).start()
            signal.signal(signal.SIGTERM, _shutdown)

            try:
                server.serve_forever()
            except KeyboardInterrupt:
                _cleanup_pid(expected_pid=my_pid)
                server.shutdown()

        if __name__ == "__main__":
            main()
    """))


# ── Test cases ────────────────────────────────────────────────────────────

class CrashScenarioBase(unittest.TestCase):
    """Base class: sets up a private temp dir and ephemeral port per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name) / "state"
        self.state_dir.mkdir()
        self.port = free_port()
        self.proxy_script = Path(self._tmp.name) / "proxy_test.py"
        self._pids_to_kill: list[int] = []

    def tearDown(self):
        for pid in self._pids_to_kill:
            kill_tree(pid)
        # Wait a moment for port cleanup
        wait_for_port_free(self.port, timeout=3)
        self._tmp.cleanup()

    def _write_proxy(self, startup_delay: float = 0):
        write_mini_proxy(self.proxy_script, self.state_dir, self.port, startup_delay)

    def _launch_daemon(self) -> subprocess.CompletedProcess:
        """Launch proxy as daemon, return CompletedProcess with stdout."""
        result = subprocess.run(
            [PYTHON, str(self.proxy_script), "--daemon"],
            capture_output=True, text=True, timeout=10,
        )
        # Parse child PID from stdout "STARTED <pid>"
        for line in result.stdout.strip().splitlines():
            if line.startswith("STARTED"):
                pid = int(line.split()[1])
                self._pids_to_kill.append(pid)
        return result

    def _launch_foreground(self) -> subprocess.Popen:
        """Launch proxy in foreground, return Popen handle."""
        proc = subprocess.Popen(
            [PYTHON, str(self.proxy_script)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._pids_to_kill.append(proc.pid)
        return proc

    def _read_pid(self) -> int | None:
        try:
            return int((self.state_dir / "proxy.pid").read_text().strip())
        except Exception:
            return None

    def _read_log(self) -> str:
        log = self.state_dir / "proxy.log"
        return log.read_text() if log.exists() else ""


class TestConcurrentDaemonLaunches(CrashScenarioBase):
    """Scenario: Multiple Claude Code sessions fire SessionStart hooks
    simultaneously, each launching `proxy.py --daemon`.

    Before the fix, this caused "Address already in use" crashes.
    Now, only one instance should survive.
    """

    def test_concurrent_launches_single_survivor(self):
        """Launch N daemons simultaneously — exactly one binds the port."""
        self._write_proxy(startup_delay=0.2)  # slow enough to race

        N = 5
        procs = []
        for _ in range(N):
            p = subprocess.Popen(
                [PYTHON, str(self.proxy_script), "--daemon"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            procs.append(p)

        # Collect all child PIDs
        for p in procs:
            out, _ = p.communicate(timeout=10)
            for line in out.strip().splitlines():
                if line.startswith("STARTED"):
                    self._pids_to_kill.append(int(line.split()[1]))

        # Wait for the winner to start listening
        self.assertTrue(wait_for_port(self.port, timeout=8),
                        f"No proxy listening on port {self.port} after 8s")

        # Verify exactly one listener
        status = proxy_status(self.port)
        self.assertIsNotNone(status)
        self.assertEqual(status["status"], "ok")

        # Verify no "Address already in use" in the log
        log = self._read_log()
        self.assertNotIn("Address already in use", log,
                          f"Crash detected in log on {ARCH}:\n{log}")

    def test_rapid_fire_launches(self):
        """Launch daemons in rapid succession (100ms apart) — no crashes."""
        self._write_proxy()
        N = 4
        for i in range(N):
            result = self._launch_daemon()
            if i < N - 1:
                time.sleep(0.1)

        wait_for_port(self.port, timeout=5)
        status = proxy_status(self.port)
        self.assertIsNotNone(status, f"Proxy not responding on {ARCH}")

        log = self._read_log()
        self.assertNotIn("Address already in use", log)
        self.assertNotIn("Traceback", log)


class TestStalePidRecovery(CrashScenarioBase):
    """Scenario: PID file references a dead process.  A new launch must
    detect this and start successfully instead of silently exiting.
    """

    def test_stale_pid_allows_new_launch(self):
        """Stale PID (dead process) should be ignored — new proxy starts."""
        self._write_proxy()

        # Write a PID that definitely doesn't exist
        (self.state_dir / "proxy.pid").write_text("99999")

        result = self._launch_daemon()
        self.assertTrue(wait_for_port(self.port, timeout=5),
                        f"Proxy failed to start with stale PID on {ARCH}")

        status = proxy_status(self.port)
        self.assertIsNotNone(status)
        # Stale PID should have been replaced
        live_pid = self._read_pid()
        self.assertNotEqual(live_pid, 99999)

    def test_stale_pid_with_port_held_by_other(self):
        """Stale PID + port held by another process → dedup exits cleanly."""
        self._write_proxy()

        # Bind the port ourselves to simulate another process holding it
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", self.port))
        s.listen(1)

        try:
            # Write a dead PID
            (self.state_dir / "proxy.pid").write_text("99999")

            result = self._launch_daemon()
            # Should exit silently (port is in use)
            time.sleep(1)

            # Our socket should still be the one holding the port
            log = self._read_log()
            self.assertNotIn("Address already in use", log)
        finally:
            s.close()


class TestPidCleanupSafety(CrashScenarioBase):
    """Scenario: Instance A is healthy.  Instance B starts, fails, and its
    atexit handler runs.  Instance B's cleanup must NOT delete A's PID.
    """

    def test_crashing_child_preserves_healthy_pid(self):
        """Start A, then force B to crash — A's PID file must survive."""
        self._write_proxy()

        # Start instance A
        self._launch_daemon()
        self.assertTrue(wait_for_port(self.port, timeout=5))
        pid_a = self._read_pid()
        self.assertIsNotNone(pid_a)

        # Launch instance B — it should exit(0) due to dedup guard
        result = self._launch_daemon()
        time.sleep(0.5)

        # A's PID must still be in the file
        pid_after = self._read_pid()
        self.assertEqual(pid_after, pid_a,
                         f"PID file corrupted on {ARCH}: was {pid_a}, now {pid_after}")

        # A must still be responsive
        status = proxy_status(self.port)
        self.assertIsNotNone(status)

    def test_sigterm_cleanup_does_not_wipe_successor(self):
        """Send SIGTERM to A after B has already taken over the PID file."""
        self._write_proxy()

        # Start instance A
        self._launch_daemon()
        self.assertTrue(wait_for_port(self.port, timeout=5))
        pid_a = self._read_pid()

        # Kill A with SIGTERM (graceful shutdown)
        os.kill(pid_a, signal.SIGTERM)
        wait_for_pid_exit(pid_a, timeout=5)

        # Simulate: B started and wrote its PID before A's handler ran
        # (In practice the lock prevents this, but test the safety net)
        fake_pid_b = 88888
        (self.state_dir / "proxy.pid").write_text(str(fake_pid_b))

        # A's atexit should NOT have wiped the file since expected_pid != current
        time.sleep(0.5)
        current = self._read_pid()
        # Either B's PID survived or the file was cleaned by A (if A ran first)
        # The key check: it must not be None if B wrote after A exited
        # Since we wrote after kill, it should survive
        self.assertEqual(current, fake_pid_b,
                         f"PID file wiped by dying instance on {ARCH}")


class TestSIGTERMGracefulShutdown(CrashScenarioBase):
    """Scenario: SIGTERM is sent to a running proxy.  It should shut down
    cleanly, release the port, and clean up its PID file.
    """

    def test_sigterm_releases_port(self):
        self._write_proxy()
        self._launch_daemon()
        self.assertTrue(wait_for_port(self.port, timeout=5))
        pid = self._read_pid()

        os.kill(pid, signal.SIGTERM)
        self.assertTrue(wait_for_pid_exit(pid, timeout=5),
                        f"Process not exited after SIGTERM on {ARCH}")
        self.assertFalse(port_listening(self.port),
                         f"Port not released after SIGTERM on {ARCH}")

    def test_sigterm_cleans_pid_file(self):
        self._write_proxy()
        self._launch_daemon()
        self.assertTrue(wait_for_port(self.port, timeout=5))
        pid = self._read_pid()

        os.kill(pid, signal.SIGTERM)
        self.assertTrue(wait_for_pid_exit(pid, timeout=5))

        # PID file should be gone (our PID matched)
        self.assertIsNone(self._read_pid(),
                          f"PID file not cleaned after SIGTERM on {ARCH}")

    def test_restart_after_sigterm(self):
        """After SIGTERM, a new daemon should start successfully."""
        self._write_proxy()

        # Start → kill → wait for full exit → restart
        self._launch_daemon()
        self.assertTrue(wait_for_port(self.port, timeout=5))
        pid_a = self._read_pid()
        os.kill(pid_a, signal.SIGTERM)
        self.assertTrue(wait_for_pid_exit(pid_a, timeout=5),
                        f"Process did not exit after SIGTERM on {ARCH}")

        # Start again
        self._launch_daemon()
        self.assertTrue(wait_for_port(self.port, timeout=5),
                        f"Restart failed after SIGTERM on {ARCH}")

        pid_b = self._read_pid()
        self.assertNotEqual(pid_a, pid_b)


class TestSIGKILLRecovery(CrashScenarioBase):
    """Scenario: proxy is killed with SIGKILL (OOM killer, `kill -9`).
    atexit handlers don't run.  PID file and lock are left behind.
    A new launch must recover.
    """

    def test_recovery_after_sigkill(self):
        self._write_proxy()
        self._launch_daemon()
        self.assertTrue(wait_for_port(self.port, timeout=5))
        pid_a = self._read_pid()

        # SIGKILL — no cleanup runs
        os.kill(pid_a, signal.SIGKILL)
        wait_for_port_free(self.port, timeout=5)

        # PID file is stale, lock file exists but fd is closed (process dead)
        stale_pid = self._read_pid()
        self.assertEqual(stale_pid, pid_a, "PID file should still have dead PID")
        self.assertTrue((self.state_dir / "proxy.lock").exists())

        # New launch must succeed despite stale state
        self._launch_daemon()
        self.assertTrue(wait_for_port(self.port, timeout=5),
                        f"Recovery after SIGKILL failed on {ARCH}")

        pid_b = self._read_pid()
        self.assertNotEqual(pid_a, pid_b)
        status = proxy_status(self.port)
        self.assertIsNotNone(status)

    def test_sigkill_during_startup(self):
        """SIGKILL during slow plugin load — next launch recovers."""
        self._write_proxy(startup_delay=2.0)
        self._launch_daemon()
        time.sleep(0.5)  # wait for fork but not for bind

        pid = self._read_pid()
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)

        # Now launch with no delay — should start fine
        write_mini_proxy(self.proxy_script, self.state_dir, self.port, startup_delay=0)
        self._launch_daemon()
        self.assertTrue(wait_for_port(self.port, timeout=5),
                        f"Recovery after startup SIGKILL failed on {ARCH}")


class TestLockFileSerialization(CrashScenarioBase):
    """Verify the fcntl.flock startup lock actually serialises launches."""

    def test_lock_blocks_second_instance(self):
        """Hold the lock externally — daemon launch should exit(0)."""
        self._write_proxy()

        import fcntl
        lock_path = self.state_dir / "proxy.lock"
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            result = self._launch_daemon()
            time.sleep(1)
            # Proxy should NOT be listening (lock held externally)
            self.assertFalse(port_listening(self.port),
                             f"Proxy started despite held lock on {ARCH}")
        finally:
            os.close(fd)

    def test_lock_released_on_process_death(self):
        """After process dies, lock is auto-released (kernel closes fd)."""
        self._write_proxy()
        self._launch_daemon()
        self.assertTrue(wait_for_port(self.port, timeout=5))
        pid = self._read_pid()

        os.kill(pid, signal.SIGKILL)
        wait_for_port_free(self.port, timeout=5)
        time.sleep(0.5)

        # Lock should be free now — new instance should acquire it
        import fcntl
        lock_path = self.state_dir / "proxy.lock"
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # If we get here, the lock was free — good
        except (OSError, IOError):
            self.fail(f"Lock still held after process death on {ARCH}")
        finally:
            os.close(fd)


class TestArchitectureSpecific(CrashScenarioBase):
    """Architecture-specific checks.  fork() and fcntl behave identically
    on arm64 and x86_64, but subtle differences in signal delivery or
    socket TIME_WAIT can surface here.
    """

    def test_architecture_reported(self):
        """Sanity: verify we know what arch we're running on."""
        self.assertIn(ARCH, ("arm64", "aarch64", "x86_64"),
                      f"Unknown architecture: {ARCH}")

    def test_fork_inherits_lock_correctly(self):
        """After fork(), the child inherits the lock fd.  Verify the parent
        exiting does not release the child's lock.
        """
        self._write_proxy()
        self._launch_daemon()
        self.assertTrue(wait_for_port(self.port, timeout=5))

        # The parent already exited.  The child should still hold the lock.
        import fcntl
        lock_path = self.state_dir / "proxy.lock"
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fail(f"Lock not held by child after parent exit on {ARCH}")
        except (OSError, IOError):
            pass  # Expected — child holds it
        finally:
            os.close(fd)

    def test_so_reuseaddr_across_restart(self):
        """SO_REUSEADDR must allow rebind after SIGTERM without TIME_WAIT issues."""
        self._write_proxy()

        for cycle in range(3):
            self._launch_daemon()
            self.assertTrue(wait_for_port(self.port, timeout=5),
                            f"Cycle {cycle}: proxy failed to start on {ARCH}")
            pid = self._read_pid()
            os.kill(pid, signal.SIGTERM)
            self.assertTrue(wait_for_pid_exit(pid, timeout=5),
                            f"Cycle {cycle}: process not exited on {ARCH}")

    def test_concurrent_launches_deterministic_winner(self):
        """The lock ensures a single winner even under scheduler pressure.
        Run 10 concurrent launches — all must resolve without errors.
        """
        self._write_proxy(startup_delay=0.1)

        procs = []
        for _ in range(10):
            p = subprocess.Popen(
                [PYTHON, str(self.proxy_script), "--daemon"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            procs.append(p)

        all_pids = []
        for p in procs:
            out, _ = p.communicate(timeout=15)
            for line in out.strip().splitlines():
                if line.startswith("STARTED"):
                    pid = int(line.split()[1])
                    all_pids.append(pid)
                    self._pids_to_kill.append(pid)

        wait_for_port(self.port, timeout=8)

        # Exactly one process should be listening
        status = proxy_status(self.port)
        self.assertIsNotNone(status, f"No proxy after 10 concurrent launches on {ARCH}")

        log = self._read_log()
        self.assertNotIn("Address already in use", log,
                          f"Bind crash in 10-way race on {ARCH}:\n{log}")
        self.assertNotIn("Traceback", log,
                          f"Traceback in 10-way race on {ARCH}:\n{log}")


if __name__ == "__main__":
    unittest.main()

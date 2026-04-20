"""Tests for the task-watcher plugin."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest import mock

import pytest

# Import with a fresh module each test to avoid shared state
import importlib
import plugins.task_watcher as tw


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_module_state(tmp_path):
    """Reset plugin state and point DB to a temp directory."""
    tw._known_ids.clear()
    tw._completed_results.clear()
    tw._control_api_url = ""
    tw._control_api_key = ""
    tw._check_timeout = 2
    tw._task_ttl = 86400
    tw._db_path = tmp_path / "task_watcher.db"
    tw._init_db()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "task_watcher.db"


# ── Helpers ───────────────────────────────────────────────────────────────

SAMPLE_TASK_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
SAMPLE_TASK_ID_2 = "11111111-2222-3333-4444-555555555555"


def _make_payload_with_dispatch(task_id: str, tool_name: str = "mcp__worker-mcp__dispatch_task") -> dict:
    """Build a minimal Anthropic API payload containing a dispatch tool_use + tool_result."""
    return {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_abc123",
                        "name": tool_name,
                        "input": {"specialization": "test", "task_description": "do stuff"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc123",
                        "content": f"Task submitted. task_id: {task_id}",
                    }
                ],
            },
        ],
    }


def _make_payload_no_dispatch() -> dict:
    """Build a payload with no dispatch tool calls."""
    return {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {"role": "user", "content": "Hello, how are you?"},
        ],
    }


# ── plugin_info / configure ──────────────────────────────────────────────


def test_plugin_info():
    info = tw.plugin_info()
    assert info["name"] == "task-watcher"
    assert "version" in info


def test_configure_sets_api_url(tmp_path):
    tw.configure({
        "control_api_url": "http://localhost:9999",
        "control_api_key": "test-key",
        "db_path": str(tmp_path / "test.db"),
    })
    assert tw._control_api_url == "http://localhost:9999"
    assert tw._control_api_key == "test-key"


def test_configure_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_API_URL", "http://env-url:8000")
    monkeypatch.setenv("CONTROL_API_KEY", "env-key")
    tw.configure({"db_path": str(tmp_path / "test.db")})
    assert tw._control_api_url == "http://env-url:8000"
    assert tw._control_api_key == "env-key"


# ── Task ID extraction ────────────────────────────────────────────────────


def test_extract_dispatch_task_id():
    payload = _make_payload_with_dispatch(SAMPLE_TASK_ID)
    found = tw._extract_new_task_ids(payload)
    assert len(found) == 1
    assert found[0] == (SAMPLE_TASK_ID, "mcp__worker-mcp__dispatch_task")


def test_extract_run_assistant_task_id():
    payload = _make_payload_with_dispatch(
        SAMPLE_TASK_ID, tool_name="mcp__worker-mcp__run_assistant"
    )
    found = tw._extract_new_task_ids(payload)
    assert len(found) == 1
    assert found[0] == (SAMPLE_TASK_ID, "mcp__worker-mcp__run_assistant")


def test_extract_skips_known_ids():
    tw._known_ids.add(SAMPLE_TASK_ID)
    payload = _make_payload_with_dispatch(SAMPLE_TASK_ID)
    found = tw._extract_new_task_ids(payload)
    assert found == []


def test_extract_no_dispatch_tools():
    payload = _make_payload_no_dispatch()
    found = tw._extract_new_task_ids(payload)
    assert found == []


def test_extract_ignores_non_dispatch_tools():
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_xyz",
                        "name": "mcp__worker-mcp__list_workers",
                        "input": {},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_xyz",
                        "content": SAMPLE_TASK_ID,
                    }
                ],
            },
        ],
    }
    found = tw._extract_new_task_ids(payload)
    assert found == []


def test_extract_handles_list_content_in_tool_result():
    """tool_result content can be a list of content blocks."""
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_list",
                        "name": "mcp__worker-mcp__dispatch_task",
                        "input": {},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_list",
                        "content": [
                            {"type": "text", "text": f"Dispatched: {SAMPLE_TASK_ID}"}
                        ],
                    }
                ],
            },
        ],
    }
    found = tw._extract_new_task_ids(payload)
    assert len(found) == 1
    assert found[0][0] == SAMPLE_TASK_ID


# ── SQLite persistence ────────────────────────────────────────────────────


def test_register_and_get_pending():
    tw._register_task(SAMPLE_TASK_ID, "dispatch_task")
    pending = tw._get_pending()
    assert len(pending) == 1
    assert pending[0][0] == SAMPLE_TASK_ID


def test_register_deduplicates():
    tw._register_task(SAMPLE_TASK_ID, "dispatch_task")
    tw._register_task(SAMPLE_TASK_ID, "dispatch_task")
    pending = tw._get_pending()
    assert len(pending) == 1


def test_mark_done_removes_from_pending():
    tw._register_task(SAMPLE_TASK_ID, "dispatch_task")
    tw._mark_done(SAMPLE_TASK_ID, "completed")
    pending = tw._get_pending()
    assert len(pending) == 0


def test_load_known_ids_on_restart(tmp_path):
    """Simulate restart: IDs from DB should be loaded into _known_ids."""
    tw._register_task(SAMPLE_TASK_ID, "test")
    tw._known_ids.clear()
    tw._load_known_ids()
    assert SAMPLE_TASK_ID in tw._known_ids


# ── on_outbound: interception ─────────────────────────────────────────────


def test_on_outbound_registers_new_task():
    payload = _make_payload_with_dispatch(SAMPLE_TASK_ID)
    result = tw.on_outbound(payload)
    assert result is None  # never modifies payload
    assert SAMPLE_TASK_ID in tw._known_ids
    assert len(tw._get_pending()) == 1


def test_on_outbound_no_dispatch_is_noop():
    payload = _make_payload_no_dispatch()
    tw.on_outbound(payload)
    assert len(tw._get_pending()) == 0


# ── on_outbound: status checking ──────────────────────────────────────────


def test_on_outbound_detects_completed_task():
    tw._control_api_url = "http://fake:8000"
    tw._register_task(SAMPLE_TASK_ID, "dispatch_task")

    status_response = {
        "task_id": SAMPLE_TASK_ID,
        "found": True,
        "history": {"status": "COMPLETED", "description": "test task"},
        "offline_queue": None,
    }

    result_response = {
        "summary": "Task finished successfully. Output: hello world.",
        "total_cost_usd": 0.0012,
        "duration_seconds": 3.5,
    }

    def mock_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        body = (
            json.dumps(result_response).encode()
            if "/result" in url
            else json.dumps(status_response).encode()
        )
        resp = mock.MagicMock()
        resp.read.return_value = body
        return resp

    with mock.patch("plugins.task_watcher.urllib.request.urlopen", side_effect=mock_urlopen):
        payload = _make_payload_no_dispatch()
        tw.on_outbound(payload)

    assert len(tw._completed_results) == 1
    assert "completed" in tw._completed_results[0].lower()
    assert "hello world" in tw._completed_results[0]


def test_on_outbound_skips_running_task():
    tw._control_api_url = "http://fake:8000"
    tw._register_task(SAMPLE_TASK_ID, "dispatch_task")

    status_response = {
        "task_id": SAMPLE_TASK_ID,
        "found": True,
        "history": {"status": "RUNNING"},
    }

    def mock_urlopen(req, timeout=None):
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(status_response).encode()
        return resp

    with mock.patch("plugins.task_watcher.urllib.request.urlopen", side_effect=mock_urlopen):
        tw.on_outbound(_make_payload_no_dispatch())

    assert tw._completed_results == []
    assert len(tw._get_pending()) == 1  # still pending


def test_on_outbound_handles_failed_task():
    tw._control_api_url = "http://fake:8000"
    tw._register_task(SAMPLE_TASK_ID, "dispatch_task")

    status_response = {
        "task_id": SAMPLE_TASK_ID,
        "found": True,
        "history": {"status": "FAILED", "description": "broken task"},
    }

    def mock_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if "/result" in url:
            raise urllib.error.URLError("not found")
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(status_response).encode()
        return resp

    import urllib.error

    with mock.patch("plugins.task_watcher.urllib.request.urlopen", side_effect=mock_urlopen):
        tw.on_outbound(_make_payload_no_dispatch())

    assert len(tw._completed_results) == 1
    assert "failed" in tw._completed_results[0].lower()
    assert len(tw._get_pending()) == 0  # removed from pending


def test_on_outbound_expires_stale_tasks():
    tw._control_api_url = "http://fake:8000"
    tw._task_ttl = 10  # 10 seconds

    # Register task with old timestamp
    tw._known_ids.add(SAMPLE_TASK_ID)
    conn = sqlite3.connect(str(tw._db_path))
    conn.execute(
        "INSERT INTO pending_tasks (task_id, registered_at, tool_name, status) "
        "VALUES (?, ?, ?, 'pending')",
        (SAMPLE_TASK_ID, time.time() - 20, "test"),
    )
    conn.commit()
    conn.close()

    tw.on_outbound(_make_payload_no_dispatch())
    assert len(tw._get_pending()) == 0


def test_on_outbound_tolerates_api_unreachable():
    tw._control_api_url = "http://fake:8000"
    tw._register_task(SAMPLE_TASK_ID, "dispatch_task")

    with mock.patch(
        "plugins.task_watcher.urllib.request.urlopen",
        side_effect=Exception("connection refused"),
    ):
        tw.on_outbound(_make_payload_no_dispatch())

    # Task should remain pending (retry next cycle)
    assert len(tw._get_pending()) == 1
    assert tw._completed_results == []


# ── on_inbound: injection ─────────────────────────────────────────────────


def test_on_inbound_returns_none_when_empty():
    assert tw.on_inbound("some response", {}) is None


def test_on_inbound_returns_and_clears_results():
    tw._completed_results = ["Result A", "Result B"]
    injection = tw.on_inbound("some response", {})
    assert "Result A" in injection
    assert "Result B" in injection
    assert tw._completed_results == []


# ── End-to-end: intercept → check → inject ───────────────────────────────


def test_e2e_intercept_check_inject():
    """Full cycle: dispatch payload → status check → injection."""
    tw._control_api_url = "http://fake:8000"

    # Cycle 1: intercept the task ID
    payload1 = _make_payload_with_dispatch(SAMPLE_TASK_ID)
    tw.on_outbound(payload1)
    assert SAMPLE_TASK_ID in tw._known_ids
    # No results yet (task just dispatched, status check returns nothing useful)

    # Cycle 2: task has completed
    status_response = {
        "task_id": SAMPLE_TASK_ID,
        "found": True,
        "history": {"status": "COMPLETED", "description": "e2e test"},
    }
    result_response = {"summary": "All done!", "total_cost_usd": 0.001}

    def mock_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        body = (
            json.dumps(result_response).encode()
            if "/result" in url
            else json.dumps(status_response).encode()
        )
        resp = mock.MagicMock()
        resp.read.return_value = body
        return resp

    with mock.patch("plugins.task_watcher.urllib.request.urlopen", side_effect=mock_urlopen):
        payload2 = _make_payload_no_dispatch()
        tw.on_outbound(payload2)

    # on_inbound should inject the result
    injection = tw.on_inbound("Claude says hello", {})
    assert injection is not None
    assert "All done!" in injection
    assert SAMPLE_TASK_ID in injection

    # Task is no longer pending
    assert len(tw._get_pending()) == 0

    # Subsequent on_inbound should return None
    assert tw.on_inbound("", {}) is None


def test_e2e_same_id_not_reregistered():
    """Once a task ID is seen, subsequent payloads with the same ID are ignored."""
    tw._control_api_url = "http://fake:8000"

    payload = _make_payload_with_dispatch(SAMPLE_TASK_ID)
    tw.on_outbound(payload)
    assert len(tw._get_pending()) == 1

    # Mark done
    tw._mark_done(SAMPLE_TASK_ID, "completed")

    # Same payload again — should NOT re-register
    tw._completed_results = []
    tw.on_outbound(payload)
    assert len(tw._get_pending()) == 0  # still zero

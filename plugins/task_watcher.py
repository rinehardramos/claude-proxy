"""Async task result watcher plugin for claude-proxy.

Intercepts task IDs from MCP tool results (run_assistant, dispatch_task),
polls the control-plane API for completion on each request cycle, and
injects finished results into Claude's response stream.

Flow:
  1. on_outbound(): scan messages for new task IDs, save to local SQLite.
     Also check all pending tasks for completion via control API.
  2. on_inbound(): inject any completed results as new content blocks.

Stdlib only: json, os, re, sqlite3, sys, time, urllib.request.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

# ── Module state ──────────────────────────────────────────────────────────

_control_api_url: str = ""
_control_api_key: str = ""
_check_timeout: int = 5
_task_ttl: int = 86400  # stop checking tasks older than 24h
_db_path: Path = Path("~/.claude/claude-proxy/task_watcher.db").expanduser()

# Results ready for injection (populated in on_outbound, consumed in on_inbound)
_completed_results: list[str] = []

# In-memory dedup: all task IDs ever seen this session (avoids re-scanning
# the full conversation history on every request).
_known_ids: set[str] = set()

# Tool names whose tool_result may contain a dispatched task ID.
_DISPATCH_TOOLS: frozenset[str] = frozenset({
    "mcp__worker-mcp__run_assistant",
    "mcp__worker-mcp__dispatch_task",
})

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# Statuses that mean "done — stop polling".
_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "COMPLETED", "DONE", "SUCCESS", "FAILED", "ERROR", "CANCELLED", "TIMED_OUT",
})


# ── Plugin interface ──────────────────────────────────────────────────────

def plugin_info() -> dict:
    return {
        "name": "task-watcher",
        "version": "0.1.0",
        "description": "Async task result injector via control-plane polling",
    }


def configure(config: dict) -> None:
    """Read control-plane connection details from plugin config / env vars."""
    global _control_api_url, _control_api_key, _check_timeout, _task_ttl, _db_path

    _control_api_url = (
        config.get("control_api_url")
        or os.environ.get("CONTROL_API_URL", "")
    )
    _control_api_key = (
        config.get("control_api_key")
        or os.environ.get("CONTROL_API_KEY", "")
    )
    _check_timeout = int(config.get("check_timeout", "5"))
    _task_ttl = int(config.get("task_ttl", "86400"))

    db = config.get("db_path")
    if db:
        _db_path = Path(db).expanduser()

    _init_db()
    _load_known_ids()

    if not _control_api_url:
        print(
            "[task-watcher] WARNING: control_api_url not set — "
            "status checks disabled until configured",
            file=sys.stderr,
        )


# ── SQLite persistence ────────────────────────────────────────────────────

def _init_db() -> None:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pending_tasks ("
        "  task_id       TEXT PRIMARY KEY,"
        "  registered_at REAL NOT NULL,"
        "  tool_name     TEXT,"
        "  status        TEXT DEFAULT 'pending'"
        ")"
    )
    conn.commit()
    conn.close()


def _load_known_ids() -> None:
    try:
        conn = sqlite3.connect(str(_db_path))
        rows = conn.execute("SELECT task_id FROM pending_tasks").fetchall()
        _known_ids.update(r[0] for r in rows)
        conn.close()
    except Exception:
        pass


def _register_task(task_id: str, tool_name: str = "unknown") -> None:
    if task_id in _known_ids:
        return
    _known_ids.add(task_id)
    try:
        conn = sqlite3.connect(str(_db_path))
        conn.execute(
            "INSERT OR IGNORE INTO pending_tasks (task_id, registered_at, tool_name) "
            "VALUES (?, ?, ?)",
            (task_id, time.time(), tool_name),
        )
        conn.commit()
        conn.close()
        print(f"[task-watcher] registered {task_id} ({tool_name})", file=sys.stderr)
    except Exception as exc:
        print(f"[task-watcher] register error: {exc}", file=sys.stderr)


def _get_pending() -> list[tuple[str, str, float]]:
    """Return [(task_id, tool_name, registered_at), ...] for pending tasks."""
    try:
        conn = sqlite3.connect(str(_db_path))
        rows = conn.execute(
            "SELECT task_id, tool_name, registered_at "
            "FROM pending_tasks WHERE status = 'pending'"
        ).fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def _mark_done(task_id: str, status: str = "completed") -> None:
    try:
        conn = sqlite3.connect(str(_db_path))
        conn.execute(
            "UPDATE pending_tasks SET status = ? WHERE task_id = ?",
            (status, task_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── Control-plane HTTP helpers ────────────────────────────────────────────

def _api_get(path: str, timeout: int | None = None) -> dict | None:
    """GET a JSON endpoint on the control API. Returns parsed dict or None."""
    if not _control_api_url:
        return None
    url = _control_api_url.rstrip("/") + path
    req = urllib.request.Request(url, method="GET")
    if _control_api_key:
        req.add_header("X-Control-API-Key", _control_api_key)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout or _check_timeout)
        return json.loads(resp.read())
    except Exception:
        return None


def _check_task_status(task_id: str) -> tuple[str | None, dict | None]:
    """Check task status. Returns (status_str, full_response) or (None, None)."""
    data = _api_get(f"/tasks/{task_id}")
    if not data or not data.get("found"):
        return None, None

    history = data.get("history") or {}
    status = (history.get("status") or "").upper()
    return status, data


def _fetch_task_result(task_id: str) -> dict | None:
    """Fetch completed task result from /tasks/{task_id}/result.

    This endpoint may not exist yet — returns None gracefully.
    """
    return _api_get(f"/tasks/{task_id}/result", timeout=10)


# ── Message scanning ──────────────────────────────────────────────────────

def _extract_new_task_ids(payload: dict) -> list[tuple[str, str]]:
    """Scan outbound messages for task IDs from dispatch tool results.

    Returns [(task_id, tool_name), ...] for IDs not yet in _known_ids.
    """
    messages = payload.get("messages", [])

    # 1. Build map: tool_use_id -> tool_name (for dispatch tools only)
    dispatch_uses: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if (
                block.get("type") == "tool_use"
                and block.get("name") in _DISPATCH_TOOLS
            ):
                use_id = block.get("id")
                if use_id:
                    dispatch_uses[use_id] = block["name"]

    if not dispatch_uses:
        return []

    # 2. Find tool_result blocks that reference a dispatch tool_use
    found: list[tuple[str, str]] = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            use_id = block.get("tool_use_id")
            if use_id not in dispatch_uses:
                continue

            tool_name = dispatch_uses[use_id]

            # Extract text from the result content
            rc = block.get("content", "")
            if isinstance(rc, list):
                rc = " ".join(
                    b.get("text", "")
                    for b in rc
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            rc = str(rc)

            # Pull UUID-shaped task IDs
            for m in _UUID_RE.finditer(rc):
                tid = m.group(0)
                if tid not in _known_ids:
                    found.append((tid, tool_name))

    return found


# ── Plugin hooks ──────────────────────────────────────────────────────────

def on_outbound(payload: dict) -> dict | None:
    """Intercept task IDs and poll pending tasks for completion.

    Called before every request to Anthropic.  Adds minimal latency
    (one HTTP GET per pending task, with a short timeout).
    """
    global _completed_results
    _completed_results = []

    # ── 1. Intercept new task IDs from conversation history ──
    for task_id, tool_name in _extract_new_task_ids(payload):
        _register_task(task_id, tool_name)

    # ── 2. Check all pending tasks ──
    now = time.time()
    for task_id, tool_name, registered_at in _get_pending():
        # Expire stale tasks
        if now - registered_at > _task_ttl:
            _mark_done(task_id, "expired")
            print(f"[task-watcher] expired stale task {task_id}", file=sys.stderr)
            continue

        status, status_data = _check_task_status(task_id)
        if status is None:
            continue  # API unreachable or task not found — retry next cycle

        if status not in _TERMINAL_STATUSES:
            continue  # still running

        # ── Task reached a terminal state ──
        if status in ("COMPLETED", "DONE", "SUCCESS"):
            result_text = _build_success_text(task_id, status_data)
            _completed_results.append(result_text)
            _mark_done(task_id, "completed")
            print(
                f"[task-watcher] task {task_id} completed, queued for injection",
                file=sys.stderr,
            )
        else:
            # FAILED / ERROR / CANCELLED / TIMED_OUT
            result_text = _build_failure_text(task_id, status, status_data)
            _completed_results.append(result_text)
            _mark_done(task_id, status.lower())
            print(
                f"[task-watcher] task {task_id} {status.lower()}",
                file=sys.stderr,
            )

    return None  # never modify the request payload


def on_inbound(response_text: str, request_summary: dict) -> str | None:
    """Inject completed task results into Claude's response.

    Returns combined result text, or None if nothing to inject.
    """
    if not _completed_results:
        return None

    injection = "\n\n".join(_completed_results)
    _completed_results.clear()
    return injection


# ── Result formatting ─────────────────────────────────────────────────────

def _build_success_text(task_id: str, status_data: dict | None) -> str:
    """Format a completed task result for injection."""
    # Try the dedicated result endpoint first
    result = _fetch_task_result(task_id)

    if result:
        summary = (
            result.get("summary")
            or result.get("result")
            or result.get("output")
            or ""
        )
        cost = result.get("total_cost_usd")
        duration = result.get("duration_seconds")

        parts = [f"[Async task {task_id} completed]"]
        if summary:
            parts.append(str(summary))
        if cost is not None:
            parts.append(f"Cost: ${cost:.4f}")
        if duration is not None:
            parts.append(f"Duration: {duration:.1f}s")
        return "\n".join(parts)

    # Fallback: no result endpoint — use whatever metadata we have
    desc = ""
    if status_data:
        history = status_data.get("history") or {}
        desc = history.get("description", "")

    parts = [f"[Async task {task_id} completed]"]
    if desc:
        parts.append(f"Task: {desc[:200]}")
    parts.append(
        "Note: full result not available via API. "
        "Use get_task_status for details or add /tasks/<id>/result endpoint."
    )
    return "\n".join(parts)


def _build_failure_text(
    task_id: str, status: str, status_data: dict | None
) -> str:
    """Format a failed/cancelled task for injection."""
    parts = [f"[Async task {task_id} {status.lower()}]"]

    if status_data:
        history = status_data.get("history") or {}
        desc = history.get("description", "")
        if desc:
            parts.append(f"Task: {desc[:200]}")

    # Try result endpoint for error details
    result = _fetch_task_result(task_id)
    if result:
        error = result.get("error") or result.get("reason") or result.get("summary")
        if error:
            parts.append(f"Details: {error}")

    return "\n".join(parts)

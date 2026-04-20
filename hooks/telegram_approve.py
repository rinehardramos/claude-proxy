#!/usr/bin/env python3
"""PreToolUse hook: send tool-call approval prompts to Telegram.

Claude Code calls this script before executing a tool. The script:
  1. Reads tool details from stdin (JSON).
  2. Optionally runs a scanner to check if the call is flagged.
  3. If flagged (or always-ask mode): sends a Telegram message with
     Approve / Deny inline buttons and polls for the user's decision.
  4. Returns a permissionDecision JSON on stdout.

Standalone — stdlib only, no imports from the proxy or plugin.

Config is read from ~/.claude/claude-proxy/plugins/telegram.toml
(same file the telegram plugin uses).

Register in ~/.claude/settings.json:
  {
    "hooks": {
      "PreToolUse": [{
        "matcher": "Bash|Write|Edit",
        "hooks": [{
          "type": "command",
          "command": "python3 ~/.claude/claude-proxy/hooks/telegram_approve.py",
          "timeout": 180
        }]
      }]
    }
  }
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import uuid
from html import escape as _esc
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────

HOOK_DIR = Path("~/.claude/claude-proxy/telegram-hook").expanduser()
PENDING_DIR = HOOK_DIR / "pending"
DECIDED_DIR = HOOK_DIR / "decided"
CONFIG_PATH = Path("~/.claude/claude-proxy/plugins/telegram.toml").expanduser()

# ── Minimal TOML parser (subset) ────────────────────────────────────────

def _parse_toml(text: str) -> dict:
    """Parse key = "value" lines, ignoring sections and comments."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"'):
            result[key] = raw[1:-1]
        elif raw.startswith("'") and raw.endswith("'"):
            result[key] = raw[1:-1]
        else:
            result[key] = raw
    return result


def _load_config() -> dict:
    """Load bot_token, chat_id, and approval settings from telegram.toml."""
    if not CONFIG_PATH.exists():
        return {}
    return _parse_toml(CONFIG_PATH.read_text())


# ── Telegram helpers ─────────────────────────────────────────────────────

def _tg_post(token: str, method: str, payload: dict, timeout: int = 10) -> dict:
    """POST JSON to Telegram Bot API and return parsed response."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read())


def _expire_approval_message(token: str, chat_id: str, message_id: int) -> None:
    """Edit an approval message to show expired state."""
    expired_keyboard = {"inline_keyboard": [[
        {"text": "\u23f3 Expired", "callback_data": "noop:expired"},
    ]]}
    try:
        _tg_post(token, "editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": json.dumps(expired_keyboard),
        })
    except Exception:
        pass


def _send_approval_message(
    token: str, chat_id: str, decision_id: str,
    project: str, tool_name: str, tool_summary: str,
    findings: str | None = None,
) -> int | None:
    """Send an approval request with Approve/Deny inline buttons.

    Returns the sent message_id, or None on failure.
    """
    lines = [
        f"<b>{_esc(project)}</b>  —  <code>{_esc(tool_name)}</code>",
        "",
        f"<pre>{_esc(tool_summary[:800])}</pre>",
    ]
    if findings:
        lines.append(f"\n<b>Findings:</b>\n{_esc(findings[:500])}")

    text = "\n".join(lines)
    keyboard = {
        "inline_keyboard": [[
            {"text": "Approve", "callback_data": f"approve:{decision_id}"},
            {"text": "Deny", "callback_data": f"deny:{decision_id}"},
        ]]
    }
    try:
        result = _tg_post(token, "sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(keyboard),
        })
        return result.get("result", {}).get("message_id")
    except Exception as exc:
        _err(f"failed to send approval message: {exc}")
        return None


# ── Scanner ──────────────────────────────────────────────────────────────

def _run_scanner(tool_input: dict, scanner: str) -> str | None:
    """Run a scanner on tool input; return findings string or None if clean.

    Supported scanners:
      - "leak-guard": shell out to leak-guard scan
      - "always": always flag (every tool call needs approval)
      - "none": never flag (only send if another mechanism flags)
    """
    if scanner == "none":
        return None
    if scanner == "always":
        return "Manual approval required"

    if scanner == "leak-guard":
        # Extract the command or content to scan
        content = tool_input.get("command", "") or tool_input.get("content", "")
        if not content:
            return None
        try:
            proc = subprocess.run(
                ["python3", "-m", "leak_guard", "scan", "--stdin"],
                input=content, capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None

    return None


# ── Tool summary formatting ──────────────────────────────────────────────

def _format_tool_summary(tool_name: str, tool_input: dict) -> str:
    """Create a human-readable summary of the tool call."""
    if tool_name == "Bash":
        return tool_input.get("command", "(no command)")
    if tool_name in ("Write", "Edit"):
        path = tool_input.get("file_path", "")
        if tool_name == "Edit":
            old = tool_input.get("old_string", "")[:100]
            new = tool_input.get("new_string", "")[:100]
            return f"{path}\n-{old}\n+{new}"
        content = tool_input.get("content", "")
        return f"{path} ({len(content)} chars)"
    if tool_name == "Read":
        return tool_input.get("file_path", "(unknown)")
    # Generic: show first 200 chars of JSON
    return json.dumps(tool_input, indent=2)[:200]


# ── Main flow ────────────────────────────────────────────────────────────

def _output_decision(decision: str, reason: str = "") -> None:
    """Print permissionDecision JSON to stdout and exit."""
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        output["hookSpecificOutput"]["permissionDecisionReason"] = reason
    print(json.dumps(output))
    sys.exit(0)


def _err(msg: str) -> None:
    print(f"[telegram-hook] {msg}", file=sys.stderr)


def main() -> None:
    # Read hook input from stdin
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        _err("invalid JSON on stdin")
        _output_decision("ask", "telegram hook: failed to read input")

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    cwd = hook_input.get("cwd", "")
    session_id = hook_input.get("session_id", "")
    project = os.path.basename(cwd) if cwd else "(unknown)"

    # Load config
    config = _load_config()
    token = config.get("bot_token", "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = config.get("chat_id", "") or os.environ.get("TELEGRAM_CHAT_ID", "")
    timeout = int(config.get("approval_timeout", "600"))
    scanner = config.get("approval_scanner", "always")

    if not token or not chat_id:
        _err("no bot_token/chat_id — falling through to local prompt")
        _output_decision("ask", "telegram hook: not configured")

    # Check approval mode (set via /mode command in Telegram)
    mode_file = HOOK_DIR / "mode"
    approval_mode = "ask"
    if mode_file.exists():
        try:
            approval_mode = mode_file.read_text().strip()
        except OSError:
            pass

    if approval_mode == "auto-approve":
        _err("mode=auto-approve — passing through")
        sys.exit(0)  # no output = let Claude Code use built-in permissions
    elif approval_mode == "auto-deny":
        _err("mode=auto-deny — denying")
        _output_decision("deny", "telegram: auto-deny mode")

    # Run scanner
    findings = _run_scanner(tool_input, scanner)
    if findings is None and scanner != "always":
        # Nothing flagged, allow immediately
        _output_decision("allow")

    # Create decision request
    decision_id = uuid.uuid4().hex[:16]
    tool_summary = _format_tool_summary(tool_name, tool_input)

    # Ensure directories exist
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    DECIDED_DIR.mkdir(parents=True, exist_ok=True)

    # Send Telegram message with inline buttons
    message_id = _send_approval_message(
        token, chat_id, decision_id,
        project, tool_name, tool_summary, findings,
    )
    if message_id is None:
        _err("failed to send telegram message — falling through to local prompt")
        _output_decision("ask", "telegram hook: could not send message")

    # Write pending decision file (for the poller to track)
    pending_path = PENDING_DIR / f"{decision_id}.json"
    pending_path.write_text(json.dumps({
        "message_id": message_id,
        "project_cwd": cwd,
        "project": project,
        "tool_name": tool_name,
        "session_id": session_id,
        "findings": findings,
        "created_at": time.time(),
    }))

    # Poll for decision
    _err(f"waiting for Telegram decision ({timeout}s timeout)...")
    decided_path = DECIDED_DIR / f"{decision_id}.json"
    deadline = time.time() + timeout

    while time.time() < deadline:
        if decided_path.exists():
            try:
                result = json.loads(decided_path.read_text())
                decision = result.get("decision", "ask")
                # Clean up
                decided_path.unlink(missing_ok=True)
                pending_path.unlink(missing_ok=True)
                label = "approved" if decision == "allow" else "denied"
                _err(f"decision received: {label}")
                _output_decision(decision, f"Telegram user {label}")
            except (json.JSONDecodeError, OSError) as exc:
                _err(f"error reading decision: {exc}")
                break
        time.sleep(2)

    # Timeout — mark message expired, clean up, fall through to local prompt
    if message_id:
        _expire_approval_message(token, chat_id, message_id)
    pending_path.unlink(missing_ok=True)
    _err("timeout — falling through to local prompt")
    _output_decision("ask", "telegram hook: no response within timeout")


if __name__ == "__main__":
    main()

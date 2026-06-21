"""Shared helpers for Telegram PreToolUse hooks. Standalone — stdlib only."""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

HOOK_DIR = Path("~/.claude/claude-proxy/telegram-hook").expanduser()
PENDING_DIR = HOOK_DIR / "pending"
DECIDED_DIR = HOOK_DIR / "decided"
CONFIG_PATH = Path("~/.claude/claude-proxy/plugins/telegram.toml").expanduser()

AUTO_APPROVE_PERMISSION_MODES = frozenset({
    "acceptEdits", "bypassPermissions", "dontAsk", "auto",
})


def parse_toml(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        raw = raw.split("#", 1)[0].strip()
        if raw[:1] in ('"', "'") and raw[-1:] == raw[:1]:
            result[key] = raw[1:-1]
        else:
            result[key] = raw
    return result


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return parse_toml(CONFIG_PATH.read_text())


def settings_search_paths() -> list[Path]:
    return [
        Path("~/.claude/settings.json").expanduser(),
        Path(".claude/settings.json"),
        Path(".claude/settings.local.json"),
    ]


def settings_default_mode() -> str | None:
    found = None
    for path in settings_search_paths():
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        perms = data.get("permissions")
        if isinstance(perms, dict) and perms.get("defaultMode"):
            found = perms["defaultMode"]
    return found


def session_auto_approves(hook_input: dict) -> bool:
    mode = hook_input.get("permission_mode") or settings_default_mode()
    return mode in AUTO_APPROVE_PERMISSION_MODES


def tg_post(token: str, method: str, payload: dict, timeout: int = 10) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read())


def send_message(token: str, chat_id: str, text: str,
                 reply_markup: dict | None = None,
                 parse_mode: str = "HTML") -> int | None:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        result = tg_post(token, "sendMessage", payload)
        return result.get("result", {}).get("message_id")
    except Exception as exc:  # noqa: BLE001
        err(f"send_message failed: {exc}")
        return None


def poll_decided(decided_path: Path) -> dict | None:
    """Return parsed JSON from a decided file, or None if not yet present."""
    if not decided_path.exists():
        return None
    try:
        return json.loads(decided_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def err(msg: str) -> None:
    print(f"[tg-hook] {msg}", file=sys.stderr)

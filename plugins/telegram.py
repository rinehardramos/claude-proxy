"""Telegram notification plugin for claude-proxy.

Sends a brief summary to a Telegram chat after each Claude response.
Stdlib only: json, os, sys, threading, urllib.request.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.request

_bot_token: str | None = None
_chat_id: str | None = None


def plugin_info() -> dict:
    return {
        "name": "telegram",
        "version": "0.1.0",
        "description": "Telegram notifications",
    }


def configure(config: dict) -> None:
    """Called once at load time with the plugin's config section from plugins.toml.

    Credential resolution order (first match wins):
      1. Direct values:  bot_token = "..." / chat_id = "..."  in plugins.toml
      2. Env var names:  bot_token_env = "VAR" / chat_id_env = "VAR"  in plugins.toml
      3. Default env vars: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
    """
    global _bot_token, _chat_id

    # 1. Direct values in config
    _bot_token = config.get("bot_token")
    _chat_id = config.get("chat_id")

    # 2/3. Fall back to env var lookup
    if not _bot_token:
        token_env = config.get("bot_token_env", "TELEGRAM_BOT_TOKEN")
        _bot_token = os.environ.get(token_env)
    if not _chat_id:
        chat_env = config.get("chat_id_env", "TELEGRAM_CHAT_ID")
        _chat_id = os.environ.get(chat_env)

    if not _bot_token or not _chat_id:
        missing = []
        if not _bot_token:
            missing.append("bot_token / " + config.get("bot_token_env", "TELEGRAM_BOT_TOKEN"))
        if not _chat_id:
            missing.append("chat_id / " + config.get("chat_id_env", "TELEGRAM_CHAT_ID"))
        print(
            f"[telegram] WARNING: credentials not found: {', '.join(missing)} — plugin disabled",
            file=sys.stderr,
        )
        _bot_token = None
        _chat_id = None


def on_inbound(response_text: str, request_summary: dict) -> str | None:
    """Fire-and-forget Telegram notification after each Claude response.

    Args:
        response_text: Full assembled text of Claude's response.
        request_summary: {"user_text": "...", "model": "...", "path": "..."}

    Returns:
        None — no content injected into the response stream.
    """
    if not _bot_token or not _chat_id:
        return None

    user_text = request_summary.get("user_text", "")
    truncated = user_text[:100]
    if len(user_text) > 100:
        truncated += "..."

    message = f'Claude responded to: "{truncated}"\n{len(response_text)} chars'

    # Capture module-level state into locals before spawning — avoids a
    # configure() race and keeps the closure free of mutable global refs.
    token = _bot_token
    chat_id = _chat_id

    def _send() -> None:
        try:
            tg_base = "https://api.telegram.org"
            tg_path = "/bot" + token + "/sendMessage"
            data = json.dumps({"chat_id": chat_id, "text": message}).encode()
            req = urllib.request.Request(
                tg_base + tg_path,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:
            print(f"[telegram] ERROR: {exc}", file=sys.stderr)

    threading.Thread(target=_send, daemon=True).start()
    return None

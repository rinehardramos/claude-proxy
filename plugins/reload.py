"""Reload plugin for claude-proxy.

Intercepts a configurable trigger prefix in the user's message (default: !reload),
fires a hot-reload of all plugins via the proxy's /reload endpoint, then strips
the prefix so Claude receives a clean message.

Usage: prefix any message with !reload to reload plugins mid-session.
Example: "!reload what plugins are active?"
"""
from __future__ import annotations

import copy
import sys
import threading
import urllib.request

_port: int = 18019
_trigger: str = "!reload"


def plugin_info() -> dict:
    return {
        "name": "reload",
        "version": "0.1.0",
        "description": "Trigger plugin hot-reload by prefixing a message with !reload",
    }


def configure(config: dict) -> None:
    global _port, _trigger
    _port = int(config.get("port", 18019))
    _trigger = config.get("trigger", "!reload")


def on_outbound(payload: dict) -> dict | None:
    """Detect trigger prefix, fire reload, return payload with prefix stripped."""
    messages = payload.get("messages", [])
    if not messages:
        return None

    last = messages[-1]
    if last.get("role") != "user":
        return None

    content = last.get("content", "")
    is_list = isinstance(content, list)

    if is_list:
        text = ""
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                break
    else:
        text = content if isinstance(content, str) else ""

    if not text.strip().startswith(_trigger):
        return None

    # Fire reload in background — will queue as pending swap if a request is
    # in flight (PluginManager handles this safely).
    port = _port

    def _do_reload() -> None:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/reload", timeout=3)
            _log("hot-reload triggered")
        except Exception as exc:
            _log(f"reload request failed: {exc}")

    threading.Thread(target=_do_reload, daemon=True).start()

    # Strip trigger prefix; fall back to original text if nothing remains.
    new_text = text.strip()[len(_trigger):].lstrip()
    if not new_text:
        new_text = text

    new_payload = copy.deepcopy(payload)
    new_last = new_payload["messages"][-1]
    if is_list:
        for block in reversed(new_last.get("content", [])):
            if isinstance(block, dict) and block.get("type") == "text":
                block["text"] = new_text
                break
    else:
        new_last["content"] = new_text

    return new_payload


def _log(msg: str) -> None:
    print(f"[reload] {msg}", file=sys.stderr)

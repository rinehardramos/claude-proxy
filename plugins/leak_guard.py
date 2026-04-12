"""leak-guard plugin for claude-proxy.

Scans outbound request payloads (system prompt + user messages) for
secrets and PII, redacting any findings before the payload is forwarded
to the Anthropic API.

Scanner discovery order:
  1. config["scanner_path"]      in plugins.toml [leak_guard] section
  2. LEAK_GUARD_SCANNER env var
  3. Auto-discover: newest scanner.py under
     ~/.claude/plugins/cache/leak-guard/*/hooks/scanner.py
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

_scanner = None  # loaded scanner module, or None if unavailable


def plugin_info() -> dict:
    return {
        "name": "leak-guard",
        "version": "0.7.0",
        "description": "Redact secrets/PII from outbound payloads via leak-guard scanner",
    }


def _sha256_short(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:8]


def _redact_token(raw: str, rule_id: str) -> str:
    return f"[REDACTED:{rule_id}:{len(raw)}ch:hash={_sha256_short(raw)}]"


def _discover_scanner() -> str | None:
    """Return path to newest scanner.py in the leak-guard plugin cache, or None."""
    cache = Path("~/.claude/plugins/cache/leak-guard").expanduser()
    if not cache.exists():
        return None
    candidates = sorted(
        cache.glob("*/hooks/scanner.py"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return str(candidates[0]) if candidates else None


def _load_scanner(path: str):
    """Import scanner.py from *path* via importlib. Returns module or None."""
    try:
        spec = importlib.util.spec_from_file_location("_lg_scanner", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod
    except Exception as exc:
        print(f"[leak-guard] failed to load scanner at {path}: {exc}", file=sys.stderr)
        return None


def configure(config: dict) -> None:
    """Resolve and load scanner.py. Called once at proxy startup."""
    global _scanner
    path = (
        config.get("scanner_path")
        or os.environ.get("LEAK_GUARD_SCANNER")
        or _discover_scanner()
    )
    if not path:
        print("[leak-guard] WARNING: scanner.py not found — plugin disabled", file=sys.stderr)
        return
    _scanner = _load_scanner(path)
    if _scanner is None:
        print("[leak-guard] WARNING: scanner load failed — plugin disabled", file=sys.stderr)


def _redact_text(text: str) -> str:
    """Return *text* with all scanner findings replaced by REDACTED tokens."""
    if not text:
        return text
    try:
        findings = _scanner.scan_all(text=text)
    except Exception as exc:
        print(f"[leak-guard] scan error: {exc}", file=sys.stderr)
        return text
    for finding in findings:
        raw = getattr(finding, "raw_match", "")
        if raw and raw in text:
            text = text.replace(raw, _redact_token(raw, finding.rule_id))
    return text


def _redact_content(content: Any) -> Any:
    """Redact a message content field (string or list of blocks)."""
    if isinstance(content, str):
        return _redact_text(content)
    if isinstance(content, list):
        result = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                block = {**block, "text": _redact_text(block.get("text", ""))}
            result.append(block)
        return result
    return content


def on_outbound(payload: dict) -> dict:
    """Scan and redact outbound payload before it reaches Anthropic.

    Only user messages and the system prompt are scanned — assistant
    messages are inbound (Claude's own previous responses) and are
    already in Claude's local context.
    """
    if _scanner is None:
        return payload

    payload = copy.deepcopy(payload)

    # System prompt
    system = payload.get("system")
    if isinstance(system, str):
        payload["system"] = _redact_text(system)
    elif isinstance(system, list):
        payload["system"] = [
            {**b, "text": _redact_text(b.get("text", ""))}
            if isinstance(b, dict) and b.get("type") == "text"
            else b
            for b in system
        ]

    # User messages only
    for msg in payload.get("messages", []):
        if msg.get("role") == "user":
            msg["content"] = _redact_content(msg.get("content", ""))

    return payload

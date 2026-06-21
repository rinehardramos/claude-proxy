#!/usr/bin/env python3
"""PreToolUse hook: answer AskUserQuestion decisions from Telegram.

Standalone — stdlib only (+ sibling _tg_hook_common). Emits no stdout to fall
back to Claude Code's local prompt; emits allow+updatedInput to answer remotely.
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import sys
import time
import uuid
from html import escape as _esc
from pathlib import Path

_spec = _ilu.spec_from_file_location(
    "_tg_hook_common", str(Path(__file__).with_name("_tg_hook_common.py")))
_common = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_common)


def should_route(hook_input: dict, config: dict) -> bool:
    mode = (config.get("decide_route", "auto") or "auto").lower()
    if mode == "off":
        return False
    if mode == "always":
        return True
    return _common.session_auto_approves(hook_input)  # "auto" → only when away


def has_multiselect(questions: list[dict]) -> bool:
    return any(q.get("multiSelect") for q in questions)


def build_question_messages(decision_id: str, questions: list[dict]) -> list[dict]:
    msgs = []
    for qi, q in enumerate(questions):
        header = _esc(q.get("header", ""))
        text = f"<b>{header}</b>\n{_esc(q.get('question', ''))}" if header \
            else _esc(q.get("question", ""))
        keyboard = [[{"text": opt.get("label", f"Option {oi}"),
                      "callback_data": f"qopt:{decision_id}:{qi}:{oi}"}]
                    for oi, opt in enumerate(q.get("options", []))]
        msgs.append({"q_index": qi, "text": text,
                     "reply_markup": {"inline_keyboard": keyboard}})
    return msgs


def assemble_answers(questions: list[dict], answered: dict) -> dict:
    out = {}
    for qi_str, oi in answered.items():
        qi = int(qi_str)
        q = questions[qi]
        label = q["options"][int(oi)]["label"]
        out[q.get("question", "")] = label
    return out


def _emit_nothing() -> None:
    sys.exit(0)  # no stdout → local TUI


def main() -> None:
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        _emit_nothing()
    if hook_input.get("tool_name") != "AskUserQuestion":
        _emit_nothing()

    config = _common.load_config()
    token = config.get("bot_token", "")
    chat_id = config.get("chat_id", "")
    if not token or not chat_id:
        _emit_nothing()
    if not should_route(hook_input, config):
        _emit_nothing()

    questions = hook_input.get("tool_input", {}).get("questions", [])
    if not questions or has_multiselect(questions):
        _emit_nothing()  # Phase 1: don't answer multiSelect wrong

    decision_id = uuid.uuid4().hex  # FIX 3: full 32-char entropy
    timeout = int(config.get("decide_timeout", "600"))
    _common.PENDING_DIR.mkdir(parents=True, exist_ok=True)
    _common.DECIDED_DIR.mkdir(parents=True, exist_ok=True)

    msgs = build_question_messages(decision_id, questions)
    # FIX 2: delete already-sent messages if a later send fails
    message_ids = []
    for msg in msgs:
        mid = _common.send_message(token, chat_id, msg["text"], msg["reply_markup"])
        if mid is None:
            for prev in message_ids:
                try:
                    _common.tg_post(token, "deleteMessage",
                                    {"chat_id": chat_id, "message_id": prev})
                except Exception:
                    pass
            _emit_nothing()
        message_ids.append(mid)

    pending = _common.PENDING_DIR / f"{decision_id}.json"
    pending.write_text(json.dumps({
        "kind": "decide",
        "n_questions": len(questions),
        "message_ids": message_ids,
        "answered": {},
        "created_at": time.time(),
    }))

    # FIX 1: always unlink decided file once seen, regardless of parse/assemble outcome
    decided_path = _common.DECIDED_DIR / f"{decision_id}.json"
    deadline = time.time() + timeout
    answers = None
    while time.time() < deadline:
        if decided_path.exists():
            try:
                result = json.loads(decided_path.read_text())
            except (OSError, json.JSONDecodeError):
                result = None
            decided_path.unlink(missing_ok=True)  # always clean up once seen
            if result is not None:
                try:
                    answers = assemble_answers(questions, result.get("answered", {}))
                except (KeyError, IndexError, TypeError):
                    answers = None
            break
        time.sleep(2)
    pending.unlink(missing_ok=True)
    if not answers:
        _emit_nothing()  # timeout or unusable decision → local fallback
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {"questions": questions, "answers": answers},
    }}))
    sys.exit(0)


if __name__ == "__main__":
    main()

# Telegram Remote Decisions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user answer Claude's `AskUserQuestion` decisions from Telegram (one button per option); the choice is fed back via a PreToolUse hook so the model proceeds without the local terminal menu.

**Architecture:** A new PreToolUse hook (`hooks/telegram_decide.py`, matcher `AskUserQuestion`) sends each question to Telegram with option buttons, polls the shared `telegram-hook` IPC dir for the user's selection (written by the proxy plugin's existing callback poller), and returns `permissionDecision:"allow"` with `updatedInput{questions, answers}`. Shared Telegram/config/gating code lives in `hooks/_tg_hook_common.py`. The resume deliverer gains an autonomy flag so headless tasks don't stall on tool approvals.

**Tech Stack:** Python 3 stdlib only (hooks are standalone); `unittest` run under **pytest**.

## Global Constraints

- Hooks (`hooks/*.py`) are **standalone, stdlib only** — no imports from `proxy.py` or `plugins/`. They may import the sibling `hooks/_tg_hook_common.py`.
- Test runner is **pytest** (in `.venv`): `. .venv/bin/activate` then `python3 -m pytest <path> -q`. Telegram tests are `unittest.TestCase`; hook tests in `tests/test_telegram_hook.py` are also unittest-style.
- **GATE:** Task 1 (validation spike) must pass before Tasks 2–7. If the `updatedInput.answers` mechanism does not answer `AskUserQuestion` on this Claude Code build, STOP and revise the spec.
- Hook output schema (exact): `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","updatedInput":{...}}}`. **Emitting no stdout** = default behavior (local TUI). Every failure/edge path emits nothing (never block the user).
- Session-aware gating lives in `hooks/_tg_hook_common.py` (self-contained; do NOT depend on the uncommitted permission-mode WIP in `telegram_approve.py`).
- `_AUTO_APPROVE_PERMISSION_MODES = frozenset({"acceptEdits","bypassPermissions","dontAsk","auto"})` (verbatim).
- Shared IPC dir: `~/.claude/claude-proxy/telegram-hook/` with `pending/` and `decided/` subdirs (same as `telegram_approve.py`).
- Callback data format for option buttons: `qopt:<decision_id>:<qIdx>:<optIdx>` (all ints except decision_id hex).

## File Structure

- `hooks/_tg_hook_common.py` — NEW. Shared stdlib helpers: config/TOML, Telegram POST/send, paths, session-aware gating.
- `hooks/telegram_decide.py` — NEW. The PreToolUse decision hook.
- `plugins/telegram.py` — MODIFY. Add `qopt` callback handling; add `deliver_autonomous` to `_deliver_one` + config.
- `plugins/telegram.toml` — MODIFY. Document `decide_route`, `decide_timeout`, `deliver_autonomous`.
- `tests/test_tg_hook_common.py` — NEW.
- `tests/test_telegram_decide.py` — NEW.
- `tests/test_telegram.py` — MODIFY. `qopt` + deliverer-autonomy tests.

---

### Task 1: Validation spike (GATE — manual, no TDD)

**Goal:** Prove a PreToolUse hook can answer `AskUserQuestion` via `updatedInput.answers` on this machine's Claude Code build, suppressing the local menu.

**Files:**
- Create (throwaway): `/tmp/spike_decide.py`

- [ ] **Step 1: Write a hard-coded-answer hook**

Create `/tmp/spike_decide.py`:

```python
#!/usr/bin/env python3
import json, sys
data = json.loads(sys.stdin.read() or "{}")
if data.get("tool_name") != "AskUserQuestion":
    sys.exit(0)  # let it pass
questions = data.get("tool_input", {}).get("questions", [])
answers = {}
for q in questions:
    opts = q.get("options", [])
    if not opts:
        sys.exit(0)
    answers[q.get("question", "")] = opts[0]["label"]  # always pick first option
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {"questions": questions, "answers": answers},
    }
}))
sys.stderr.write(f"[spike] answered {answers}\n")
```

- [ ] **Step 2: Register it temporarily**

Add to `~/.claude/settings.json` `hooks.PreToolUse` (back up the file first):

```json
{ "matcher": "AskUserQuestion", "hooks": [
  { "type": "command", "command": "python3 /tmp/spike_decide.py", "timeout": 60 } ] }
```

- [ ] **Step 3: Trigger a real AskUserQuestion**

In a Claude Code session, ask Claude something that makes it call `AskUserQuestion` (e.g. "ask me whether to use tabs or spaces using the question tool").

- [ ] **Step 4: Observe**

Expected: the local terminal does NOT show the option menu; Claude proceeds as if the **first option** was chosen; `/tmp/spike-stderr` (or the session's hook stderr) shows `[spike] answered {...}`.

- [ ] **Step 5: Record result and clean up**

Remove the temporary hook from `~/.claude/settings.json`; delete `/tmp/spike_decide.py`.

- **PASS** → proceed to Task 2.
- **FAIL** (menu still shows, or model errors / re-asks): STOP. The `updatedInput.answers` schema is wrong for this build — report exactly what happened and revise the spec before any further work.

No commit (throwaway).

---

### Task 2: Extract `hooks/_tg_hook_common.py`

**Files:**
- Create: `hooks/_tg_hook_common.py`
- Test: `tests/test_tg_hook_common.py`

**Interfaces:**
- Produces: `HOOK_DIR`, `PENDING_DIR`, `DECIDED_DIR`, `CONFIG_PATH` (Paths); `parse_toml(text)->dict`; `load_config()->dict`; `session_auto_approves(hook_input)->bool`; `settings_default_mode()->str|None`; `tg_post(token, method, payload, timeout=10)->dict`; `send_message(token, chat_id, text, reply_markup=None, parse_mode="HTML")->int|None` (returns message_id); `AUTO_APPROVE_PERMISSION_MODES` (frozenset).

- [ ] **Step 1: Write the failing test**

Create `tests/test_tg_hook_common.py`:

```python
import importlib.util
from pathlib import Path

_PATH = Path(__file__).parent.parent / "hooks" / "_tg_hook_common.py"


def _load():
    spec = importlib.util.spec_from_file_location("tg_hook_common", _PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_parse_toml_basic():
    m = _load()
    d = m.parse_toml('a = "x"\nb = "y"  # c\n[sec]\n')
    assert d["a"] == "x" and d["b"] == "y"


def test_session_auto_approves_true_for_accept_edits():
    m = _load()
    assert m.session_auto_approves({"permission_mode": "acceptEdits"}) is True


def test_session_auto_approves_false_for_default():
    m = _load()
    assert m.session_auto_approves({"permission_mode": "default"}) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate; python3 -m pytest tests/test_tg_hook_common.py -q`
Expected: FAIL — module file does not exist.

- [ ] **Step 3: Write minimal implementation**

Create `hooks/_tg_hook_common.py` (move the corresponding helpers out of `telegram_approve.py`'s logic, stdlib only):

```python
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


def err(msg: str) -> None:
    print(f"[tg-hook] {msg}", file=sys.stderr)
```

Then refactor `hooks/telegram_approve.py` to import these. **On committed `main`, `telegram_approve.py` only defines `_parse_toml`, `_load_config`, `_tg_post`** (the session-gating functions are NOT on main — they're in the uncommitted WIP, and `_tg_hook_common` defines them fresh above). So replace only those three. Add near the top of `telegram_approve.py`, after its existing path constants:

```python
import importlib.util as _ilu
from pathlib import Path as _P
_spec = _ilu.spec_from_file_location(
    "_tg_hook_common", str(_P(__file__).with_name("_tg_hook_common.py")))
_common = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_common)

_parse_toml = _common.parse_toml
_load_config = _common.load_config
_tg_post = _common.tg_post
```

Delete the now-duplicated `def _parse_toml`, `def _load_config`, `def _tg_post` bodies in `telegram_approve.py` (keep everything else: `_send_approval_message`, `_run_scanner`, `_format_tool_summary`, `main`, etc.). Do NOT touch any session-gating code — there is none on main.

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate; python3 -m pytest tests/test_tg_hook_common.py tests/test_telegram_hook.py -q`
Expected: PASS (new common tests AND the existing hook tests — the aliases preserve `telegram_approve`'s attribute names).

- [ ] **Step 5: Commit**

```bash
git add hooks/_tg_hook_common.py hooks/telegram_approve.py tests/test_tg_hook_common.py
git commit -m "refactor(hooks): extract shared _tg_hook_common from telegram_approve"
```

---

### Task 3: `telegram_decide.py` — pure logic (routing, messages, answers)

**Files:**
- Create: `hooks/telegram_decide.py`
- Test: `tests/test_telegram_decide.py`

**Interfaces:**
- Consumes: `_tg_hook_common.session_auto_approves`, `load_config`.
- Produces:
  - `should_route(hook_input, config) -> bool`
  - `has_multiselect(questions) -> bool`
  - `build_question_messages(decision_id, questions) -> list[dict]` — each `{"q_index": int, "text": str, "reply_markup": dict}` with buttons `qopt:<decision_id>:<qIdx>:<optIdx>`.
  - `assemble_answers(questions, answered) -> dict` — `answered` is `{qIdx: optIdx}`; returns `{question_text: option_label}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_telegram_decide.py`:

```python
import importlib.util
from pathlib import Path

_PATH = Path(__file__).parent.parent / "hooks" / "telegram_decide.py"


def _load():
    spec = importlib.util.spec_from_file_location("telegram_decide", _PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_QS = [{
    "question": "Tabs or spaces?",
    "header": "Indent",
    "options": [{"label": "Tabs", "description": "t"},
                {"label": "Spaces", "description": "s"}],
    "multiSelect": False,
}]


def test_should_route_off():
    m = _load()
    assert m.should_route({"permission_mode": "acceptEdits"}, {"decide_route": "off"}) is False


def test_should_route_always():
    m = _load()
    assert m.should_route({"permission_mode": "default"}, {"decide_route": "always"}) is True


def test_should_route_auto_when_away():
    m = _load()
    assert m.should_route({"permission_mode": "acceptEdits"}, {"decide_route": "auto"}) is True


def test_should_route_auto_at_keyboard():
    m = _load()
    assert m.should_route({"permission_mode": "default"}, {"decide_route": "auto"}) is False


def test_has_multiselect():
    m = _load()
    assert m.has_multiselect(_QS) is False
    assert m.has_multiselect([{"multiSelect": True, "options": []}]) is True


def test_build_question_messages():
    m = _load()
    msgs = m.build_question_messages("abc123", _QS)
    assert len(msgs) == 1
    kb = msgs[0]["reply_markup"]["inline_keyboard"]
    # one button per option, correct callback_data
    assert kb[0][0]["callback_data"] == "qopt:abc123:0:0"
    assert kb[1][0]["callback_data"] == "qopt:abc123:0:1"
    assert "Tabs or spaces?" in msgs[0]["text"]


def test_assemble_answers():
    m = _load()
    ans = m.assemble_answers(_QS, {0: 1})
    assert ans == {"Tabs or spaces?": "Spaces"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate; python3 -m pytest tests/test_telegram_decide.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write minimal implementation**

Create `hooks/telegram_decide.py`:

```python
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

    decision_id = uuid.uuid4().hex[:16]
    timeout = int(config.get("decide_timeout", "600"))
    _common.PENDING_DIR.mkdir(parents=True, exist_ok=True)
    _common.DECIDED_DIR.mkdir(parents=True, exist_ok=True)

    msgs = build_question_messages(decision_id, questions)
    message_ids = []
    for msg in msgs:
        mid = _common.send_message(token, chat_id, msg["text"], msg["reply_markup"])
        if mid is None:
            _emit_nothing()  # a send failed → local fallback
        message_ids.append(mid)

    pending = _common.PENDING_DIR / f"{decision_id}.json"
    pending.write_text(json.dumps({
        "kind": "decide",
        "n_questions": len(questions),
        "message_ids": message_ids,
        "answered": {},
        "created_at": time.time(),
    }))

    decided = _common.DECIDED_DIR / f"{decision_id}.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if decided.exists():
            try:
                result = json.loads(decided.read_text())
                answered = result.get("answered", {})
                decided.unlink(missing_ok=True)
                pending.unlink(missing_ok=True)
                answers = assemble_answers(questions, answered)
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": {"questions": questions, "answers": answers},
                }}))
                sys.exit(0)
            except (json.JSONDecodeError, OSError, KeyError, IndexError):
                break
        time.sleep(2)

    pending.unlink(missing_ok=True)
    _emit_nothing()  # timeout → local TUI


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate; python3 -m pytest tests/test_telegram_decide.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hooks/telegram_decide.py tests/test_telegram_decide.py
git commit -m "feat(hooks): telegram_decide hook for remote AskUserQuestion answers"
```

---

### Task 4: Plugin `qopt` callback handling

**Files:**
- Modify: `plugins/telegram.py` (`_handle_callback` dispatch near line ~12; add `_handle_qopt_callback`)
- Test: `tests/test_telegram.py`

**Interfaces:**
- Consumes: pending/decided files written/read by `telegram_decide.py` (Task 3).
- Produces: `_handle_qopt_callback(cb, token, chat_id, rest)` where `rest = "<decision_id>:<qIdx>:<optIdx>"`. Records `answered[qIdx]=optIdx` into `pending/<id>.json`; when `len(answered)==n_questions`, writes `decided/<id>.json` with `{"answered": {...}, "decided_at": ...}`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_telegram.py` a new class:

```python
class TestQoptCallback(unittest.TestCase):
    def setUp(self):
        self.t = _load()
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.t.HOOK_DIR = Path(self.tmp)
        (Path(self.tmp) / "pending").mkdir()
        (Path(self.tmp) / "decided").mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pending(self, did, n):
        p = Path(self.tmp) / "pending" / f"{did}.json"
        p.write_text(json.dumps({"kind": "decide", "n_questions": n,
                                 "message_ids": [1], "answered": {}}))
        return p

    def test_single_question_writes_decided(self):
        self._pending("d1", 1)
        cb = {"id": "q", "data": "qopt:d1:0:1", "message": {"message_id": 1}}
        with patch("urllib.request.urlopen"):
            self.t._handle_callback(cb, "tok", "chat")
        decided = Path(self.tmp) / "decided" / "d1.json"
        self.assertTrue(decided.exists())
        self.assertEqual(json.loads(decided.read_text())["answered"], {"0": 1})

    def test_two_questions_waits_for_both(self):
        self._pending("d2", 2)
        with patch("urllib.request.urlopen"):
            self.t._handle_callback({"id": "q", "data": "qopt:d2:0:0",
                                     "message": {"message_id": 1}}, "tok", "chat")
        self.assertFalse((Path(self.tmp) / "decided" / "d2.json").exists())
        with patch("urllib.request.urlopen"):
            self.t._handle_callback({"id": "q", "data": "qopt:d2:1:1",
                                     "message": {"message_id": 1}}, "tok", "chat")
        decided = Path(self.tmp) / "decided" / "d2.json"
        self.assertTrue(decided.exists())
        self.assertEqual(json.loads(decided.read_text())["answered"], {"0": 0, "1": 1})

    def test_unknown_decision_ignored(self):
        cb = {"id": "q", "data": "qopt:nope:0:0", "message": {"message_id": 1}}
        with patch("urllib.request.urlopen"):
            self.t._handle_callback(cb, "tok", "chat")  # no crash
        self.assertFalse((Path(self.tmp) / "decided" / "nope.json").exists())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate; python3 -m pytest tests/test_telegram.py::TestQoptCallback -q`
Expected: FAIL — `qopt` not dispatched; no decided file.

- [ ] **Step 3: Write minimal implementation**

In `plugins/telegram.py` `_handle_callback`, after the existing `if action == "option":` block, add:

```python
    if action == "qopt":
        _handle_qopt_callback(cb, token, chat_id, decision_id)
        return
```

(`decision_id` here is the post-`split(":",1)` remainder = `"<id>:<qIdx>:<optIdx>"`.)

Add the handler (near `_handle_option_callback`):

```python
def _handle_qopt_callback(cb: dict, token: str, chat_id: str, rest: str) -> None:
    """Record an AskUserQuestion option pick; write decided when all answered."""
    query_id = cb.get("id", "")
    try:
        did, q_idx, opt_idx = rest.split(":")
    except ValueError:
        return
    pending_path = HOOK_DIR / "pending" / f"{did}.json"
    decided_path = HOOK_DIR / "decided" / f"{did}.json"
    if not pending_path.exists():
        _answer_callback_query(token, query_id, "Decision expired")
        return
    try:
        info = json.loads(pending_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    answered = info.get("answered", {})
    answered[str(q_idx)] = int(opt_idx)
    info["answered"] = answered
    try:
        pending_path.write_text(json.dumps(info))
    except OSError:
        pass
    _answer_callback_query(token, query_id, "Recorded")
    # show the pick on the message
    msg = cb.get("message", {})
    msg_id = msg.get("message_id")
    label = "selected"
    kb = msg.get("reply_markup", {}).get("inline_keyboard", [])
    try:
        label = kb[int(opt_idx)][0].get("text", label)
    except (ValueError, IndexError):
        pass
    if msg_id:
        try:
            data = json.dumps({
                "chat_id": chat_id, "message_id": msg_id,
                "reply_markup": json.dumps({"inline_keyboard": [[
                    {"text": f"✅ {label}", "callback_data": "noop:qopt"}]]}),
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/editMessageReplyMarkup",
                data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass
    # all answered → write decided
    if len(answered) >= int(info.get("n_questions", 1)):
        try:
            decided_path.write_text(json.dumps({
                "answered": answered, "decided_at": time.time()}))
            pending_path.unlink(missing_ok=True)
        except OSError:
            pass
    _log(f"qopt {did[:8]} q{q_idx}={opt_idx} ({len(answered)}/{info.get('n_questions')})")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate; python3 -m pytest tests/test_telegram.py::TestQoptCallback tests/test_telegram.py::TestCallbackPoller -q`
Expected: PASS (new + existing callback tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/telegram.py tests/test_telegram.py
git commit -m "feat(telegram): qopt callback records AskUserQuestion answers for the decide hook"
```

---

### Task 5: Deliverer autonomy flag

**Files:**
- Modify: `plugins/telegram.py` (`configure()` near line 375; `_deliver_one` near line 1437)
- Test: `tests/test_telegram.py`

**Interfaces:**
- Produces: module global `_deliver_autonomous: bool` (default True); when true, `_deliver_one`'s command includes `--dangerously-skip-permissions --permission-mode acceptEdits`.

- [ ] **Step 1: Write the failing test**

Add to `class TestDeliverer` in `tests/test_telegram.py`:

```python
    def test_deliver_autonomous_flags(self):
        self.t._deliver_autonomous = True
        p = self.t._inbox_put("x", self.cwd)
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")) as m:
            self.t._deliver_one(p)
        args = m.call_args[0][0]
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertIn("--permission-mode", args)
        self.assertIn("acceptEdits", args)

    def test_deliver_non_autonomous_omits_flags(self):
        self.t._deliver_autonomous = False
        p = self.t._inbox_put("x", self.cwd)
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")) as m:
            self.t._deliver_one(p)
        self.assertNotIn("--dangerously-skip-permissions", m.call_args[0][0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate; python3 -m pytest "tests/test_telegram.py::TestDeliverer::test_deliver_autonomous_flags" -q`
Expected: FAIL — flags absent (and `_deliver_autonomous` may be undefined).

- [ ] **Step 3: Write minimal implementation**

Add a module global near `_resume_max_attempts`:

```python
_deliver_autonomous: bool = True
```

In `configure()`, after `_resume_max_attempts = int(...)`, add (and extend the `global` line to include it):

```python
    _deliver_autonomous = str(config.get("deliver_autonomous", "true")).lower() in ("true", "1", "yes", "on")
```

In `_deliver_one`, replace the command list build:

```python
        cmd = [_claude_bin, "--continue", "--print", text]
        if _deliver_autonomous:
            cmd = [_claude_bin, "--continue", "--dangerously-skip-permissions",
                   "--permission-mode", "acceptEdits", "--print", text]
        proc = subprocess.run(
            cmd,
            cwd=cwd, capture_output=True, text=True, timeout=_resume_timeout,
            stdin=subprocess.DEVNULL,
            env=env,
        )
```

(Replace the existing `subprocess.run([_claude_bin, "--continue", "--print", text], ...)` call with the above.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate; python3 -m pytest tests/test_telegram.py::TestDeliverer -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/telegram.py tests/test_telegram.py
git commit -m "feat(telegram): deliver_autonomous runs claude with skip-permissions + acceptEdits"
```

---

### Task 6: Config docs + decide config reads + registration + verify

**Files:**
- Modify: `plugins/telegram.toml`
- Test: full suites

**Interfaces:** none new (config is consumed by the hook via `load_config()` and by `configure()` for `deliver_autonomous`).

- [ ] **Step 1: Document config in `plugins/telegram.toml`**

Append:

```toml
# ── Remote decisions (answer Claude's AskUserQuestion from Telegram) ──────
# Requires the PreToolUse hook registered in settings.json:
#   matcher "AskUserQuestion" -> python3 ~/.claude/claude-proxy/hooks/telegram_decide.py
decide_route = "auto"     # "auto" (route only when away) | "always" | "off"
decide_timeout = 600      # seconds the hook waits for a Telegram answer

# Resume-delivery autonomy: run delivered `claude --continue` without tool-
# approval prompts so headless tasks don't stall. AskUserQuestion decisions
# still route to Telegram via the decide hook.
deliver_autonomous = true
```

- [ ] **Step 2: Document the hook registration**

Add to the repo README or a comment block in `hooks/telegram_decide.py` docstring (already present): the settings.json snippet:

```json
{ "hooks": { "PreToolUse": [
  { "matcher": "AskUserQuestion", "hooks": [
    { "type": "command",
      "command": "python3 ~/.claude/claude-proxy/hooks/telegram_decide.py",
      "timeout": 600 } ] } ] } }
```

- [ ] **Step 3: Run the full relevant suites**

Run: `. .venv/bin/activate; python3 -m pytest tests/test_telegram.py tests/test_telegram_decide.py tests/test_tg_hook_common.py tests/test_telegram_hook.py -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add plugins/telegram.toml hooks/telegram_decide.py
git commit -m "docs(telegram): document decide_route/deliver_autonomous + hook registration"
```

---

## Self-Review

**Spec coverage:**
- AskUserQuestion remote answer via `updatedInput.answers` → Tasks 3, 4 (+ gate Task 1). ✓
- Session-aware "away" gating → `should_route` (Task 3) using `session_auto_approves` (Task 2). ✓
- Shared hook helper extraction → Task 2. ✓
- Deliverer autonomy (`--dangerously-skip-permissions --permission-mode acceptEdits`) → Task 5. ✓
- multiSelect falls back to local (Phase 1) → `has_multiselect` guard in Task 3. ✓
- Never-block-user invariant (emit nothing on every failure/edge) → Task 3 `_emit_nothing` paths. ✓
- Config keys + registration → Task 6. ✓
- Deferred (ExitPlanMode, multiSelect answering, prose tier) → not in plan, per spec. ✓

**Placeholder scan:** none.

**Type consistency:** `decision_id` (hex str), `answered` keys are stringified qIdx with int optIdx values (JSON), `assemble_answers(questions, answered)` and `_handle_qopt_callback` agree on `{str(qIdx): int(optIdx)}`; `qopt:<id>:<q>:<opt>` callback parsed consistently in Tasks 3/4; `build_question_messages` button `callback_data` matches the plugin parser.

**Known entanglement (note for executor):** Task 2 builds session-gating in `_tg_hook_common.py` independent of the uncommitted permission-mode WIP in `telegram_approve.py`. If that WIP is committed first, reconcile by pointing its gating at `_tg_hook_common` (a small follow-up, not part of this plan).

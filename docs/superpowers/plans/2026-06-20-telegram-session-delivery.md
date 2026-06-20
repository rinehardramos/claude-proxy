# Telegram → Session Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Actively deliver Telegram messages into the relevant project's Claude Code session — preserving conversation context and waking idle sessions — via a headless `claude --continue` deliverer driven by a shared supervised tick.

**Architecture:** The existing `tg-poller` thread receives a Telegram message, resolves a target project directory (`cwd`), and writes it to a durable disk inbox. A new generic `on_tick()` plugin hook — fired from the proxy's existing supervised `ResourceMonitor` loop — drains the inbox and spawns `claude --continue --print "<text>"` in that `cwd`; Claude's reply flows back through the proxy and is posted to Telegram by the existing `on_inbound` path. A `/project` Telegram command lets the user target a specific project. The plugin owns no scheduler of its own.

**Tech Stack:** Python 3 stdlib only (`threading`, `subprocess`, `urllib`, `json`, `pathlib`, `shlex`); `unittest` for tests (pytest is NOT installed — run tests with `python3 -m unittest`).

## Global Constraints

- Python stdlib only — no third-party imports in `plugins/telegram.py` or `monitor.py`.
- The project's test runner is **pytest** (installed in `.venv` via `uv pip install pytest`). Activate first: `. .venv/bin/activate`. Run a file with `python3 -m pytest tests/test_<name>.py -q`; a single test with `python3 -m pytest tests/test_<name>.py::TestClass::test_method -q` (or `tests/test_<name>.py::test_func` for bare functions). The telegram tests are `unittest.TestCase`-based and also run under `python3 -m unittest tests.test_telegram.TestClass -v`, but `tests/test_monitor.py` is pytest-style and must be run with pytest. Prefer pytest everywhere for consistency.
- Tests use the existing pattern in `tests/test_telegram.py`: `_load()` returns a FRESH module instance per test class to prevent module-level state bleed. Always load via `self.t = _load()`.
- Backward compatibility: `delivery_mode` defaults to `"inject"` → behavior identical to today. All existing telegram tests must stay green.
- `monitor.py` `ResourceMonitor.start()` must remain backward compatible: existing calls `start(on_recycle=..., interval_s=...)` keep working unchanged.
- Never use `Date.now()`-style nondeterminism in tests; freeze or inject where timing matters. (`time.time_ns()` is fine in production code.)
- Commit after each task with the exact message shown.

## File Structure

- `plugins/telegram.py` — MODIFY. All plugin-side logic: registry, override, inbox, parsing, command handling, deliverer, `on_tick`, config.
- `monitor.py` — MODIFY. Add optional `on_tick`/`tick_interval_s` to `ResourceMonitor.start()`.
- `proxy.py` — MODIFY. Fan out `on_tick` to plugins; pass it to `resource_monitor.start()`.
- `plugins/telegram.toml` — MODIFY. Document the new config keys and commands.
- `tests/test_telegram.py` — MODIFY. New unit/integration tests.
- `tests/test_monitor.py` — MODIFY. Tests for the `on_tick` carrier.

---

### Task 1: Delivery config + module state in `configure()`

**Files:**
- Modify: `plugins/telegram.py` (module-state block near line 33; `configure()` near line 238)
- Test: `tests/test_telegram.py`

**Interfaces:**
- Produces: module globals `_delivery_mode: str`, `_claude_bin: str`, `_resume_timeout: int`, `_resume_max_attempts: int`, `_last_active_cwd: str | None`; `configure()` reads `delivery_mode`, `claude_bin`, `resume_timeout`, `resume_max_attempts` from config.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_telegram.py` inside `class TestConfigure`:

```python
    def test_reads_delivery_config_defaults(self):
        env = self._clean_env()
        env["TELEGRAM_BOT_TOKEN"] = "tok"
        env["TELEGRAM_CHAT_ID"] = "chat"
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({})
        self.assertEqual(self.t._delivery_mode, "inject")
        self.assertEqual(self.t._claude_bin, "claude")
        self.assertEqual(self.t._resume_timeout, 300)
        self.assertEqual(self.t._resume_max_attempts, 3)

    def test_reads_delivery_config_overrides(self):
        env = self._clean_env()
        env["TELEGRAM_BOT_TOKEN"] = "tok"
        env["TELEGRAM_CHAT_ID"] = "chat"
        with patch.dict(os.environ, env, clear=True):
            self.t.configure({
                "delivery_mode": "resume",
                "claude_bin": "/usr/local/bin/claude",
                "resume_timeout": 120,
                "resume_max_attempts": 5,
            })
        self.assertEqual(self.t._delivery_mode, "resume")
        self.assertEqual(self.t._claude_bin, "/usr/local/bin/claude")
        self.assertEqual(self.t._resume_timeout, 120)
        self.assertEqual(self.t._resume_max_attempts, 5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestConfigure.test_reads_delivery_config_defaults -v`
Expected: FAIL with `AttributeError: module ... has no attribute '_delivery_mode'`

- [ ] **Step 3: Write minimal implementation**

In `plugins/telegram.py`, add to the module-state block (after line 33, `_voice_upload_timeout`):

```python
_delivery_mode: str = "inject"   # "inject" | "resume"
_claude_bin: str = "claude"
_resume_timeout: int = 300
_resume_max_attempts: int = 3
_last_active_cwd: str | None = None
```

In `configure()`, extend the `global` declaration and add reads. Change the existing `global` line near the top of `configure()` to include the new names, and add after `_notify_on_recycle = bool(...)`:

```python
    global _delivery_mode, _claude_bin, _resume_timeout, _resume_max_attempts

    _delivery_mode = config.get("delivery_mode", "inject")
    _claude_bin = config.get("claude_bin", "claude")
    _resume_timeout = int(config.get("resume_timeout", 300))
    _resume_max_attempts = int(config.get("resume_max_attempts", 3))
```

(Place these reads BEFORE the `if not _bot_token or not _chat_id:` early-return so they are always set.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestConfigure -v`
Expected: PASS (all TestConfigure tests, including the two new ones)

- [ ] **Step 5: Commit**

```bash
git add plugins/telegram.py tests/test_telegram.py
git commit -m "feat(telegram): add delivery_mode/claude_bin/resume config"
```

---

### Task 2: Project-tag registry (`message_id → cwd`)

**Files:**
- Modify: `plugins/telegram.py` (add a registry section after the module-state block)
- Test: `tests/test_telegram.py`

**Interfaces:**
- Produces:
  - `_record_message_target(message_id: int | None, cwd: str) -> None` — records mapping and updates `_last_active_cwd`.
  - `_cwd_for_message(message_id: int | None) -> str | None`
  - `_recent_cwds(limit: int = 10) -> list[str]` — most-recent-first, de-duplicated.
  - module globals `_msg_cwd` (OrderedDict), `_MSG_CWD_CAP = 500`.

- [ ] **Step 1: Write the failing test**

Add a new test class to `tests/test_telegram.py`:

```python
class TestMessageRegistry(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_record_and_resolve(self):
        self.t._record_message_target(42, "/home/u/proj")
        self.assertEqual(self.t._cwd_for_message(42), "/home/u/proj")

    def test_record_updates_last_active(self):
        self.t._record_message_target(1, "/home/u/a")
        self.assertEqual(self.t._last_active_cwd, "/home/u/a")

    def test_resolve_unknown_returns_none(self):
        self.assertIsNone(self.t._cwd_for_message(999))

    def test_none_message_id_ignored_but_sets_last_active(self):
        self.t._record_message_target(None, "/home/u/b")
        self.assertEqual(self.t._last_active_cwd, "/home/u/b")
        self.assertIsNone(self.t._cwd_for_message(None))

    def test_fifo_eviction_at_cap(self):
        self.t._MSG_CWD_CAP = 3
        for i in range(5):
            self.t._record_message_target(i, f"/p/{i}")
        # Oldest (0, 1) evicted; newest kept
        self.assertIsNone(self.t._cwd_for_message(0))
        self.assertIsNone(self.t._cwd_for_message(1))
        self.assertEqual(self.t._cwd_for_message(4), "/p/4")

    def test_recent_cwds_most_recent_first_deduped(self):
        self.t._record_message_target(1, "/p/a")
        self.t._record_message_target(2, "/p/b")
        self.t._record_message_target(3, "/p/a")
        self.assertEqual(self.t._recent_cwds(), ["/p/a", "/p/b"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestMessageRegistry -v`
Expected: FAIL with `AttributeError: ... has no attribute '_record_message_target'`

- [ ] **Step 3: Write minimal implementation**

Add `from collections import OrderedDict` to the imports at the top of `plugins/telegram.py`. Add a new section after the module-state block:

```python
# ── Project-tag registry (message_id → cwd) ──────────────────────────────

_msg_cwd_lock = threading.Lock()
_msg_cwd: "OrderedDict[int, str]" = OrderedDict()
_MSG_CWD_CAP = 500


def _record_message_target(message_id: int | None, cwd: str) -> None:
    """Record which project cwd a sent Telegram message belongs to."""
    global _last_active_cwd
    if cwd:
        _last_active_cwd = cwd
    if not message_id or not cwd:
        return
    with _msg_cwd_lock:
        _msg_cwd[message_id] = cwd
        _msg_cwd.move_to_end(message_id)
        while len(_msg_cwd) > _MSG_CWD_CAP:
            _msg_cwd.popitem(last=False)


def _cwd_for_message(message_id: int | None) -> str | None:
    if not message_id:
        return None
    with _msg_cwd_lock:
        return _msg_cwd.get(message_id)


def _recent_cwds(limit: int = 10) -> list[str]:
    """Distinct cwds, most-recently-recorded first."""
    seen: list[str] = []
    with _msg_cwd_lock:
        for cwd in reversed(list(_msg_cwd.values())):
            if cwd not in seen:
                seen.append(cwd)
            if len(seen) >= limit:
                break
    return seen
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestMessageRegistry -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/telegram.py tests/test_telegram.py
git commit -m "feat(telegram): message_id→cwd registry with FIFO cap"
```

---

### Task 3: Capture `message_id` + cwd in `on_inbound`

**Files:**
- Modify: `plugins/telegram.py` (`on_inbound._send`, the send loop near lines 386–407; capture `cwd` from `request_summary` near line 332)
- Test: `tests/test_telegram.py`

**Interfaces:**
- Consumes: `_record_message_target` (Task 2).
- Produces: after `on_inbound` sends notifications, the registry maps each returned `message_id → cwd` and `_last_active_cwd` is set to the response's cwd.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_telegram.py` inside `class TestOnInbound` (it already has `_call_and_capture`; we need the urlopen mock to RETURN a response with a message_id). Add this test plus a helper:

```python
    def _call_with_message_ids(self, response_text, request_summary, ids):
        """urlopen returns sequential message_ids so the registry can record them."""
        seq = list(ids)
        def mock_urlopen(req, timeout=None):
            mid = seq.pop(0) if seq else 1
            class R:
                def read(self_inner):
                    return json.dumps({"ok": True, "result": {"message_id": mid}}).encode()
            return R()
        with patch.object(urllib.request, "urlopen", side_effect=mock_urlopen):
            self.t.on_inbound(response_text, request_summary)
            for th in threading.enumerate():
                if th.daemon and th is not threading.current_thread():
                    th.join(timeout=2)

    def test_on_inbound_records_message_cwd(self):
        self._call_with_message_ids(
            "hello", {"user_text": "hi", "cwd": "/home/u/projX"}, ids=[777])
        self.assertEqual(self.t._cwd_for_message(777), "/home/u/projX")
        self.assertEqual(self.t._last_active_cwd, "/home/u/projX")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestOnInbound.test_on_inbound_records_message_cwd -v`
Expected: FAIL — `_cwd_for_message(777)` returns `None` (message_id not captured).

- [ ] **Step 3: Write minimal implementation**

In `on_inbound`, the `cwd` is computed near line 332 as part of `project = os.path.basename(cwd) ...`. Ensure `cwd` is captured into the closure. Find:

```python
    user_text = request_summary.get("user_text", "")
    cwd = request_summary.get("cwd", "")
    project = os.path.basename(cwd) if cwd else (_project_name or "(unknown project)")
```

`cwd` is already a local; it is captured by the nested `_send`. Now change the send loop (lines 400–407) to read the response and record the message_id:

```python
                try:
                    data = json.dumps(payload).encode()
                    req = urllib.request.Request(
                        tg_url, data=data, headers={"Content-Type": "application/json"},
                    )
                    resp = urllib.request.urlopen(req, timeout=10)
                    try:
                        result = json.loads(resp.read())
                        mid = result.get("result", {}).get("message_id")
                    except Exception:
                        mid = None
                    if cwd:
                        _record_message_target(mid, cwd)
                except Exception as exc:
                    _log(f"ERROR: {exc}")
```

Note: the TTS/voice path also returns early on success; recording there is optional and out of scope (voice messages are rarely replied-to). Text path coverage is sufficient.

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestOnInbound -v`
Expected: PASS (new test plus all existing TestOnInbound tests — the existing ones mock urlopen returning `None`, which the new `try/except` around `resp.read()` tolerates).

- [ ] **Step 5: Commit**

```bash
git add plugins/telegram.py tests/test_telegram.py
git commit -m "feat(telegram): record message_id→cwd when sending notifications"
```

---

### Task 4: cwd override (set/get/clear + persistence)

**Files:**
- Modify: `plugins/telegram.py` (add override section; call `_load_cwd_override()` in `configure()`)
- Test: `tests/test_telegram.py`

**Interfaces:**
- Produces:
  - `_normalize_cwd(path: str) -> str` — expandvars → expanduser → resolve (absolute string).
  - `_set_cwd_override(path: str) -> tuple[bool, str]` — `(ok, resolved_path)`; persists on success.
  - `_get_cwd_override() -> str | None`
  - `_clear_cwd_override() -> None`
  - `_load_cwd_override() -> None` — loads persisted override if still a dir.
  - module global `_cwd_override: str | None`, `_CWD_OVERRIDE_FILE = HOOK_DIR / "cwd_override"`.

- [ ] **Step 1: Write the failing test**

Add a new test class to `tests/test_telegram.py`:

```python
class TestCwdOverride(unittest.TestCase):
    def setUp(self):
        self.t = _load()
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.t.HOOK_DIR = Path(self.tmp)
        self.t._CWD_OVERRIDE_FILE = Path(self.tmp) / "cwd_override"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_set_valid_dir_succeeds_and_persists(self):
        ok, resolved = self.t._set_cwd_override(self.tmp)
        self.assertTrue(ok)
        self.assertEqual(resolved, str(Path(self.tmp).resolve()))
        self.assertEqual(self.t._get_cwd_override(), resolved)
        self.assertEqual(self.t._CWD_OVERRIDE_FILE.read_text().strip(), resolved)

    def test_set_invalid_dir_rejected_state_unchanged(self):
        ok, resolved = self.t._set_cwd_override(self.tmp + "/nope")
        self.assertFalse(ok)
        self.assertIsNone(self.t._get_cwd_override())

    def test_normalize_expands_tilde(self):
        # ~ resolves to the proxy user's home; just assert no literal ~ remains
        norm = self.t._normalize_cwd("~/somewhere")
        self.assertNotIn("~", norm)
        self.assertTrue(norm.startswith("/"))

    def test_clear_removes_override_and_file(self):
        self.t._set_cwd_override(self.tmp)
        self.t._clear_cwd_override()
        self.assertIsNone(self.t._get_cwd_override())
        self.assertFalse(self.t._CWD_OVERRIDE_FILE.exists())

    def test_load_restores_persisted_override(self):
        self.t._CWD_OVERRIDE_FILE.write_text(str(Path(self.tmp).resolve()))
        self.t._load_cwd_override()
        self.assertEqual(self.t._get_cwd_override(), str(Path(self.tmp).resolve()))

    def test_load_ignores_deleted_dir(self):
        self.t._CWD_OVERRIDE_FILE.write_text("/nonexistent/dir/xyz")
        self.t._load_cwd_override()
        self.assertIsNone(self.t._get_cwd_override())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestCwdOverride -v`
Expected: FAIL — `_set_cwd_override` not defined.

- [ ] **Step 3: Write minimal implementation**

Add after the registry section in `plugins/telegram.py`:

```python
# ── Project cwd override (/project sticky target) ────────────────────────

_CWD_OVERRIDE_FILE = HOOK_DIR / "cwd_override"
_cwd_override: str | None = None


def _normalize_cwd(path: str) -> str:
    """expandvars → expanduser → resolve to an absolute path string."""
    expanded = os.path.expandvars(path)
    return str(Path(expanded).expanduser().resolve())


def _set_cwd_override(path: str) -> tuple[bool, str]:
    """Validate and persist a sticky project override. Returns (ok, resolved)."""
    global _cwd_override
    resolved = _normalize_cwd(path)
    if not Path(resolved).is_dir():
        return False, resolved
    _cwd_override = resolved
    try:
        HOOK_DIR.mkdir(parents=True, exist_ok=True)
        _CWD_OVERRIDE_FILE.write_text(resolved)
    except OSError as exc:
        _log(f"could not persist cwd override: {exc}")
    return True, resolved


def _get_cwd_override() -> str | None:
    return _cwd_override


def _clear_cwd_override() -> None:
    global _cwd_override
    _cwd_override = None
    try:
        _CWD_OVERRIDE_FILE.unlink()
    except OSError:
        pass


def _load_cwd_override() -> None:
    """Restore a persisted override if its directory still exists."""
    global _cwd_override
    try:
        val = _CWD_OVERRIDE_FILE.read_text().strip()
    except OSError:
        return
    if val and Path(val).is_dir():
        _cwd_override = val
```

In `configure()`, add a call to `_load_cwd_override()` near the end (after credentials are set, before/after `_start_poller()` — order does not matter):

```python
    _load_cwd_override()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestCwdOverride -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/telegram.py tests/test_telegram.py
git commit -m "feat(telegram): persistent /project cwd override"
```

---

### Task 5: `_parse_project_command`

**Files:**
- Modify: `plugins/telegram.py` (add parser; add `import shlex`)
- Test: `tests/test_telegram.py`

**Interfaces:**
- Produces: `_parse_project_command(text: str) -> tuple[str, str | None, str | None]` returning `(action, path, prompt)` with `action ∈ {"show","set","oneshot","clear"}`.

- [ ] **Step 1: Write the failing test**

```python
class TestParseProjectCommand(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_no_arg_is_show(self):
        self.assertEqual(self.t._parse_project_command("/project"), ("show", None, None))

    def test_path_only_is_set(self):
        self.assertEqual(
            self.t._parse_project_command("/project ~/foo"), ("set", "~/foo", None))

    def test_path_plus_prompt_is_oneshot(self):
        self.assertEqual(
            self.t._parse_project_command("/project ~/foo fix the test"),
            ("oneshot", "~/foo", "fix the test"))

    def test_clear_keyword(self):
        self.assertEqual(self.t._parse_project_command("/project clear"), ("clear", None, None))

    def test_off_keyword(self):
        self.assertEqual(self.t._parse_project_command("/project off"), ("clear", None, None))

    def test_quoted_path_with_spaces(self):
        action, path, prompt = self.t._parse_project_command('/project "~/my proj" do it')
        self.assertEqual(action, "oneshot")
        self.assertEqual(path, "~/my proj")
        self.assertEqual(prompt, "do it")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestParseProjectCommand -v`
Expected: FAIL — `_parse_project_command` not defined.

- [ ] **Step 3: Write minimal implementation**

Add `import shlex` to the imports. Add:

```python
def _parse_project_command(text: str) -> tuple[str, str | None, str | None]:
    """Parse a /project command into (action, path, prompt).

    Forms:
      /project                 → ("show", None, None)
      /project <path>          → ("set", path, None)
      /project <path> <prompt> → ("oneshot", path, prompt)
      /project clear | off     → ("clear", None, None)
    Quoted paths with spaces are honored.
    """
    body = text[len("/project"):].strip()
    if not body:
        return ("show", None, None)
    try:
        tokens = shlex.split(body)
    except ValueError:
        tokens = body.split()
    if not tokens:
        return ("show", None, None)
    path = tokens[0]
    if path.lower() in ("clear", "off"):
        return ("clear", None, None)
    if len(tokens) > 1:
        return ("oneshot", path, " ".join(tokens[1:]))
    return ("set", path, None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestParseProjectCommand -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/telegram.py tests/test_telegram.py
git commit -m "feat(telegram): parse /project command (set/oneshot/clear/show)"
```

---

### Task 6: Disk inbox (put/list/complete/fail)

**Files:**
- Modify: `plugins/telegram.py` (add inbox section)
- Test: `tests/test_telegram.py`

**Interfaces:**
- Produces:
  - `_inbox_put(text: str, cwd: str) -> Path`
  - `_inbox_list() -> list[Path]` (sorted oldest-first)
  - `_inbox_complete(path: Path) -> None`
  - `_inbox_fail(path: Path) -> None` (moves to `failed/`)
  - module globals `_INBOX_DIR = HOOK_DIR / "session-inbox"`, `_INBOX_FAILED_DIR = _INBOX_DIR / "failed"`.

- [ ] **Step 1: Write the failing test**

```python
class TestInbox(unittest.TestCase):
    def setUp(self):
        self.t = _load()
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.t.HOOK_DIR = Path(self.tmp)
        self.t._INBOX_DIR = Path(self.tmp) / "session-inbox"
        self.t._INBOX_FAILED_DIR = self.t._INBOX_DIR / "failed"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_put_creates_item(self):
        p = self.t._inbox_put("hello", "/home/u/p")
        self.assertTrue(p.exists())
        info = json.loads(p.read_text())
        self.assertEqual(info["text"], "hello")
        self.assertEqual(info["cwd"], "/home/u/p")
        self.assertEqual(info["attempts"], 0)

    def test_list_returns_items_sorted(self):
        a = self.t._inbox_put("1", "/p")
        b = self.t._inbox_put("2", "/p")
        listed = self.t._inbox_list()
        self.assertEqual(listed, sorted([a, b]))

    def test_complete_removes_item(self):
        p = self.t._inbox_put("x", "/p")
        self.t._inbox_complete(p)
        self.assertFalse(p.exists())

    def test_fail_moves_to_failed_dir(self):
        p = self.t._inbox_put("x", "/p")
        self.t._inbox_fail(p)
        self.assertFalse(p.exists())
        moved = self.t._INBOX_FAILED_DIR / p.name
        self.assertTrue(moved.exists())

    def test_list_empty_when_no_dir(self):
        self.assertEqual(self.t._inbox_list(), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestInbox -v`
Expected: FAIL — `_inbox_put` not defined.

- [ ] **Step 3: Write minimal implementation**

Add:

```python
# ── Session delivery inbox ───────────────────────────────────────────────

_INBOX_DIR = HOOK_DIR / "session-inbox"
_INBOX_FAILED_DIR = _INBOX_DIR / "failed"


def _inbox_put(text: str, cwd: str) -> Path:
    """Write a delivery item to the durable inbox. Returns its path."""
    _INBOX_DIR.mkdir(parents=True, exist_ok=True)
    path = _INBOX_DIR / f"{time.time_ns()}.json"
    path.write_text(json.dumps({
        "text": text, "cwd": cwd, "received_at": time.time(), "attempts": 0,
    }))
    return path


def _inbox_list() -> list[Path]:
    if not _INBOX_DIR.exists():
        return []
    return sorted(p for p in _INBOX_DIR.iterdir() if p.suffix == ".json")


def _inbox_complete(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _inbox_fail(path: Path) -> None:
    try:
        _INBOX_FAILED_DIR.mkdir(parents=True, exist_ok=True)
        path.rename(_INBOX_FAILED_DIR / path.name)
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestInbox -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/telegram.py tests/test_telegram.py
git commit -m "feat(telegram): durable session-delivery inbox"
```

---

### Task 7: Target resolution (`_resolve_target`)

**Files:**
- Modify: `plugins/telegram.py` (add resolver)
- Test: `tests/test_telegram.py`

**Interfaces:**
- Consumes: `_cwd_for_message` (Task 2), `_get_cwd_override` (Task 4), `_last_active_cwd`.
- Produces: `_resolve_target(msg: dict) -> str | None` with precedence native-reply > sticky override > last-active.

- [ ] **Step 1: Write the failing test**

```python
class TestResolveTarget(unittest.TestCase):
    def setUp(self):
        self.t = _load()

    def test_native_reply_wins(self):
        self.t._record_message_target(5, "/p/replytarget")
        self.t._cwd_override = "/p/override"
        self.t._last_active_cwd = "/p/last"
        msg = {"reply_to_message": {"from": {"is_bot": True}, "message_id": 5}}
        self.assertEqual(self.t._resolve_target(msg), "/p/replytarget")

    def test_override_used_when_no_reply(self):
        self.t._cwd_override = "/p/override"
        self.t._last_active_cwd = "/p/last"
        self.assertEqual(self.t._resolve_target({}), "/p/override")

    def test_last_active_when_no_override(self):
        self.t._last_active_cwd = "/p/last"
        self.assertEqual(self.t._resolve_target({}), "/p/last")

    def test_none_when_nothing_known(self):
        self.assertIsNone(self.t._resolve_target({}))

    def test_reply_to_unknown_message_falls_through(self):
        self.t._last_active_cwd = "/p/last"
        msg = {"reply_to_message": {"from": {"is_bot": True}, "message_id": 999}}
        self.assertEqual(self.t._resolve_target(msg), "/p/last")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestResolveTarget -v`
Expected: FAIL — `_resolve_target` not defined.

- [ ] **Step 3: Write minimal implementation**

```python
def _resolve_target(msg: dict) -> str | None:
    """Resolve the target project cwd for an incoming message.

    Precedence: native reply-to-bot → /project override → last-active.
    """
    reply_to = msg.get("reply_to_message", {})
    if reply_to.get("from", {}).get("is_bot"):
        cwd = _cwd_for_message(reply_to.get("message_id"))
        if cwd:
            return cwd
    if _cwd_override:
        return _cwd_override
    return _last_active_cwd
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestResolveTarget -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/telegram.py tests/test_telegram.py
git commit -m "feat(telegram): target cwd resolution with precedence"
```

---

### Task 8: `/project` command handling in `_handle_text_message`

**Files:**
- Modify: `plugins/telegram.py` (`_handle_text_message`, insert after the `/mode` block near line 714)
- Test: `tests/test_telegram.py`

**Interfaces:**
- Consumes: `_parse_project_command`, `_set_cwd_override`, `_clear_cwd_override`, `_get_cwd_override`, `_recent_cwds`, `_normalize_cwd`, `_inbox_put`, `_delivery_mode`, `_pending_replies`.
- Produces: `/project` handled and returns early; does not fall through to plain-message logic.

- [ ] **Step 1: Write the failing test**

```python
class TestProjectCommand(unittest.TestCase):
    def setUp(self):
        self.t = _load()
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.t.HOOK_DIR = Path(self.tmp)
        self.t._CWD_OVERRIDE_FILE = Path(self.tmp) / "cwd_override"
        self.t._INBOX_DIR = Path(self.tmp) / "session-inbox"
        self.t._INBOX_FAILED_DIR = self.t._INBOX_DIR / "failed"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_set_override_valid(self):
        with patch("urllib.request.urlopen"):
            self.t._handle_text_message({"text": f"/project {self.tmp}"}, "tok", "chat")
        self.assertEqual(self.t._get_cwd_override(), str(Path(self.tmp).resolve()))

    def test_set_override_invalid_rejected(self):
        with patch("urllib.request.urlopen"):
            self.t._handle_text_message(
                {"text": f"/project {self.tmp}/nope"}, "tok", "chat")
        self.assertIsNone(self.t._get_cwd_override())

    def test_clear_override(self):
        self.t._set_cwd_override(self.tmp)
        with patch("urllib.request.urlopen"):
            self.t._handle_text_message({"text": "/project clear"}, "tok", "chat")
        self.assertIsNone(self.t._get_cwd_override())

    def test_oneshot_resume_mode_writes_inbox(self):
        self.t._delivery_mode = "resume"
        with patch("urllib.request.urlopen"):
            self.t._handle_text_message(
                {"text": f"/project {self.tmp} do the thing"}, "tok", "chat")
        items = self.t._inbox_list()
        self.assertEqual(len(items), 1)
        info = json.loads(items[0].read_text())
        self.assertEqual(info["text"], "do the thing")
        self.assertEqual(info["cwd"], str(Path(self.tmp).resolve()))
        # oneshot must NOT change sticky override
        self.assertIsNone(self.t._get_cwd_override())

    def test_show_does_not_crash(self):
        with patch("urllib.request.urlopen") as m:
            self.t._handle_text_message({"text": "/project"}, "tok", "chat")
        self.assertTrue(m.called)

    def test_project_not_treated_as_plain_message(self):
        with patch("urllib.request.urlopen"):
            self.t._handle_text_message({"text": "/project clear"}, "tok", "chat")
        self.assertEqual(self.t._pending_replies, [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestProjectCommand -v`
Expected: FAIL — `/project` falls through; `test_set_override_valid` fails (override is None) and `test_oneshot_resume_mode_writes_inbox` fails (no inbox item).

- [ ] **Step 3: Write minimal implementation**

In `_handle_text_message`, immediately AFTER the `/mode` handling block (which ends with its `return` near line 714) and BEFORE the `_log(f"text msg received...` line, insert:

```python
    # /project command — project targeting
    if text.lower() == "/project" or text.lower().startswith("/project "):
        action, path, prompt = _parse_project_command(text)
        if action == "show":
            ov = _get_cwd_override()
            effective = ov or _last_active_cwd or "(none)"
            lines = [f"Target: {effective}", f"Override: {ov or '(none)'}"]
            recents = _recent_cwds()
            if recents:
                lines.append("Recent:\n" + "\n".join(f"• {c}" for c in recents))
            _send_text(token, chat_id, "\n".join(lines))
            return
        if action == "clear":
            _clear_cwd_override()
            _send_text(token, chat_id, "✅ Override cleared")
            return
        if action == "set":
            ok, resolved = _set_cwd_override(path)
            if ok:
                _send_text(token, chat_id, f"✅ Project: {resolved}")
            else:
                _send_text(token, chat_id, f"⚠ Not a directory: {resolved}")
            return
        if action == "oneshot":
            resolved = _normalize_cwd(path)
            if not Path(resolved).is_dir():
                _send_text(token, chat_id, f"⚠ Not a directory: {resolved}")
                return
            if _delivery_mode == "resume":
                _inbox_put(prompt, resolved)
                _send_text(token, chat_id, f"✅ Sent to {os.path.basename(resolved)}")
            else:
                with _pending_replies_lock:
                    _pending_replies.append(prompt)
                _send_text(token, chat_id, "✅ Queued (inject mode — lands on next request)")
            return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestProjectCommand -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/telegram.py tests/test_telegram.py
git commit -m "feat(telegram): handle /project command in message handler"
```

---

### Task 9: Plain-message routing (resume → inbox / inject → queue)

**Files:**
- Modify: `plugins/telegram.py` (`_handle_text_message`, the fallback block added in v0.5.0 near the end of the function)
- Test: `tests/test_telegram.py`

**Interfaces:**
- Consumes: `_resolve_target`, `_inbox_put`, `_delivery_mode`, `_pending_replies`.
- Produces: in `resume` mode with a resolved target, a plain message becomes an inbox item; otherwise it queues in `_pending_replies` (unchanged inject behavior).

- [ ] **Step 1: Write the failing test**

```python
class TestPlainMessageRouting(unittest.TestCase):
    def setUp(self):
        self.t = _load()
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.t.HOOK_DIR = Path(self.tmp)
        self.t._INBOX_DIR = Path(self.tmp) / "session-inbox"
        self.t._INBOX_FAILED_DIR = self.t._INBOX_DIR / "failed"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resume_mode_routes_to_inbox(self):
        self.t._delivery_mode = "resume"
        self.t._last_active_cwd = "/home/u/projY"
        with patch("urllib.request.urlopen"):
            self.t._handle_text_message({"text": "run the tests"}, "tok", "chat")
        items = self.t._inbox_list()
        self.assertEqual(len(items), 1)
        info = json.loads(items[0].read_text())
        self.assertEqual(info["text"], "run the tests")
        self.assertEqual(info["cwd"], "/home/u/projY")
        self.assertEqual(self.t._pending_replies, [])

    def test_inject_mode_still_queues(self):
        self.t._delivery_mode = "inject"
        with patch("urllib.request.urlopen"):
            self.t._handle_text_message({"text": "hello"}, "tok", "chat")
        self.assertEqual(self.t._pending_replies, ["hello"])
        self.assertEqual(self.t._inbox_list(), [])

    def test_resume_mode_no_target_falls_back_to_queue(self):
        self.t._delivery_mode = "resume"
        self.t._last_active_cwd = None
        with patch("urllib.request.urlopen"):
            self.t._handle_text_message({"text": "hello"}, "tok", "chat")
        self.assertEqual(self.t._pending_replies, ["hello"])
        self.assertEqual(self.t._inbox_list(), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestPlainMessageRouting -v`
Expected: FAIL — `test_resume_mode_routes_to_inbox` fails (message went to `_pending_replies`, inbox empty).

- [ ] **Step 3: Write minimal implementation**

Replace the v0.5.0 fallback block at the end of `_handle_text_message`:

```python
    # Fallback: any other plain message in the channel is forwarded to the
    # running Claude Code session. It's queued here and injected into the
    # session's next outbound request by on_outbound() as a <system-reminder>.
    # No Reply button or native reply required — just type and send.
    with _pending_replies_lock:
        _pending_replies.append(text)
    _send_text(token, chat_id, "✅ Sent to session")
    _log(f"message forwarded to session ({len(text)} chars)")
```

with:

```python
    # Fallback: any other plain message is forwarded to the running session.
    # In "resume" mode it is delivered to the resolved project via the inbox
    # (wakes idle sessions). In "inject" mode (or when no target is known) it
    # is queued for on_outbound() to inject into the session's next request.
    target = _resolve_target(msg)
    if _delivery_mode == "resume" and target:
        _inbox_put(text, target)
        _send_text(token, chat_id, f"✅ Sent to {os.path.basename(target)}")
        _log(f"message → inbox for {target} ({len(text)} chars)")
        return
    with _pending_replies_lock:
        _pending_replies.append(text)
    _send_text(token, chat_id, "✅ Sent to session")
    _log(f"message forwarded to session ({len(text)} chars)")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestPlainMessageRouting tests.test_telegram.TestHandleTextMessage -v`
Expected: PASS (new routing tests AND the existing `TestHandleTextMessage` tests — inject is still the default there).

- [ ] **Step 5: Commit**

```bash
git add plugins/telegram.py tests/test_telegram.py
git commit -m "feat(telegram): route plain messages to inbox in resume mode"
```

---

### Task 10: Deliverer (`_deliver_one`) + `on_tick` drain

**Files:**
- Modify: `plugins/telegram.py` (add deliverer section + `on_tick`)
- Test: `tests/test_telegram.py`

**Interfaces:**
- Consumes: `_inbox_list`, `_inbox_complete`, `_inbox_fail`, `_send_message`, `_claude_bin`, `_resume_timeout`, `_resume_max_attempts`.
- Produces:
  - `_deliver_one(item_path: Path) -> None` — runs `claude --continue --print` synchronously; completes/retries/fails.
  - `on_tick(now: float) -> None` — poller watchdog (Task 11 extends) + per-cwd guarded inbox drain.
  - module globals `_inflight_lock`, `_inflight_cwds: set[str]`.

- [ ] **Step 1: Write the failing test**

```python
class TestDeliverer(unittest.TestCase):
    def setUp(self):
        self.t = _load()
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.t.HOOK_DIR = Path(self.tmp)
        self.t._INBOX_DIR = Path(self.tmp) / "session-inbox"
        self.t._INBOX_FAILED_DIR = self.t._INBOX_DIR / "failed"
        self.t._claude_bin = "claude"
        self.t._resume_timeout = 30
        self.t._resume_max_attempts = 3
        # target cwd must exist
        self.cwd = str(Path(self.tmp).resolve())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_deliver_success_completes_item(self):
        p = self.t._inbox_put("do it", self.cwd)
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")) as m:
            self.t._deliver_one(p)
        self.assertFalse(p.exists())  # completed
        args = m.call_args[0][0]
        self.assertEqual(args[0], "claude")
        self.assertIn("--continue", args)
        self.assertIn("--print", args)
        self.assertEqual(self.t._INBOX_FAILED_DIR.exists() and
                         list(self.t._INBOX_FAILED_DIR.iterdir()) or [], [])

    def test_deliver_runs_in_target_cwd(self):
        p = self.t._inbox_put("x", self.cwd)
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")) as m:
            self.t._deliver_one(p)
        self.assertEqual(m.call_args.kwargs["cwd"], self.cwd)

    def test_deliver_failure_increments_attempts(self):
        p = self.t._inbox_put("x", self.cwd)
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="boom")):
            self.t._deliver_one(p)
        self.assertTrue(p.exists())  # not completed
        self.assertEqual(json.loads(p.read_text())["attempts"], 1)

    def test_deliver_gives_up_after_max_attempts(self):
        p = self.t._inbox_put("x", self.cwd)
        # pre-set attempts to max-1 so this run trips the limit
        info = json.loads(p.read_text()); info["attempts"] = 2; p.write_text(json.dumps(info))
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="boom")), \
             patch.object(self.t, "_send_message"):
            self.t._deliver_one(p)
        self.assertFalse(p.exists())
        self.assertTrue((self.t._INBOX_FAILED_DIR / p.name).exists())

    def test_deliver_missing_cwd_fails_fast(self):
        p = self.t._inbox_put("x", self.tmp + "/gone")
        with patch.object(self.t, "_send_message"):
            self.t._deliver_one(p)
        self.assertTrue((self.t._INBOX_FAILED_DIR / p.name).exists())

    def test_on_tick_drains_inbox(self):
        self.t._inbox_put("a", self.cwd)
        # make delivery synchronous by patching the worker thread to run inline
        with patch.object(self.t.threading, "Thread") as MockThread, \
             patch.object(self.t, "_deliver_one") as mock_deliver:
            MockThread.side_effect = lambda target, daemon=None: \
                type("T", (), {"start": lambda s: target()})()
            self.t.on_tick(0.0)
        mock_deliver.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestDeliverer -v`
Expected: FAIL — `_deliver_one` / `on_tick` not defined.

- [ ] **Step 3: Write minimal implementation**

Add a deliverer section near the bottom of `plugins/telegram.py` (before `# ── Helpers ──`):

```python
# ── Session deliverer (resume mode) ──────────────────────────────────────

_inflight_lock = threading.Lock()
_inflight_cwds: set[str] = set()


def _deliver_one(item_path: Path) -> None:
    """Deliver one inbox item via `claude --continue --print` in its cwd."""
    try:
        info = json.loads(item_path.read_text())
    except (OSError, json.JSONDecodeError):
        _inbox_fail(item_path)
        return

    cwd = info.get("cwd", "")
    text = info.get("text", "")
    attempts = int(info.get("attempts", 0))

    if not cwd or not Path(cwd).is_dir():
        _inbox_fail(item_path)
        _send_message(
            f"⚠ Cannot deliver to {cwd or '(unknown)'}: not a directory. "
            f"Use /project clear or set a valid path."
        )
        return

    err = ""
    try:
        proc = subprocess.run(
            [_claude_bin, "--continue", "--print", text],
            cwd=cwd, capture_output=True, text=True, timeout=_resume_timeout,
        )
        if proc.returncode == 0:
            _inbox_complete(item_path)
            _log(f"delivered to {cwd} ({len(text)} chars)")
            return
        err = (proc.stderr or "").strip()[:300]
    except FileNotFoundError:
        err = f"claude binary not found: {_claude_bin}"
    except subprocess.TimeoutExpired:
        err = f"timed out after {_resume_timeout}s"
    except Exception as exc:  # noqa: BLE001 — report any spawn failure
        err = str(exc)

    attempts += 1
    info["attempts"] = attempts
    try:
        item_path.write_text(json.dumps(info))
    except OSError:
        pass

    if attempts >= _resume_max_attempts:
        _inbox_fail(item_path)
        _send_message(f"⚠ Delivery to {cwd} failed after {attempts} attempts: {err}")
    else:
        _log(f"delivery attempt {attempts} to {cwd} failed: {err}")


def on_tick(now: float) -> None:
    """Called periodically by the proxy's supervised monitor loop.

    Drains the delivery inbox, spawning `claude --continue` per item with a
    per-cwd in-flight guard so a slow delivery does not pile up across ticks.
    (Poller watchdog is added in a later task.)
    """
    if _delivery_mode != "resume":
        return
    for item_path in _inbox_list():
        try:
            info = json.loads(item_path.read_text())
        except (OSError, json.JSONDecodeError):
            _inbox_fail(item_path)
            continue
        cwd = info.get("cwd", "")
        with _inflight_lock:
            if cwd in _inflight_cwds:
                continue
            _inflight_cwds.add(cwd)

        def _run(p=item_path, c=cwd):
            try:
                _deliver_one(p)
            finally:
                with _inflight_lock:
                    _inflight_cwds.discard(c)

        threading.Thread(target=_run, daemon=True).start()
```

Rationale for the per-item worker thread: `_deliver_one` blocks up to `_resume_timeout`; running it inline in `on_tick` would stall the shared monitor loop. The thread is ephemeral (not a scheduler); durability lives in the disk inbox, and the supervised tick re-drives any item that didn't complete.

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestDeliverer -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/telegram.py tests/test_telegram.py
git commit -m "feat(telegram): inbox deliverer via claude --continue + on_tick drain"
```

---

### Task 11: Poller watchdog in `on_tick`

**Files:**
- Modify: `plugins/telegram.py` (`on_tick`, prepend watchdog)
- Test: `tests/test_telegram.py`

**Interfaces:**
- Consumes: `_poller_thread`, `_start_poller`, `_bot_token`, `_chat_id`.
- Produces: `on_tick` respawns a dead poller when credentials are present.

- [ ] **Step 1: Write the failing test**

```python
class TestPollerWatchdog(unittest.TestCase):
    def setUp(self):
        self.t = _load()
        self.t._bot_token = "tok"
        self.t._chat_id = "chat"
        self.t._delivery_mode = "inject"  # so the drain part is skipped

    def test_respawns_dead_poller(self):
        self.t._poller_thread = None
        with patch.object(self.t, "_start_poller") as mock_start:
            self.t.on_tick(0.0)
        mock_start.assert_called_once()

    def test_does_not_respawn_live_poller(self):
        class FakeThread:
            def is_alive(self):
                return True
        self.t._poller_thread = FakeThread()
        with patch.object(self.t, "_start_poller") as mock_start:
            self.t.on_tick(0.0)
        mock_start.assert_not_called()

    def test_no_respawn_without_credentials(self):
        self.t._bot_token = None
        self.t._poller_thread = None
        with patch.object(self.t, "_start_poller") as mock_start:
            self.t.on_tick(0.0)
        mock_start.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestPollerWatchdog -v`
Expected: FAIL — `test_respawns_dead_poller` fails (no watchdog yet; `_start_poller` not called).

- [ ] **Step 3: Write minimal implementation**

At the TOP of `on_tick`, before the `if _delivery_mode != "resume":` line, insert:

```python
    # Poller watchdog: respawn the long-poll thread if it has died.
    if _bot_token and _chat_id:
        if _poller_thread is None or not _poller_thread.is_alive():
            _log("poller thread not alive — respawning")
            _start_poller()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate; python3 -m unittest tests.test_telegram.TestPollerWatchdog -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/telegram.py tests/test_telegram.py
git commit -m "feat(telegram): on_tick poller watchdog respawns dead poller"
```

---

### Task 12: Monitor `on_tick` carrier + proxy fan-out

**Files:**
- Modify: `monitor.py` (`ResourceMonitor.start`, near line 147)
- Modify: `proxy.py` (`main()`, near lines 1454–1470)
- Test: `tests/test_monitor.py`

**Interfaces:**
- Consumes: plugin `on_tick(now: float)` (Tasks 10–11).
- Produces: `ResourceMonitor.start(on_recycle, interval_s=60.0, on_tick=None, tick_interval_s=3.0)`. When `on_tick` is provided, the loop wakes every `tick_interval_s`, calls `on_tick` each wake, and evaluates recycle every ~`interval_s`. When `on_tick` is None, behavior is unchanged (legacy cadence).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_monitor.py`:

```python
# A metrics dict that never breaches the default Thresholds.
_SAFE_METRICS = {"rss_mb": 1, "threads": 1, "fds": 1, "fd_limit": 1000}


def test_monitor_fires_on_tick_each_interval():
    import threading as _th
    from monitor import ResourceMonitor
    mon = ResourceMonitor(metrics_source=lambda: dict(_SAFE_METRICS))
    ticks = []
    ev = _th.Event()

    def on_tick(now):
        ticks.append(now)
        if len(ticks) >= 2:
            ev.set()

    mon.start(on_recycle=lambda b: None, interval_s=60.0,
              on_tick=on_tick, tick_interval_s=0.01)
    assert ev.wait(timeout=2.0), "on_tick not fired at least twice"
    mon.stop()


def test_on_tick_exception_does_not_kill_loop():
    import threading as _th
    from monitor import ResourceMonitor
    mon = ResourceMonitor(metrics_source=lambda: dict(_SAFE_METRICS))
    count = {"n": 0}
    ev = _th.Event()

    def on_tick(now):
        count["n"] += 1
        if count["n"] == 1:
            raise RuntimeError("boom")
        ev.set()

    mon.start(on_recycle=lambda b: None, interval_s=60.0,
              on_tick=on_tick, tick_interval_s=0.01)
    assert ev.wait(timeout=2.0), "loop died after on_tick raised"
    mon.stop()
```

Metrics keys are `rss_mb`, `threads`, `fds`, `fd_limit` (see `evaluate_thresholds`);
`_SAFE_METRICS` stays well under the default `Thresholds` so no recycle fires.

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate; python3 -m pytest tests/test_monitor.py::test_monitor_fires_on_tick_each_interval -q`
Expected: FAIL — `start()` got an unexpected keyword argument `on_tick`.

- [ ] **Step 3: Write minimal implementation**

In `monitor.py`, replace the `start` method body's loop with a branch on `on_tick`:

```python
    def start(self, on_recycle: Callable[[Breach], None], interval_s: float = 60.0,
              on_tick: Optional[Callable[[float], None]] = None,
              tick_interval_s: float = 3.0) -> None:
        if hasattr(self, "_stop_event") and not self._stop_event.is_set():
            raise RuntimeError("ResourceMonitor is already running; call stop() first")
        self._stop_event = threading.Event()

        def _eval_recycle():
            breach = self.should_recycle()
            if breach is not None:
                self.mark_recycled(breach.reason)
                try:
                    on_recycle(breach)
                except Exception as exc:
                    print(f"[monitor] on_recycle callback failed: {exc}", file=sys.stderr)

        def loop():
            if on_tick is None:
                while not self._stop_event.wait(interval_s):
                    _eval_recycle()
                return
            ticks_per_eval = max(1, round(interval_s / tick_interval_s))
            n = 0
            while not self._stop_event.wait(tick_interval_s):
                try:
                    on_tick(self._clock())
                except Exception as exc:
                    print(f"[monitor] on_tick callback failed: {exc}", file=sys.stderr)
                n += 1
                if n >= ticks_per_eval:
                    n = 0
                    _eval_recycle()

        self._thread = threading.Thread(target=loop, daemon=True, name="ResourceMonitor")
        self._thread.start()
```

(`monitor.py` already has `from typing import Callable, Optional` — no import change needed.)

In `proxy.py` `main()`, add a plugin fan-out and pass it to `start()`. Replace the existing `resource_monitor.start(on_recycle=_on_recycle, interval_s=60.0)` (line 1470) with:

```python
    def _on_tick(now):
        for p in plugin_mgr.plugins:
            tick = getattr(p, "on_tick", None)
            if callable(tick):
                try:
                    tick(now)
                except Exception as exc:
                    print(f"[monitor] plugin on_tick failed: {exc}", file=sys.stderr, flush=True)

    resource_monitor.start(
        on_recycle=_on_recycle, interval_s=60.0,
        on_tick=_on_tick, tick_interval_s=3.0,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate; python3 -m pytest tests/test_monitor.py -q`
Expected: PASS (new tests + all existing monitor tests, since `on_tick=None` preserves legacy behavior).

- [ ] **Step 5: Commit**

```bash
git add monitor.py proxy.py tests/test_monitor.py
git commit -m "feat(monitor): on_tick carrier + proxy plugin fan-out"
```

---

### Task 13: Config docs, version bump, full-suite verification

**Files:**
- Modify: `plugins/telegram.toml`
- Modify: `plugins/telegram.py` (`plugin_info` version)
- Test: full suite

**Interfaces:** none new.

- [ ] **Step 1: Update `plugins/telegram.toml`**

Add after the `approval_scanner` block:

```toml
# ── Session delivery ─────────────────────────────────────────────────────
# How messages typed in the channel reach the running Claude Code session:
#   "inject" (default) — queued and injected into the session's NEXT request
#                        (works while a session is active; does not wake idle)
#   "resume"           — actively delivered to the project via
#                        `claude --continue`, waking idle sessions and
#                        preserving conversation context
delivery_mode = "inject"

# Path to the Claude Code CLI (the proxy daemon's PATH may differ from your
# shell; use an absolute path if `claude` is not found):
# claude_bin = "claude"

# Seconds before a headless delivery run is killed:
# resume_timeout = 300

# Max delivery attempts before moving a message to session-inbox/failed/:
# resume_max_attempts = 3

# Telegram commands (sent in the channel):
#   /project                       show current target + override + recent cwds
#   /project <path>                set sticky target (validated dir on host)
#   /project <path> <prompt>       one-shot: deliver <prompt> to <path> now
#   /project clear | /project off  clear sticky target
# Plain (non-command) messages go to the default target:
#   native reply-to > /project override > last-active project.
```

- [ ] **Step 2: Bump the plugin version**

In `plugin_info()` change `"version": "0.5.0"` to `"version": "0.6.0"`.

- [ ] **Step 3: Run the full telegram + monitor suites**

Run: `. .venv/bin/activate; python3 -m pytest tests/test_telegram.py tests/test_monitor.py -q 2>&1 | tail -15`
Expected: all tests pass.

- [ ] **Step 4: Run the broader suite to catch regressions**

Run: `. .venv/bin/activate; python3 -m pytest tests/test_proxy.py tests/test_status_endpoint.py -q 2>&1 | tail -15`
Expected: all pass (these exercise the monitor/proxy wiring touched in Task 12).

- [ ] **Step 5: Commit**

```bash
git add plugins/telegram.toml plugins/telegram.py
git commit -m "docs(telegram): document delivery_mode + /project; bump to 0.6.0"
```

---

## Self-Review

**Spec coverage:**
- Project-tag registry → Tasks 2, 3. ✓
- Target resolution precedence (reply > override > last-active) → Task 7. ✓
- Disk inbox (put/list/complete/fail, `failed/`) → Task 6. ✓
- Deliverer `claude --continue` + per-cwd in-flight guard → Task 10. ✓
- Poller watchdog → Task 11. ✓
- `on_tick` carrier on monitor loop + recycle still gated to ~60s → Task 12. ✓
- `/project` command (show/set/oneshot/clear, normalization, persistence) → Tasks 4, 5, 8. ✓
- Path normalization (`~`, `$VAR`, relative) → Task 4 (`_normalize_cwd`), tested Task 4. ✓
- Config keys (`delivery_mode`, `claude_bin`, `resume_timeout`, `resume_max_attempts`) → Tasks 1, 13. ✓
- Backward-compat inject default → Tasks 1, 9; existing tests rerun in Tasks 9, 13. ✓
- Error-handling rows (invalid `/project`, missing cwd, timeout, retries, recycle mid-delivery, throwing `on_tick`) → Tasks 8, 10, 12. ✓

**Deferred (not in plan, per spec):** killing a live TUI; true per-session `session_id` targeting. ✓

**Type consistency:** `_resolve_target(msg)`, `_record_message_target(message_id, cwd)`, `_cwd_for_message(message_id)`, `_inbox_put(text, cwd) -> Path`, `_set_cwd_override(path) -> (bool, str)`, `_parse_project_command(text) -> (action, path, prompt)`, `on_tick(now)`, `start(..., on_tick, tick_interval_s)` — names/signatures consistent across Tasks 1–13.

**Known limitation to document at release:** two live sessions sharing one cwd → `claude --continue` continues the most recent transcript (mitigated by `/project`).

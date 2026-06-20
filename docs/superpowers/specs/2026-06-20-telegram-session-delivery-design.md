# Telegram → Session Delivery (Approach A: headless `--continue` deliverer)

**Date:** 2026-06-20
**Status:** Design — awaiting review
**Component:** `plugins/telegram.py`, `proxy.py`, `monitor.py`

## Problem

Today, a Telegram message can reach a running Claude Code session only *passively*:
the telegram plugin queues the text in the in-memory list `_pending_replies`, and
`on_outbound()` injects it into the session's **next** API request as a
`<system-reminder>`. This works while a session is mid-task, but it **cannot wake a
fully idle session** — an idle session makes no API requests, so there is nothing to
inject into.

We want: a message typed in the Telegram channel is actively delivered to the
relevant project's session, **preserving that project's conversation context**, even
when the session is idle. The reply Claude produces should come back to Telegram.

## Constraints discovered in the codebase

- **The proxy never sees the CLI `session_id`.** It only scrapes the **project working
  directory (`cwd`)** from Claude Code's system prompt (`_extract_cwd`, `proxy.py`).
  The `session_id` is available only to the PreToolUse hook, not the proxy. Therefore
  "target a session" is in practice "target a project directory".
- **Continuing a conversation without losing context** is done with
  `claude --continue --print "<text>"` run **in that project directory**: it appends to
  the most-recent transcript for that cwd. (`--resume <id>` is the per-session variant,
  but we do not have the id at proxy level.)
- **The Telegram poller already runs independently.** `configure()` starts a daemon
  thread (`tg-poller`) running `_poll_loop()`, which long-polls `getUpdates(timeout=30)`
  continuously, independent of any request traffic. Verified on the live proxy (named
  thread present, two ESTABLISHED sockets to Telegram DCs at idle, `callback poller
  started` in the log). So *receipt* of messages needs no trigger.
- **The proxy core already owns one supervised periodic ticker.**
  `ResourceMonitor.start()` (`monitor.py`) runs a single daemon loop
  (`while not self._stop_event.wait(interval_s)`) that every 60s evaluates health and
  **fans out to every plugin** (calls `on_monitor_recycle`). The *process* is supervised
  by systemd (`Restart=always`) / launchd (`KeepAlive=True`).
- **The `claude` binary may not be on the daemon's PATH** (found at
  `~/.local/bin/claude` on this host). Path must be configurable.

## Design principle

The plugin owns **no scheduler of its own** for delivery. It reuses:
1. the `tg-poller` thread it already needs for Telegram (receipt only), and
2. the existing supervised monitor loop, generalized to fire a new `on_tick()` plugin
   hook (delivery + retry + watchdog).

This avoids a fragile plugin-owned deliverer thread that the process supervisor cannot
see (the supervisor restarts on *process* death, not *thread* death).

## Architecture

```
Telegram reply
   │
   ▼
tg-poller thread (existing)
   │  resolve target cwd (registry / native-reply / last-active)
   ▼
disk inbox:  telegram-hook/session-inbox/<ns>.json  = {text, cwd, received_at, attempts}
   │
   ▼
shared monitor loop  ──fires──▶  on_tick(now)  ──▶  telegram.on_tick():
   (proxy core, supervised)                          drain inbox →
                                                      spawn `claude --continue -p` in cwd
                                                      (per-cwd in-flight guard)
   │
   ▼
claude headless run's API calls flow back through the proxy
   │
   ▼
on_inbound() (existing) ──▶ Claude's answer posted to Telegram
```

### Components (each independently testable)

1. **Project-tag registry** (`message_id → cwd`).
   Built where notifications are sent. `on_inbound`'s `_send()` currently discards the
   `sendMessage` response (`telegram.py:405`); capture each chunk's returned
   `message_id` and record `message_id → cwd` in a bounded, lock-guarded dict (cap ~500,
   FIFO eviction). Also maintain `_last_active_cwd`.
   *Unit:* `_record_message_target(message_id, cwd)` / `_resolve_target(reply_to_id) -> cwd|None`.

2. **Target resolution** (in `_handle_text_message` and the reply/option callbacks).
   Precedence, most specific first:
   1. native reply (`reply_to_message.message_id`) → registry lookup → cwd
      (also option/reply button callbacks → their message's id → registry → cwd)
   2. **`/cwd` override** (if set) → override cwd
   3. plain typed message with no override → `_last_active_cwd`
   - unresolved → fall back to `inject` behavior (queue in `_pending_replies`) and tell
     the user it'll land on the session's next request.
   *Unit:* `_resolve_target(msg) -> cwd|None`.

3. **Disk inbox** (`telegram-hook/session-inbox/`).
   In `resume` mode, a resolved message is written as `<time_ns>.json` =
   `{text, cwd, received_at, attempts}`. Durable across proxy recycles (the
   ResourceMonitor can restart the proxy mid-flight; an in-memory queue would drop it).
   `failed/` subdir holds give-ups.
   *Unit:* `_inbox_put(text, cwd)`, `_inbox_list()`, `_inbox_complete(path)`,
   `_inbox_fail(path)`.

4. **Deliverer** = `on_tick()` (no dedicated thread).
   Fired by the shared monitor loop. Drains the inbox; for each item not already
   in-flight for its cwd, spawns `claude --continue --print "<text>"` with `cwd=<cwd>`
   and the proxy environment, serialized per-cwd via an in-flight set. The spawn itself
   is handed to a short-lived `subprocess`/thread so `on_tick` returns promptly; the
   in-flight guard prevents pile-up across ticks.
   - success (exit 0) → `_inbox_complete` (delete). Claude's answer returns to Telegram
     via the existing `on_inbound` path automatically.
   - failure / timeout / missing binary → increment `attempts`; after N (default 3) →
     `_inbox_fail` + post an error message to Telegram.

5. **Poller watchdog** (also in `on_tick()`).
   Check `_poller_thread.is_alive()`; if dead and credentials are present, respawn it
   via `_start_poller()`. This makes the shared supervised tick the watchdog for the
   long-poll thread, closing the "thread silently died" gap.

6. **`/cwd` command — manual project override** (in `_handle_text_message`, alongside
   `/mute` and `/mode`). Lets the user resume a project other than the last-active one —
   the escape hatch for the shared-cwd limitation and for projects with no recent
   notification. Feeds resolution step 2 above.
   - `/cwd <path>` — set a **sticky** override. The path is **normalized before
     validation**: `os.path.expandvars` (for `$HOME`-style vars) → `Path.expanduser`
     (for `~`, resolving to the **proxy daemon's** home, since that user runs
     `claude --continue`) → `Path.resolve` (absolute, symlink-collapsed). The normalized
     absolute path must be an existing directory **on the proxy host**. Invalid → reject
     with a clear Telegram error showing the resolved path; override unchanged. The
     stored override is always the absolute resolved path, so `~`/relative/`$VAR` forms
     all work (e.g. `/cwd ~/project_name`).
   - `/cwd` (no arg) — show the current effective target and override state, plus the
     recently-seen cwds from the registry (so the user knows valid options).
   - `/cwd clear` / `/cwd off` — drop the override; revert to `_last_active_cwd`.
   Persisted to `telegram-hook/cwd_override` so it survives a proxy recycle; loaded on
   `configure()`. Override applies to `resume` mode; in `inject` mode it is accepted and
   shown but only affects behavior once `resume` mode is active.
   *Unit:* `_set_cwd_override(path) -> ok|err`, `_get_cwd_override() -> cwd|None`,
   `_clear_cwd_override()`.

### Carrier: generalize the monitor loop

Add a generic `on_tick(now: float)` plugin hook fired from the existing monitor loop.
To keep delivery responsive without changing health behavior:
- run the loop at a short cadence (`tick_interval_s`, default **3.0s**),
- counter-gate the **recycle evaluation** so it still runs every ~60s.

`on_tick` is fanned out to every plugin that defines it, each call wrapped in
`try/except` (a throwing plugin must not kill the loop or block recycle). Hook is
optional — plugins without it are unaffected.

## Configuration (`telegram.toml`)

```toml
# Delivery mode for messages typed in the channel:
#   "inject"  (default) — queue into the session's NEXT request (current behavior)
#   "resume"            — actively deliver to the project via `claude --continue`,
#                         waking idle sessions and preserving context
delivery_mode = "inject"

# Path to the Claude Code CLI (daemon PATH may differ from your shell):
claude_bin = "claude"

# Seconds before a headless delivery run is killed:
resume_timeout = 300

# Max delivery attempts before moving a message to session-inbox/failed/:
resume_max_attempts = 3
```

Proxy-level (optional, `plugins.toml`/env): `tick_interval_s` (default 3.0).

### Telegram commands (handled in `_handle_text_message`)

| Command | Effect |
|---|---|
| `/mute`, `/mute_on`, `/mute_off` | existing — toggle notifications |
| `/mode auto-approve\|ask\|auto-deny` | existing — approval gate mode |
| `/cwd <path>` | **new** — set sticky target-project override (validated dir on proxy host) |
| `/cwd` | **new** — show effective target + override state + recently-seen cwds |
| `/cwd clear` \| `/cwd off` | **new** — clear override, revert to last-active |

Any non-command text continues to be forwarded to the session (inject or resume per
`delivery_mode`).

## Error handling

| Failure | Behavior |
|---|---|
| `claude_bin` not found | error posted to Telegram once per item; item → `failed/` after retries |
| headless run times out | killed at `resume_timeout`; retried up to `resume_max_attempts` |
| target cwd unresolved | fall back to `inject` queue; user told it lands on next request |
| `/cwd <path>` not a dir on proxy host | rejected with Telegram error; override unchanged |
| `/cwd` override set to a dir later deleted | delivery item → `failed/`; user told to `/cwd clear` or set a valid path |
| cwd no longer exists | item → `failed/` with a clear Telegram error |
| proxy recycles mid-delivery | item remains in inbox (not completed); redelivered after restart |
| `on_tick` plugin raises | caught in loop; logged; loop and recycle unaffected |
| poller thread dead | respawned by watchdog on next tick |
| two live sessions share one cwd | `--continue` picks the most recent transcript; documented limitation |

## Testing (TDD)

Unit (pure, no network/subprocess):
- registry: record/resolve, FIFO eviction at cap, last-active tracking
- target resolution precedence: native-reply > `/cwd` override > last-active;
  unresolved → inject fallback
- `/cwd` command: set valid dir succeeds + persists; set non-existent dir rejected,
  override unchanged; `/cwd clear` reverts to last-active; `/cwd` no-arg reports state;
  override loaded from `cwd_override` file on configure()
- `/cwd` path normalization: `~/x`, `$HOME/x`, and relative paths expand to the same
  absolute resolved dir and validate; stored value is the absolute path
- inbox: put/list/complete/fail round-trips; `failed/` move; malformed file skipped
- `on_tick`: drains pending items; per-cwd in-flight guard prevents double-spawn;
  increments attempts on failure; moves to `failed/` after max; respawns dead poller
- monitor loop: `on_tick` fired every tick; recycle eval still gated to ~60s; throwing
  `on_tick` does not break the loop (extend `tests/test_proxy.py` / monitor tests)

Integration (mocked subprocess + urlopen):
- `resume` mode: message → inbox → `on_tick` spawns `claude --continue -p` with correct
  cwd/args; success deletes item
- `inject` mode unchanged (existing tests stay green)
- failure path posts a Telegram error and moves to `failed/`

Mocks limited to process boundaries (`subprocess.run`, `urllib.request.urlopen`,
clock). No mocking of the units under test.

## Backward compatibility

- Default `delivery_mode = "inject"` ⇒ behavior identical to today; all existing
  telegram tests remain valid.
- `on_tick` is an optional hook; plugins and the monitor loop work unchanged without it.
- The earlier "plain message → forwarded" change (v0.5.0) remains the `inject`-mode
  behavior; `resume` mode upgrades *where* that forwarded text is delivered.

## Deferred (YAGNI)

- **Killing a live TUI** on the target dir before resuming. v1 targets headless usage;
  `--continue` appends to the latest transcript. Forced takeover of a live interactive
  session needs PID tracking via a SessionStart hook — add only if real demand appears.
- **True per-session targeting** via a `session_id` registry — needs a hook feeding ids
  to the proxy; the registry is structured (`message_id → cwd`) so this can later become
  `message_id → (cwd, session_id)` without a rewrite.
```

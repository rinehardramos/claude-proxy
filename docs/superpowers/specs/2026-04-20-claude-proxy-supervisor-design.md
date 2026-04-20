# claude-proxy supervisor & resilience — design

**Date:** 2026-04-20
**Status:** Draft (design approved, pre-plan)

## Problem

The proxy dies (crash, inactivity timeout, or bad plugin load) and does not reliably come back. Today's behavior:

- `SessionStart` hook spawns the proxy with `python3 proxy.py --daemon 2>/dev/null || true` — errors silenced, no retry between sessions.
- `_inactivity_watchdog` shuts the proxy down after idle; revival depends on the next SessionStart firing *and* succeeding.
- The LaunchAgent only exports `ANTHROPIC_BASE_URL`; nothing supervises the proxy process itself.
- A dead proxy leaves `ANTHROPIC_BASE_URL` pointing at a closed port, so Claude Code fails until the user works around it (e.g. launching Claude Desktop, which spawns shells without the env var).

**Root cause:** the proxy has no supervisor. Every reliability mechanism in place today is compensating for that gap.

## Goals

- Proxy crashes and intentional recycles are invisible to the user.
- User can tell at a glance whether the proxy is healthy, without reading logs.
- Accumulated weirdness (memory/thread/fd leaks, plugin-reload drift) triggers a clean recycle.
- macOS and Linux supported day one; Windows adapter stubbed for later.

## Non-goals

- Windows service integration (stub only).
- Multi-host / remote proxy supervision.
- Replacing the existing plugin hot-reload mechanism (it works; the problem is process lifecycle).

## Architecture

```
┌─────────────── OS supervisor ───────────────┐
│  launchd (macOS) | systemd --user (Linux)   │
│  KeepAlive=true, ThrottleInterval=5s        │
└────────────────────┬────────────────────────┘
                     │ spawns / respawns
                     ▼
              proxy.py (foreground)
                     │
    ┌────────────────┼────────────────┐
    │                │                │
 PluginManager   HealthProbe    ResourceMonitor
 (file-watcher)  (/health)      (self-recycle)
```

### Key moves

1. **Supervisor owns lifecycle.** `proxy.py` no longer daemonizes under supervision; supervisor runs it foreground and captures logs.
2. **SessionStart hook becomes a health probe**, not a launcher. Pings `/health`, prints a banner, only falls back to spawning when no supervisor is installed.
3. **Inactivity watchdog removed.** Proxy runs indefinitely.
4. **ResourceMonitor** detects leaks/drift and exits cleanly (`exit 75` = `EX_TEMPFAIL`); supervisor respawns.
5. **Supervisor abstraction** (`supervisor/` package) hides OS differences behind one interface. `setup.py` picks the adapter by `sys.platform`.

## File layout (delta from today)

```
claude-proxy/
├── proxy.py                          # remove --daemon fork + inactivity watchdog
├── supervisor/
│   ├── __init__.py                   # get_adapter() → picks by sys.platform
│   ├── base.py                       # interface: install/uninstall/status/restart/is_installed
│   ├── launchd.py                    # macOS plist + launchctl
│   ├── systemd.py                    # Linux user unit + systemctl --user
│   └── windows.py                    # stub — NotImplementedError with guidance
├── monitor.py                        # ResourceMonitor, metrics, /health payload
├── setup.py                          # cmd_* delegates to supervisor adapter
└── tests/
    ├── test_monitor.py
    ├── test_supervisor_launchd.py
    ├── test_supervisor_systemd.py
    └── test_health_endpoint.py
```

## Component specs

### `supervisor/base.py`

```python
class Supervisor(Protocol):
    def install(self, proxy_path: Path) -> None: ...   # idempotent
    def uninstall(self) -> None: ...                    # idempotent
    def is_installed(self) -> bool: ...
    def status(self) -> dict: ...                       # {loaded, running, pid, exit_code}
    def restart(self) -> None: ...                      # kickstart -k / systemctl restart
```

### `supervisor/launchd.py`

- Label: `com.claude-proxy.proxy` (new; migrates from legacy `com.claude-proxy.env`).
- Plist fields: `KeepAlive=true`, `ThrottleInterval=5`, `StandardOutPath`/`StandardErrorPath` → `~/.claude/claude-proxy/proxy.log`.
- Also exports `ANTHROPIC_BASE_URL` via `EnvironmentVariables` so Claude Desktop inherits it (replaces the old setenv-only plist).
- Install: write plist → `launchctl bootstrap gui/<uid>`.
- Migration: on install, if legacy label exists, `launchctl bootout` it first.

### `supervisor/systemd.py`

- Unit: `~/.config/systemd/user/claude-proxy.service`.
- `Type=simple`, `Restart=always`, `RestartSec=5`, `Environment=ANTHROPIC_BASE_URL=...`.
- Install: write unit → `systemctl --user daemon-reload` → `systemctl --user enable --now claude-proxy`.
- Also writes a `~/.config/environment.d/claude-proxy.conf` so GUI sessions pick up `ANTHROPIC_BASE_URL`.

### `supervisor/windows.py`

- Stub. `install()` raises `NotImplementedError("Windows supervisor not yet implemented; run proxy.py --daemon manually")`.

### `monitor.py` — `ResourceMonitor`

Background thread, 60s cadence. Thresholds (overridable via `plugins.toml` `[monitor]`):

| Metric | Default | Source |
|---|---|---|
| RSS memory | 512 MB | `psutil` if present, else `/proc/self/status` (Linux) or `resource.getrusage` (macOS) |
| Thread count | 200 | `threading.active_count()` |
| Open fd count | 80% of `RLIMIT_NOFILE` soft limit | `/proc/self/fd` (Linux), `psutil` or `lsof` fallback (macOS) |
| Plugin-reload count | 50 | counter incremented in `PluginManager._apply_swap` |

Behavior on breach:
1. Log `[monitor] recycling: reason=<r> value=<v> threshold=<t>`.
2. Flip a drain flag; stop accepting new requests. Wait up to 10s for in-flight to finish.
3. Notify telegram plugin (if enabled + `[monitor] notify = true`).
4. `os._exit(75)`.

Safety:
- **Cooldown:** never recycle twice within 10 min.
- **Kill switch:** `CLAUDE_PROXY_MONITOR=off` disables entirely.
- **No uptime cap** — recycle only on signal.

Snapshot API:
```python
monitor.snapshot() -> {
    "uptime_s": int,
    "rss_mb": int,
    "threads": int,
    "fds": int,
    "plugin_reloads": int,
    "last_recycle": {"at": iso8601, "reason": str} | None,
    "warnings": list[str],   # derived: e.g. "rss near cap"
}
```

### `/health` endpoint

Already exists. Extended to return `monitor.snapshot()` plus:
```json
{
  "status": "ok" | "warning",
  "pid": 22641,
  "port": 18019,
  ...monitor.snapshot()...
}
```
`status` is `warning` iff `warnings` is non-empty.

### SessionStart banner

Replaces the silent `|| true` spawn. New hook script (shipped in repo, installed by `setup.py install`):

```bash
#!/bin/sh
# Probe /health and print a one-line status. Respawn only if no supervisor.
```

Output when healthy:
```
[claude-proxy] ok · uptime 3h · rss 47MB · 2 reloads
```
Output when warning:
```
[claude-proxy] ⚠ rss 480MB (cap 512MB) · 48 reloads · consider: proxy restart
```
Output when dead and supervisor present:
```
[claude-proxy] ⚠ proxy not responding; supervisor should respawn within 5s
```
Output when dead and no supervisor:
```
[claude-proxy] starting proxy... (no supervisor installed; run `proxy install` for auto-restart)
```

### `proxy status` CLI

Extended to render the same metrics + warnings array.

### Telegram recycle notifications

On recycle, `monitor` calls into the telegram plugin's outbound hook with a synthesized system event. Message format:
```
⚠ claude-proxy recycling: rss 614MB exceeded 512MB cap — restarted cleanly
```
Opt-out via `[monitor] notify = false`.

## Changes to existing code

- **`proxy.py`**
  - Remove `_inactivity_watchdog` and its thread start.
  - Remove `--daemon` fork block when running under a supervisor (detect via env var set by adapter: `CLAUDE_PROXY_SUPERVISED=1`). Standalone `--daemon` still supported for dev/legacy.
  - Instantiate `ResourceMonitor` at startup; wire into `/health`.
  - Increment `plugin_reloads` counter on every `_apply_swap`.
- **`setup.py`**
  - `cmd_install` → calls `get_adapter().install(proxy_path)`, writes SessionStart hook, migrates legacy LaunchAgent.
  - `cmd_uninstall` → calls `get_adapter().uninstall()`.
  - `cmd_restart` → `get_adapter().restart()`; still supports `--force` to hard-kill + respawn (fallback for when supervisor itself is misbehaving).
  - `cmd_status` → prints supervisor status + monitor snapshot.
- **`plugins/leak_guard.py`** (if it auto-starts the proxy today — grep confirms it does): switch to a health probe that warns if the proxy is dead, instead of spawning.

## Testing

- `test_monitor.py` — threshold logic with injected metric source + clock; cooldown; kill switch; drain flag behavior.
- `test_supervisor_launchd.py` / `test_supervisor_systemd.py` — plist / unit generation is stable and idempotent; migration path removes legacy label.
- `test_health_endpoint.py` — `/health` returns monitor snapshot; `status` flips to `warning` when thresholds approached (e.g. rss at 80% of cap).
- `test_crash_scenarios.py` (existing, extend) — assert process exits 75 on breach; SessionStart banner renders correctly for each health state.

## Rollout

1. Land `monitor.py` + `/health` changes with monitor running but no supervisor yet. Verify metrics via `proxy status` in isolation.
2. Land `supervisor/` package; `setup.py install` switches to new adapter. Existing users get migration on next `proxy install`.
3. Flip SessionStart hook to the banner script. Remove `|| true` error-swallowing.
4. Remove `_inactivity_watchdog` only after (1–3) land and are green for a few days.

## Open questions

None blocking. Tuning of default thresholds (RSS 512MB, threads 200, fds 80%) is guesswork based on normal proxy behavior — revisit after a week of real-world data.

# WIP Split — COMPLETE ✅

> All 8 split PRs merged to main. This document is now a record of the split; the next phase is `feat/supervisor` (see bottom of this file).

---

# WIP Split — progress & handoff

Ongoing effort to split a large pre-existing WIP (stashed in local branch `wip-backup`) into scope-focused PRs before starting the `feat/supervisor` implementation plan at `docs/superpowers/plans/2026-04-20-claude-proxy-supervisor.md`.

## Merged PRs

| # | Title | Branch |
|---|---|---|
| 1 | feat: add task_watcher plugin | `feat/task-watcher` |
| 2 | feat(leak-guard): redact tool_use inputs and tool_result blocks | `feat/leak-guard-config` |
| 3 | chore: rename /proxy-status endpoint to /status | `chore/status-endpoint-rename` |
| 4 | chore: rename /proxy-status endpoint to /status (proxy.py) | `chore/status-rename-proxy` (hotfix to #3) |
| 5 | feat(reload): add /reload endpoint and !reload chat trigger plugin | `feat/reload-plugin` |

## All merged PRs

| # | Branch | Title |
|---|---|---|
| 1 | `feat/task-watcher` | feat: add task_watcher plugin |
| 2 | `feat/leak-guard-config` | feat(leak-guard): redact tool_use inputs and tool_result blocks |
| 3 | `chore/status-endpoint-rename` | chore: rename /proxy-status endpoint to /status |
| 4 | `chore/status-rename-proxy` | chore: rename /proxy-status endpoint to /status (proxy.py hotfix) |
| 5 | `feat/reload-plugin` | feat(reload): add /reload endpoint and !reload chat trigger plugin |
| 6 | `feat/plugin-hook-dispatcher` | feat(hooks): add unified PreToolUse hook dispatcher |
| 7 | `feat/telegram-approve` | feat(telegram): remote approval flow with inline buttons + hook dispatcher |
| 8 | `feat/proxy-resilience` | feat(proxy): startup lock, SIGTERM+SIGKILL kill, PID ownership guards |

## Next phase: feat/supervisor

Now that main is clean, the next branch is `feat/supervisor` — the 18-task plan at [plans/2026-04-20-claude-proxy-supervisor.md](plans/2026-04-20-claude-proxy-supervisor.md). It implements the launchd/systemd supervisor + ResourceMonitor + observability touchpoints.

Recommended execution: use `superpowers:subagent-driven-development` to dispatch one implementer subagent per task + two reviewers (spec + code quality) after each. 18 tasks × 3 subagents each is a lot of work — consider running it across multiple sessions.

Start fresh: verify you are on `main` and the working tree is clean, then `git checkout -b feat/supervisor main` and work through the plan task-by-task.

---

## Archive: original split plan (for reference)

1. **`feat/plugin-hook-dispatcher`** — generic PreToolUse hook infrastructure.
   - proxy.py: `_dispatch_hook(event)` function + `--hook EVENT` CLI arg + dispatch in `main()`.
   - setup.py: `PRETOOLUSE_HOOK_MARKER`, `patch_pretooluse_hook`, `unpatch_pretooluse_hook`; call from `install()` / `uninstall()`.
   - No plugin files — this just adds the dispatch infra telegram will use.

2. **`feat/telegram-approve`** — depends on (1). Large.
   - Whole files: `plugins/telegram.py`, `plugins/telegram.toml`, `tests/test_telegram.py`, `hooks/telegram_approve.py`, `tests/test_telegram_hook.py`.
   - proxy.py hunks: `_test_telegram` handler + `/test-telegram` route, `request_summary["cwd"]` + `_extract_cwd()` helper + `_CWD_PATTERNS` block.
   - setup.py hunks: `ensure_approval_config`, `install_hooks`, `_post_enable_telegram`, `_post_disable_telegram`, `_PLUGIN_POST_ENABLE` / `_PLUGIN_POST_DISABLE` dicts, `cmd_add_plugin`/`cmd_remove_plugin` wiring, install message update.
   - tests/test_proxy.py hunks: `_extract_cwd` import + `TestExtractCwd` class.

3. **`feat/proxy-resilience`** — the remaining proxy/setup changes.
   - proxy.py: `_cleanup_pid(expected_pid=...)`, `_port_in_use()`, `is_proxy_running()` fallback, `_inactivity_watchdog(server, my_pid)`, `_acquire_startup_lock()`, `main()` lock + SIGTERM shutdown via thread + removed parent-process `_write_pid` in fork.
   - setup.py: `kill_proxy` SIGTERM-then-SIGKILL, `cmd_restart` port-freed polling loop + better failure message.
   - tests/test_proxy.py: `TestMainDedup` patches (`_acquire_startup_lock`, `_port_in_use`).
   - tests/test_proxy_state.py (new, whole file).
   - tests/test_crash_scenarios.py (new, whole file).
   - tests/test_setup.py: `kill_proxy` SIGTERM+SIGKILL test update (line ~426-440).

4. **`feat/supervisor`** — branch from main **after** 1–3 merge. Runs the 18-task plan from [docs/superpowers/plans/2026-04-20-claude-proxy-supervisor.md](plans/2026-04-20-claude-proxy-supervisor.md).

## Critical constraint: the PreToolUse hook

`~/.claude/settings.json` contains a `PreToolUse` hook that runs `python3 proxy.py --hook pre-tool` before **every** tool call. This flag only exists in the WIP (`wip-backup`) version of `proxy.py`, not on `main`. **If `proxy.py` on disk doesn't support `--hook`, every tool call is blocked.**

Therefore, throughout this split work, **`proxy.py` in the working tree must remain the WIP version**. After every `git checkout main`, immediately run:
```bash
git checkout wip-backup -- proxy.py
```
This makes git show `proxy.py` as modified (unstaged) vs. `main`, which is fine — the important thing is the file on disk has `--hook` support.

Only **`feat/plugin-hook-dispatcher`** (branch 1 remaining) actually needs the `--hook` handler to land on main. Once it's merged, the working-tree workaround is no longer needed.

## Technique: staging arbitrary content without touching working tree

Use `git hash-object -w` + `git update-index --cacheinfo` to stage a specific file content without modifying the working tree:

```python
content = subprocess.check_output(["git", "show", "main:proxy.py"], text=True)
# ... apply desired hunks to `content` ...
h = subprocess.run(["git", "hash-object", "-w", "--stdin"], input=content, text=True,
                   capture_output=True, check=True).stdout.strip()
subprocess.run(["git", "update-index", "--cacheinfo", f"100644,{h},proxy.py"], check=True)
```

This is how I've been building scoped proxy.py / setup.py commits without disturbing the WIP `proxy.py` on disk.

## Gotcha encountered: `git stash -- file` discards staged blobs

`git stash push -- path/file.py` stashes **both index and working tree** state for that path and resets both. If you ran `update-index --cacheinfo` to stage a specific blob for that path, **the stash captures it and the subsequent `pop` can lose it** (seen in PR #3, fixed by hotfix PR #4). For this reason, do not use `git stash push -- proxy.py` when proxy.py has content staged via `update-index`.

## Per-branch workflow (cheat-sheet)

```bash
# 1. Branch from main
git checkout -b feat/X main

# 2. Whole files (new files) — cheap
git checkout wip-backup -- <files>

# 3. Hunks on proxy.py / setup.py — use update-index method
python3 <build-target-content-script>  # see technique above

# 4. Verify
git diff --cached --stat
git cat-file -p <staged-blob-sha> > /tmp/staged.py
python3 -c "import ast; ast.parse(open('/tmp/staged.py').read()); print('OK')"

# 5. Commit, push, PR, merge
git commit -m "..."
git push -u origin feat/X
gh pr create ...
gh pr merge --squash --delete-branch --admin

# 6. Sync
git stash push -u -m "cleanup"   # includes working tree WIP
git checkout main
git pull --ff-only origin main
git stash drop                   # or pop if you need the WIP back
git checkout wip-backup -- proxy.py   # restore WIP proxy.py for hook
```

## Who's next

A fresh session should:
1. Verify `proxy.py` in working tree has `--hook` support (`grep args.hook proxy.py`). If not, `git checkout wip-backup -- proxy.py`.
2. Read this file + the audit findings in an earlier conversation if needed.
3. Tackle `feat/plugin-hook-dispatcher` first (highest value — removes the on-disk workaround once landed).
4. Then `feat/telegram-approve`, then `feat/proxy-resilience`.
5. Finally, branch `feat/supervisor` from main and execute the 18-task plan.

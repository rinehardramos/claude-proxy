# Telegram Remote Decisions (answer Claude's flagged decisions from Telegram)

**Date:** 2026-06-21
**Status:** Design — awaiting review
**Components:** `hooks/telegram_decide.py` (new), `hooks/_tg_hook_common.py` (new), `plugins/telegram.py`, `plugins/telegram.toml`, settings.json registration

## Problem

When Claude flags an architectural decision via the **`AskUserQuestion`** tool (structured multiple-choice), Claude Code renders it in the **local terminal** and blocks on local input. If the user is away from the keyboard (e.g. only on Telegram, or the session was triggered by a Telegram message in resume mode), the decision is unanswerable remotely and the session stalls. The proxy plugin can't help: its SSE parser captures only `text_delta`, so `tool_use` blocks (the questions) are invisible to it, and even if surfaced, a plugin can't supply a tool's answer.

We want: when Claude asks a decision and the user is away, the question + options are sent to Telegram with one button per option; the user's tap is fed back as the answer and Claude proceeds — without the local terminal menu.

## Key mechanism (verified scope)

A **PreToolUse hook** on `AskUserQuestion` can return:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "updatedInput": {
      "questions": [ ...original questions array... ],
      "answers": { "<question text>": "<chosen label>" }
    }
  }
}
```

Returning this **bypasses the local terminal menu** and feeds the answers to the model. `multiSelect` answers are arrays. This is the same hook IPC the existing `telegram_approve.py` uses (pending/decided files + the plugin's callback poller), with a richer return value.

> **GATE — validation spike (must pass before building the rest):** the `updatedInput.answers` schema comes from documentation, not from running it on this machine's Claude Code build. Task 1 is a throwaway hook that hard-codes an answer for a one-question `AskUserQuestion` and confirms the model proceeds with it and no local menu appears. If it doesn't work, this design is revised before any further work.

## Scope (this spec)

**In scope (Phase 1):**
- Remote answering of **`AskUserQuestion`** (1–4 questions, single-select per question).
- **Session-aware gating**: route to Telegram only when "away"; otherwise let the local TUI handle it.
- **Autonomy config** for the resume-delivery deliverer: run `claude` with `--dangerously-skip-permissions` (and `--permission-mode acceptEdits`) so headless deliveries don't block on tool-approval prompts — leaving only genuine `AskUserQuestion` decisions to route to Telegram.
- Shared hook helper extracted so `telegram_approve.py` and `telegram_decide.py` don't duplicate Telegram/config code.

**Deferred (documented follow-on phases, not built here):**
- **`ExitPlanMode`** remote approve/reject. Feasible via the same hook, but the **plan text is not in the PreToolUse input** (Claude Code injects it later), so showing the plan needs a transcript read — separate phase.
- **`multiSelect`** questions (need a toggle + "Done" button); Phase 1 routes a multiSelect question to the local TUI (no Telegram routing) rather than answer it wrong.
- **Prose option lists** — already half-handled by the plugin's `_extract_options` → `_pending_replies` injection (lower fidelity, no tool answer). Unchanged here.

## Architecture

```
Claude calls AskUserQuestion
        │  (PreToolUse hook fires, stdin = {tool_name, tool_input.questions, permission_mode, ...})
        ▼
hooks/telegram_decide.py
  ├─ gating: away? (permission_mode / mode file)  ── no ──▶ emit nothing → local TUI handles it
  │                                                yes
  ├─ for each question: send a Telegram message, one inline button per option
  │     callback_data = "qopt:<decision_id>:<qIdx>:<optIdx>"
  ├─ write pending/<decision_id>.json  {questions, answered:{}, n_questions, created_at}
  ├─ poll decided/<decision_id>.json until all questions answered OR timeout
  └─ return allow + updatedInput{questions, answers}   (timeout → emit nothing → local TUI)
        ▲
        │ decided file written by:
plugins/telegram.py callback poller (already running)
  └─ on "qopt:" callback: record selection into pending file; when all questions
     answered, write decided/<decision_id>.json; edit the TG message to show the pick
```

The hook and the plugin communicate only through files under `~/.claude/claude-proxy/telegram-hook/` (same dir `telegram_approve.py` uses). The plugin's existing `_poll_loop` already receives callback queries; we add one new callback type.

## Components

1. **`hooks/_tg_hook_common.py`** (new, stdlib only). Extracted from `telegram_approve.py`:
   - `load_config()`, `_parse_toml()`, `tg_post(token, method, payload)`, `send_message(...)`, paths (`HOOK_DIR`, `PENDING_DIR`, `DECIDED_DIR`), and the session-aware gating helpers (`session_auto_approves`, `settings_default_mode`).
   - `telegram_approve.py` is refactored to import these (no behavior change; its tests stay green).

2. **`hooks/telegram_decide.py`** (new PreToolUse hook). Pure-ish, stdlib only:
   - `main()`: read stdin; if `tool_name != "AskUserQuestion"` → emit nothing (let it pass). Phase 1 ignores `ExitPlanMode`.
   - Gating: if not "away" → emit nothing (local TUI). Definition of away: reuse the decision-routing mode (see Config).
   - `build_question_messages(questions) -> list[payload]`: one message per single-select question, inline keyboard of option buttons (`qopt:<id>:<q>:<opt>`); a multiSelect question → return a sentinel so `main()` falls back to local for the whole call (Phase 1).
   - Send messages, write pending, poll decided, assemble `answers`, return allow+updatedInput. Timeout → emit nothing (local fallback) and mark the messages expired.
   - *Units:* `build_question_messages`, `assemble_answers(questions, decided) -> dict`, `should_route(hook_input) -> bool`.

3. **`plugins/telegram.py` — `qopt` callback handling** (in `_handle_callback`):
   - Parse `qopt:<decision_id>:<qIdx>:<optIdx>`; load `pending/<decision_id>.json`; record `answered[qIdx]=optIdx`; if `len(answered)==n_questions` → write `decided/<decision_id>.json`; ack + edit the message to show the chosen option.
   - *Unit:* `_handle_qopt_callback(cb, token, chat_id, payload)`.

4. **Deliverer autonomy** (`plugins/telegram.py` `_deliver_one`): when `deliver_autonomous` config is true (default true), the `claude` command gains `--dangerously-skip-permissions`. So a resume-delivered task runs to completion autonomously, and any `AskUserQuestion` it raises is answered via the decide hook (which works even in `--print` mode, since the hook supplies the answer).
   - **Verify during implementation:** whether `--dangerously-skip-permissions` and `--permission-mode acceptEdits` can be passed together (they may be mutually exclusive). `--dangerously-skip-permissions` already bypasses *all* tool-permission prompts, so it subsumes `acceptEdits`; the plan should pass the single flag unless the spike shows `acceptEdits` is separately needed.

5. **Registration**: `PreToolUse` matcher `AskUserQuestion` → `python3 ~/.claude/claude-proxy/hooks/telegram_decide.py`, timeout 600. Documented + added by the setup/install path.

## Config (`telegram.toml`)

```toml
# Route Claude's AskUserQuestion decisions to Telegram for remote answering.
decide_route = "auto"        # "auto" (route only when away) | "always" | "off"
decide_timeout = 600         # seconds the hook waits for a Telegram answer

# Resume-delivery autonomy: run delivered `claude --continue` without tool-approval
# prompts so headless tasks don't stall. Genuine AskUserQuestion decisions still
# route to Telegram via the decide hook.
deliver_autonomous = true    # adds --dangerously-skip-permissions to the deliverer
```

"Away" (for `decide_route="auto"`): the session's `permission_mode` is an auto-approving mode OR the `/mode` file is set to a remote-routing mode — reusing `telegram_approve.py`'s existing `session_auto_approves` logic. At the keyboard in default mode → local TUI.

> **Note the deliberate inversion vs. the approve hook.** For `telegram_approve.py`, an auto-approving mode means "I granted autonomy — *don't* bug me on Telegram for permissions." For the decide hook it means the opposite: an autonomous/away session *can't* answer a genuine decision locally, so the decision *must* go to Telegram. Same signal, complementary routing — this is intended, not a contradiction.

## Data flow (single call, 2 questions)

1. Claude → `AskUserQuestion(questions=[Q1,Q2])`; hook fires.
2. Hook routes (away) → sends TG msg for Q1 (buttons A/B), Q2 (buttons X/Y); writes `pending` with `n_questions=2`.
3. User taps Q1→B: plugin records `answered[0]=1`; not complete.
4. User taps Q2→X: plugin records `answered[1]=0`; complete → writes `decided{0:1, 1:0}`.
5. Hook (polling) reads decided → `answers={"Q1 text":"B label","Q2 text":"X label"}` → returns allow+updatedInput.
6. Model proceeds with those answers; no local menu shown.

## Error handling

| Case | Behavior |
|---|---|
| not configured (no token/chat) | emit nothing → local TUI (never blocks the user) |
| `decide_route="off"` | emit nothing → local TUI |
| not away (`auto`) | emit nothing → local TUI |
| any question is `multiSelect` (Phase 1) | emit nothing → local TUI (don't answer wrong) |
| Telegram send fails | emit nothing → local TUI; log to stderr |
| timeout (no answer within `decide_timeout`) | mark messages expired; emit nothing → local TUI |
| malformed `tool_input` | emit nothing → local TUI |
| decided file unreadable | timeout path |

The invariant: **the hook never blocks the user out of a decision** — every failure falls back to the local terminal menu (emit no output = default Claude Code behavior).

## Testing

Unit (stdlib, no network):
- `should_route`: away→true; at-keyboard default→false; `off`→false; `always`→true.
- `build_question_messages`: single-select → N buttons with correct `qopt:` callback_data; multiSelect present → sentinel (fall back).
- `assemble_answers`: maps qIdx/optIdx back to `{question_text: option_label}`; multiSelect would be a list (deferred).
- `_handle_qopt_callback`: records into pending; writes decided only when all answered; idempotent on re-tap.
- `_tg_hook_common` extraction: `telegram_approve.py`'s existing tests stay green after refactor.
- deliverer: `_deliver_one` includes `--dangerously-skip-permissions` when `deliver_autonomous`, omits it when false.

Integration / manual (the gate):
- **Task 1 spike**: hard-coded-answer hook proves `updatedInput.answers` works on this CLI build and suppresses the local menu.
- End-to-end live: a real `AskUserQuestion` (away) → Telegram buttons → tap → model proceeds with the choice.

## Deferred / follow-on (explicitly not in this plan)

- `ExitPlanMode` remote approve/reject (+ plan text via transcript read).
- `multiSelect` questions (toggle + Done button).
- Prose option lists upgraded to tool-grade answers.
- Surfacing the decision as a *notification* in addition to the buttons (the plugin's `on_inbound` could announce "decision pending" — but the hook already sends the buttons, so this is redundant for Phase 1).

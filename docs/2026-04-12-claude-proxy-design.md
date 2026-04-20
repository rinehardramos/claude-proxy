# claude-proxy — Design Spec

> **Goal:** A generic, extensible HTTP proxy for Claude Code that sits between the client and Anthropic's API, with a plugin system for request/response manipulation and a file-based sideload mechanism for external agent integration.

## Problem Statement

Claude Code communicates directly with Anthropic's API. There's no interception point for:
- Injecting context from external agents into requests
- Appending metadata (cost, token counts) to responses
- Forwarding notifications (Telegram, Slack) on activity
- Any custom request/response transformation

Claude Code hooks are limited — they can't rewrite prompts or modify responses. A local proxy with a plugin architecture fills this gap.

## Architecture

```
Claude Code (ANTHROPIC_BASE_URL=http://127.0.0.1:18019)
       |
       v
claude-proxy (127.0.0.1:18019)
       |
       ├── Load sideload/outbound/ files → inject into request
       ├── Load sideload/inbound/ files → hold in memory
       ├── Call on_outbound() for each enabled plugin
       |
       v
Anthropic API (api.anthropic.com:443)
       |
       v
claude-proxy receives response
       |
       ├── Stream SSE chunks to client (pass-through, zero latency)
       ├── Track content block index as events pass through
       ├── Before message_stop: synthesize SSE events for inbound sideload
       ├── Call on_inbound() for each enabled plugin (fire-and-forget side effects)
       |
       v
Claude Code receives response + injected content
```

## Component Design

### 1. Plugin Interface

Each plugin is a `.py` file in `~/.claude/claude-proxy/plugins/`. Plugins are proxy-agnostic — they receive and return plain dicts/strings with no proxy imports.

**Required function:**

```python
def plugin_info() -> dict:
    """Return plugin metadata.
    
    Returns:
        {"name": "telegram", "version": "0.1.0", "description": "Telegram notifications"}
    """
```

**Optional hooks:**

```python
def on_outbound(payload: dict) -> dict | None:
    """Called with the full API request payload before forwarding to Anthropic.
    
    Args:
        payload: The JSON request body (messages, system, model, etc.)
    
    Returns:
        Modified payload dict, or None to leave unchanged.
    """

def on_inbound(response_text: str, request_summary: dict) -> str | None:
    """Called with Claude's assembled response text after streaming completes.
    
    Args:
        response_text: The full text of Claude's response.
        request_summary: {"user_text": "...", "model": "...", "path": "..."}
    
    Returns:
        Extra text to inject into the response stream (as a new content block),
        or None for no injection.
    """
```

**Constraints:**
- Plugins must only use stdlib imports (no third-party dependencies)
- Plugin errors are caught and logged, never crash the proxy
- Plugins must not block — long-running side effects should spawn daemon threads
- Plugins receive copies of data, not references to proxy internals

### 2. Plugin Loader & Configuration

**Discovery:** At startup, proxy scans `~/.claude/claude-proxy/plugins/` for `.py` files. Each file is loaded via `importlib.util.spec_from_file_location` + `module_from_spec`.

**Configuration** (`~/.claude/claude-proxy/plugins.toml`):

```toml
# Only explicitly enabled plugins run (allowlist model)
enabled = ["telegram"]

# Plugin-specific configuration sections
[telegram]
bot_token_env = "TELEGRAM_BOT_TOKEN"
chat_id_env = "TELEGRAM_CHAT_ID"
```

**Enable/disable:** Only plugins listed in `enabled` are activated. Adding/removing from the list and restarting the proxy is the mechanism. No runtime toggle.

**Plugin config access:** Plugins read their own section from `plugins.toml` via a helper the proxy provides at load time, or read env vars directly. The proxy passes the plugin's config section to an optional `configure(config: dict)` function if the plugin defines one.

```python
def configure(config: dict) -> None:
    """Optional. Called once at load time with the plugin's config section from plugins.toml."""
```

### 3. Sideload Mechanism

Two directories under `~/.claude/claude-proxy/sideload/`:
- `outbound/` — content injected into the request going to Anthropic
- `inbound/` — content injected into the response coming back to the client

**File format (same for both):**

```json
{
  "target": "system",
  "content": "Context from external agent: the CI build is failing.",
  "priority": 0
}
```

**Target options for outbound (request modification):**
- `"system"` — append to the system field
- `"user_message"` — append as a content block in the last user message
- `"user_turn"` — append as a new user message in messages[]

**Target options for inbound (response modification):**
- `"append"` (default) — new content block at end of response
- `"prepend"` — new content block before the first response block

**File naming:** Timestamp-based for ordering, e.g., `1712345678_agent-name.json`. Sorted by filename before processing.

**Lifecycle:**
1. Request arrives at proxy
2. Load and consume (delete) all `.json` files from both `sideload/outbound/` and `sideload/inbound/`
3. Discard files with `mtime` older than 5 minutes (stale protection)
4. Inject outbound content into request payload
5. Hold inbound content in memory for response injection

**Trust model:** Sideloaded content is NOT scanned or redacted. External agents writing to the sideload directory are trusted.

### 4. SSE Response Injection

For inbound sideload content and plugin `on_inbound()` return values, the proxy synthesizes SSE events at the end of Claude's response stream.

**Streaming responses (SSE):**

1. Proxy forwards all SSE chunks to the client as they arrive (zero added latency on the main response)
2. Proxy tracks the current content block index by counting `content_block_start` events as they pass through
3. Proxy buffers `content_block_delta` text to assemble the full response text for `on_inbound()` plugins
4. When proxy sees `message_stop` event, it pauses before forwarding
5. For each piece of inbound content to inject:
   a. Emit `content_block_start` with `index = last_index + 1`, `type = "text"`
   b. Emit `content_block_delta` with the injection text
   c. Emit `content_block_stop`
   d. Increment index
6. Update the `message_stop` event's `usage` if needed
7. Forward `message_stop`

**SSE event format (Anthropic streaming protocol):**
```
event: content_block_start
data: {"type":"content_block_start","index":N,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":N,"delta":{"type":"text_delta","text":"injected content here"}}

event: content_block_stop
data: {"type":"content_block_stop","index":N}
```

**Non-streaming responses:**
Parse JSON response body, append/prepend content blocks to `content[]` array, re-serialize.

**Failure mode:** If SSE synthesis fails, forward `message_stop` unchanged. Never break the response.

### 5. Telegram Plugin

**File:** `~/.claude/claude-proxy/plugins/telegram.py` (also shipped as `plugins/telegram.py` in the project repo for easy copying)

**Behavior:**
- `on_inbound()`: Receives Claude's response text + request summary. Spawns a daemon thread that sends a summary to Telegram via Bot API. Returns `None` (no content injection).

**Summary format:**
```
Claude responded to: "{first 100 chars of user prompt}..."
{response length} chars
```

**Implementation:**
- `urllib.request.urlopen` to POST to `https://api.telegram.org/bot{token}/sendMessage`
- `threading.Thread(daemon=True)` for fire-and-forget delivery
- Reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from env vars (configured in `plugins.toml` via `bot_token_env` and `chat_id_env`)
- If either env var is missing, `configure()` logs a warning and plugin becomes a no-op
- All errors caught and logged to stderr

**No proxy dependency:** Only imports stdlib (`json`, `urllib.request`, `threading`, `os`).

### 6. HTTP Proxy Server

Stdlib-only HTTP proxy based on the same pattern as leak-guard's proxy.py.

**Components:**
- `ThreadedHTTPServer` — threaded request handling
- `ProxyHandler` — request forwarding with plugin/sideload orchestration
- Health endpoint: `GET /status` → `{"status": "ok", "plugins": [...], "sideload_pending": N}`
- Daemon mode: `--daemon` flag, PID file at `~/.claude/claude-proxy/proxy.pid`
- Inactivity auto-exit: 4 hours
- SIGTERM handler for clean shutdown

**Port:** `18019` (default), configurable via `CLAUDE_PROXY_PORT` env var.

**Request flow in `_forward()`:**
1. Read request body, parse JSON
2. Load + consume sideload files (outbound + inbound)
3. Inject outbound sideload content into payload
4. Call `on_outbound()` for each enabled plugin
5. Forward to Anthropic
6. Stream response back, tracking SSE events
7. Before `message_stop`: call `on_inbound()`, inject returns + inbound sideload
8. Forward `message_stop`

### 7. Project Structure

```
~/Projects/claude-proxy/
├── proxy.py              # HTTP proxy server, plugin loader, sideload, SSE injection
├── plugins/
│   └── telegram.py       # Telegram notification plugin (reference implementation)
├── tests/
│   ├── test_proxy.py     # Proxy unit + integration tests
│   └── test_telegram.py  # Telegram plugin tests
├── docs/
│   └── 2026-04-12-claude-proxy-design.md
├── README.md
└── pyproject.toml
```

**Runtime state** (`~/.claude/claude-proxy/`):
```
~/.claude/claude-proxy/
├── plugins/              # User's installed plugins
│   └── telegram.py
├── plugins.toml          # Plugin configuration
├── sideload/
│   ├── inbound/          # Files for response injection
│   └── outbound/         # Files for request injection
├── proxy.pid
└── proxy.log
```

## Relationship to leak-guard

**leak-guard standalone:** Uses its own simple proxy (`plugins/leak-guard/hooks/proxy.py`). No plugin system, no sideload. Distributed via Anthropic marketplace.

**leak-guard as claude-proxy plugin:** For personal use, leak-guard's scan/redact logic can be wrapped as a claude-proxy plugin implementing `on_outbound()`. This is a future integration — not part of this spec.

## Testing Strategy

### Unit tests
- `TestPluginLoader` — discovery, import, enable/disable, error handling
- `TestSideloadOutbound` — file reading, consumption, target injection, stale protection
- `TestSideloadInbound` — file reading, consumption, SSE synthesis
- `TestSSEInjection` — content block index tracking, event synthesis, non-streaming fallback
- `TestPluginInterface` — on_outbound/on_inbound contract, error isolation

### Integration tests
- End-to-end: request with sideload files → correct payload modifications
- Health endpoint with plugin list
- Telegram plugin with mocked HTTP (verify fire-and-forget, correct summary format)

### Plugin tests
- `TestTelegramPlugin` — plugin_info, configure, on_inbound with mock, missing env vars → no-op

## Version

v0.1.0 — initial release.

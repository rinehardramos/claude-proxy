# TODO

## Gemini CLI Proxy Support

Add support for proxying Google's Gemini CLI alongside Claude Code. Both tools use the same pattern: a base URL env var that can be pointed at a local proxy.

### Why

The plugin architecture (telegram notifications, sideloading, request/response hooks) is valuable for any AI CLI tool, not just Claude. Gemini CLI supports `GOOGLE_GEMINI_BASE_URL` for redirecting API calls to a local endpoint.

### What needs to change

#### 1. Dynamic upstream routing (`proxy.py`)

Currently `UPSTREAM_HOST` is hardcoded to `api.anthropic.com`. Needs to become dynamic:

- Detect which API client is calling based on request headers or path:
  - Anthropic: `x-api-key` header, paths like `/v1/messages`
  - Gemini: `x-goog-api-key` header, paths like `/v1beta/models/*/generateContent`
- Route to the correct upstream:
  - `api.anthropic.com:443` for Claude
  - `generativelanguage.googleapis.com:443` for Gemini
- Consider a config-driven routing table for extensibility

#### 2. Response format normalization

Plugin hooks need to work with both response formats:

- **Anthropic**: `{"content": [{"type": "text", "text": "..."}], ...}`
- **Gemini**: `{"candidates": [{"content": {"parts": [{"text": "..."}]}}]}`

Options:
- Normalize to a common internal format before passing to plugins
- Add `request_summary["provider"]` field so plugins can branch
- Keep `response_text` extraction working for both (already a string by the time plugins see it)

#### 3. SSE / Streaming differences

- Anthropic uses SSE with `event:` and `data:` lines
- Gemini uses SSE but with different event structure
- The content block injection logic in `_stream_and_inject` needs to handle both
- May need provider-specific injection strategies

#### 4. Setup / Configuration (`setup.py`)

- Add Gemini CLI detection and env var setup (`GOOGLE_GEMINI_BASE_URL`)
- LaunchAgent should set both `ANTHROPIC_BASE_URL` and `GOOGLE_GEMINI_BASE_URL`
- `proxy status` should show which providers are configured

#### 5. Plugin `request_summary` enrichment

Add `provider` field to `request_summary`:
```python
request_summary = {
    "provider": "anthropic" | "gemini",
    "path": "...",
    "model": "...",
    "user_text": "...",
}
```

Plugins like telegram can then include the provider in notifications.

### Rough implementation order

1. Refactor `UPSTREAM_HOST` → dynamic routing based on request inspection
2. Extract `response_text` for Gemini responses (non-streaming first)
3. Get basic proxying working (passthrough, no injection)
4. Add Gemini SSE streaming support
5. Validate plugin hooks work with Gemini responses
6. Update `setup.py` for Gemini env var configuration
7. Tests for Gemini request detection, routing, and response extraction

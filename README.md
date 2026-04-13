# claude-proxy

A local HTTP proxy for Claude Code that sits between the CLI and Anthropic's API, with a plugin system for request/response manipulation and notifications.

## What it does

```
Claude Code  ──►  claude-proxy (127.0.0.1:18019)  ──►  Anthropic API
                         │
                         ├── on_outbound: modify/inspect requests
                         ├── on_inbound: modify/inspect responses
                         ├── sideload: inject files into conversations
                         └── plugins: telegram, leak-guard, task-watcher, etc.
```

Claude Code sets `ANTHROPIC_BASE_URL=http://127.0.0.1:18019` so all API traffic flows through the proxy. The proxy forwards requests to `api.anthropic.com` while running plugin hooks on each request/response cycle.

## Features

- **Plugin system** — Drop `.py` + `.toml` files into `~/.claude/claude-proxy/plugins/` to add functionality. Plugins can modify requests, inspect responses, and trigger side effects.
- **Sideload mechanism** — Place text files in `sideload/outbound/` or `sideload/inbound/` to inject content into the next request or response.
- **Telegram notifications** — Get notified on every Claude response. Short responses sent as text, long responses converted to voice messages via TTS.
- **TTS audio** — Extensible TTS engine registry (macOS `say`, OpenAI, pyttsx3) with automatic fallback and diagnostics when engines fail.
- **Hot reload** — `curl http://127.0.0.1:18019/proxy-reload` reloads plugins without restarting.
- **Zero-latency streaming** — SSE events are streamed through to the client in real-time; plugin hooks run after the response completes.

## Quick start

```bash
# Install and start
python3 setup.py install
python3 setup.py start

# Verify
python3 setup.py status

# Claude Code will automatically use the proxy via ANTHROPIC_BASE_URL
```

## Setup commands

```bash
python3 setup.py install     # Copy files, set up LaunchAgent for env vars
python3 setup.py start       # Start the proxy daemon
python3 setup.py stop        # Stop the proxy
python3 setup.py restart     # Hot-reload plugins or full restart
python3 setup.py status      # Show proxy status and loaded plugins
python3 setup.py enable <p>  # Enable a plugin
python3 setup.py disable <p> # Disable a plugin
```

## Plugin development

Each plugin is a standalone `.py` file with optional hooks:

```python
def plugin_info() -> dict:
    return {"name": "my-plugin", "version": "0.1.0", "description": "..."}

def configure(config: dict) -> None:
    """Called once at load time with config from the .toml file."""
    pass

def on_outbound(payload: dict) -> dict | None:
    """Modify the request before it's sent upstream. Return modified payload or None."""
    return None

def on_inbound(response_text: str, request_summary: dict) -> str | None:
    """Called after response. Return text to inject, or None."""
    return None
```

Configuration goes in a `.toml` file with the same name:

```toml
enabled = true
# plugin-specific config keys here
```

## Included plugins

### telegram

Sends Claude's full response to a Telegram chat. Features:

- Full response text, split into 4096-char chunks with project name and prompt headers
- TTS audio fallback for long responses (configurable threshold, default 8192 chars)
- Extensible TTS engine registry: macOS `say`, OpenAI TTS, pyttsx3
- Dynamic encoding timeouts that scale with text length
- Status tracking with per-stage logging
- Automatic fallback to text with diagnostic note when TTS fails

Config (`telegram.toml`):

```toml
enabled = true
bot_token = "your-bot-token"
chat_id = "your-chat-id"

# project_name = "my-project"        # defaults to cwd basename
# audio_threshold = 8192             # chars before switching to audio
# tts_engine = "say"                 # "say", "openai", "pyttsx3", "none"
# voice_upload_timeout = 300         # seconds
# tts_openai_model = "tts-1"
# tts_openai_voice = "alloy"
# tts_openai_api_key_env = "OPENAI_API_KEY"
```

### task_watcher

Monitors background tasks dispatched via MCP tools (like `run_assistant`) and injects results back into the conversation when they complete.

### leak_guard

Scans outbound payloads for secrets, credentials, and PII before they leave the proxy.

## Project structure

```
proxy.py              # HTTP proxy server
setup.py              # CLI for install/start/stop/status/plugin management
plugins/
  telegram.py         # Telegram notification + TTS plugin
  telegram.toml       # Telegram config template
  task_watcher.py     # Background task result injection
  task_watcher.toml
  leak_guard.toml     # Leak guard config
tests/
  test_telegram.py    # 80 tests for telegram plugin
  test_proxy.py       # Proxy integration tests
  test_setup.py       # Setup CLI tests
  test_task_watcher.py
docs/
  2026-04-12-claude-proxy-design.md  # Architecture design spec
```

## Requirements

- Python 3.9+
- macOS (for `say` TTS engine) or ffmpeg (`brew install ffmpeg`)
- No pip dependencies — stdlib only (pyttsx3 optional for cross-platform TTS)

## Roadmap

See [TODO.md](TODO.md) for planned features, including Gemini CLI proxy support.

## License

Private project.

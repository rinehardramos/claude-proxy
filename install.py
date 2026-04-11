"""
claude-proxy installer — global setup for Claude Code integration.

Usage:
    python3 install.py           # install
    python3 install.py uninstall # remove proxy hook from settings + shell profile note

What it does:
  1. Creates ~/.claude/claude-proxy/{plugins,sideload/inbound,sideload/outbound}
  2. Copies plugins/*.py → ~/.claude/claude-proxy/plugins/
  3. Writes a starter plugins.toml (skips if already present)
  4. Patches ~/.claude/settings.json — adds a SessionStart hook that launches
     proxy.py --daemon on Claude Code startup
  5. Patches your shell profile — adds ANTHROPIC_BASE_URL=http://127.0.0.1:18019
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────

HOOK_MARKER = "claude-proxy"
SESSION_START_HOOK_COMMAND = "python3 {proxy_py} --daemon 2>/dev/null || true  # {marker}"

_DEFAULT_PLUGINS_TOML = """\
# claude-proxy plugin configuration
# Add plugin names to "enabled" to activate them.
# Each plugin must exist as a .py file in ~/.claude/claude-proxy/plugins/
enabled = []

# ── Telegram notifications ──────────────────────────────────────────────────
# Sends a summary to Telegram after each Claude response.
#
# Option A — credentials directly in this file (simplest):
#
# enabled = ["telegram"]
#
# [telegram]
# bot_token = "your-bot-token-here"
# chat_id   = "your-chat-id-here"
#
# Option B — credentials via environment variables (better for shared machines):
#
# enabled = ["telegram"]
#
# [telegram]
# bot_token_env = "TELEGRAM_BOT_TOKEN"   # default, can omit
# chat_id_env   = "TELEGRAM_CHAT_ID"     # default, can omit
#
# Resolution order: direct value → env var name → default env var name
"""

_SHELL_BLOCK = """\

# {marker} — route Claude Code through local proxy
export ANTHROPIC_BASE_URL=http://127.0.0.1:18019
# end {marker}
""".format(marker=HOOK_MARKER)


# ── Public API ─────────────────────────────────────────────────────────────

def create_runtime_dirs(state_dir: Path) -> None:
    """Create ~/.claude/claude-proxy/ directory tree."""
    for subdir in [
        state_dir,
        state_dir / "plugins",
        state_dir / "sideload" / "inbound",
        state_dir / "sideload" / "outbound",
    ]:
        subdir.mkdir(parents=True, exist_ok=True)


def install_plugins(src_dir: Path, dst_dir: Path) -> None:
    """Copy all *.py files from src_dir to dst_dir (overwrites)."""
    for py_file in src_dir.glob("*.py"):
        dst = dst_dir / py_file.name
        dst.write_bytes(py_file.read_bytes())


def write_plugins_toml(path: Path) -> None:
    """Write a starter plugins.toml — skips if the file already exists."""
    if path.exists():
        return
    path.write_text(_DEFAULT_PLUGINS_TOML, encoding="utf-8")


def patch_settings_json(settings_path: Path, proxy_py: Path) -> None:
    """Add a SessionStart startup hook to ~/.claude/settings.json.

    Idempotent — does nothing if the marker is already present.
    Preserves all existing hooks.
    """
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
        settings_path.parent.mkdir(parents=True, exist_ok=True)

    hooks = data.setdefault("hooks", {})
    session_start = hooks.setdefault("SessionStart", [])

    # Idempotency — bail if marker already present
    if any(HOOK_MARKER in json.dumps(entry) for entry in session_start):
        return

    command = SESSION_START_HOOK_COMMAND.format(
        proxy_py=str(proxy_py),
        marker=HOOK_MARKER,
    )
    session_start.append({
        "matcher": "startup",
        "hooks": [{"type": "command", "command": command}],
    })

    settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def unpatch_settings_json(settings_path: Path) -> None:
    """Remove all claude-proxy SessionStart hooks from settings.json."""
    if not settings_path.exists():
        return
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    session_start = data.get("hooks", {}).get("SessionStart", [])
    filtered = [e for e in session_start if HOOK_MARKER not in json.dumps(e)]
    data.setdefault("hooks", {})["SessionStart"] = filtered

    # Clean up empty SessionStart list to keep settings tidy
    if not filtered:
        data["hooks"].pop("SessionStart", None)

    settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def detect_shell_profile() -> Path:
    """Return the most appropriate shell profile path for the current user."""
    shell = os.environ.get("SHELL", "")
    home = Path.home()
    if "zsh" in shell:
        return home / ".zshrc"
    if "bash" in shell:
        # macOS default bash uses .bash_profile; Linux uses .bashrc
        bp = home / ".bash_profile"
        return bp if sys.platform == "darwin" else home / ".bashrc"
    # Unknown shell — default to zshrc (macOS default since Catalina)
    return home / ".zshrc"


def patch_shell_profile(profile: Path) -> None:
    """Append ANTHROPIC_BASE_URL export to the shell profile — idempotent."""
    existing = profile.read_text(encoding="utf-8") if profile.exists() else ""
    if HOOK_MARKER in existing:
        return
    profile.write_text(existing + _SHELL_BLOCK, encoding="utf-8")


# ── Install / Uninstall orchestration ─────────────────────────────────────

def install(
    project_dir: Path | None = None,
    state_dir: Path | None = None,
    settings_path: Path | None = None,
    shell_profile: Path | None = None,
) -> None:
    """Run the full install flow."""
    project_dir = project_dir or Path(__file__).resolve().parent
    state_dir = state_dir or Path("~/.claude/claude-proxy").expanduser()
    settings_path = settings_path or Path("~/.claude/settings.json").expanduser()
    shell_profile = shell_profile or detect_shell_profile()

    proxy_py = project_dir / "proxy.py"

    print("[claude-proxy] Creating runtime directories...")
    create_runtime_dirs(state_dir)

    print("[claude-proxy] Installing plugins...")
    install_plugins(project_dir / "plugins", state_dir / "plugins")

    print("[claude-proxy] Writing plugins.toml...")
    write_plugins_toml(state_dir / "plugins.toml")

    print(f"[claude-proxy] Patching {settings_path}...")
    patch_settings_json(settings_path, proxy_py)

    print(f"[claude-proxy] Patching {shell_profile}...")
    patch_shell_profile(shell_profile)

    print("""
[claude-proxy] Installation complete.

Next steps:
  1. Reload your shell:   source {profile}
  2. Restart Claude Code — the proxy will start automatically.
  3. Verify:              curl -s http://127.0.0.1:18019/proxy-status

To enable Telegram notifications:
  - Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your environment.
  - Edit ~/.claude/claude-proxy/plugins.toml and set enabled = ["telegram"]
""".format(profile=shell_profile))


def uninstall(
    settings_path: Path | None = None,
    shell_profile: Path | None = None,
) -> None:
    """Remove the proxy hook from settings.json."""
    settings_path = settings_path or Path("~/.claude/settings.json").expanduser()
    shell_profile = shell_profile or detect_shell_profile()

    print(f"[claude-proxy] Removing hook from {settings_path}...")
    unpatch_settings_json(settings_path)

    print(f"""
[claude-proxy] Hook removed from settings.json.

Manual step — remove these lines from {shell_profile}:

    # {HOOK_MARKER} — route Claude Code through local proxy
    export ANTHROPIC_BASE_URL=http://127.0.0.1:18019
    # end {HOOK_MARKER}
""")


# ── Entry point ────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "uninstall":
        uninstall()
    else:
        install()


if __name__ == "__main__":
    main()

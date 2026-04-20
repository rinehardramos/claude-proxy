#!/usr/bin/env python3
"""
claude-proxy CLI — manage the local Claude Code proxy.

Usage:
    python3 setup.py install              # full install
    python3 setup.py uninstall            # full removal
    python3 setup.py add-plugin <name>    # enable a plugin
    python3 setup.py remove-plugin <name> # disable a plugin
    python3 setup.py list-plugins         # show installed plugins
    python3 setup.py status               # proxy health check

What it does (install):
  1. Creates ~/.claude/claude-proxy/{plugins,sideload/inbound,sideload/outbound}
  2. Copies plugins/*.py -> ~/.claude/claude-proxy/plugins/
  3. Writes a starter plugins.toml (skips if already present)
  4. Patches ~/.claude/settings.json -- SessionStart hook starts proxy on session open
  5. Patches shell profile -- starts proxy + conditionally exports ANTHROPIC_BASE_URL
     (only exported when the proxy is confirmed healthy = automatic direct fallback)

What it does (uninstall):
  1. Sends SIGTERM to running proxy via PID file
  2. Removes ~/.claude/claude-proxy/ entirely
  3. Removes SessionStart hook from settings.json
  4. Removes the claude-proxy block from your shell profile
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────

HOOK_MARKER = "claude-proxy"
SESSION_START_HOOK_COMMAND = "python3 {proxy_py} --daemon 2>/dev/null || true  # {marker}"

# Shell profile block template.
# Starts the proxy, then polls /status for up to 1.5 s.
# ANTHROPIC_BASE_URL is only exported when the proxy is confirmed healthy --
# if the proxy fails to start, the variable is left unset and Claude Code
# talks directly to api.anthropic.com (graceful fallback).
_SHELL_BLOCK_TEMPLATE = """\

# {marker} -- route Claude Code through local proxy
python3 {proxy_py} --daemon 2>/dev/null
_cp=0; while [ "$_cp" -lt 5 ]; do
  curl -sf http://127.0.0.1:18019/status >/dev/null 2>&1 \\
    && export ANTHROPIC_BASE_URL=http://127.0.0.1:18019 && break
  sleep 0.3; _cp=$((_cp+1))
done; unset _cp
# end {marker}
"""

_DEFAULT_PLUGINS_TOML = """\
# claude-proxy plugin configuration
# Add plugin names to "enabled" to activate them.
# Each plugin must exist as a .py file in ~/.claude/claude-proxy/plugins/
enabled = []

# -- Telegram notifications ------------------------------------------------
# Sends a summary to Telegram after each Claude response.
#
# Option A -- credentials directly in this file (simplest):
#
# enabled = ["telegram"]
#
# [telegram]
# bot_token = "your-bot-token-here"
# chat_id   = "your-chat-id-here"
#
# Option B -- credentials via environment variables (better for shared machines):
#
# enabled = ["telegram"]
#
# [telegram]
# bot_token_env = "TELEGRAM_BOT_TOKEN"   # default, can omit
# chat_id_env   = "TELEGRAM_CHAT_ID"     # default, can omit
#
# Resolution order: direct value -> env var name -> default env var name
"""

# ── Library functions ─────────────────────────────────────────────────────


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
    """Copy all *.py and *.toml files from src_dir to dst_dir.

    .py files are always overwritten (code updates).
    .toml files are only written if missing (preserves user config).
    """
    for py_file in src_dir.glob("*.py"):
        (dst_dir / py_file.name).write_bytes(py_file.read_bytes())
    for toml_file in src_dir.glob("*.toml"):
        dst = dst_dir / toml_file.name
        if not dst.exists():
            dst.write_bytes(toml_file.read_bytes())


def write_plugins_toml(path: Path) -> None:
    """Write a starter plugins.toml -- skips if the file already exists."""
    if path.exists():
        return
    path.write_text(_DEFAULT_PLUGINS_TOML, encoding="utf-8")


def patch_settings_json(settings_path: Path, proxy_py: Path) -> None:
    """Add a SessionStart startup hook to ~/.claude/settings.json.

    Idempotent -- does nothing if the marker is already present.
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

    # Idempotency -- bail if marker already present
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
    # Unknown shell -- default to zshrc (macOS default since Catalina)
    return home / ".zshrc"


def patch_shell_profile(profile: Path, proxy_py: Path) -> None:
    """Append proxy start + conditional ANTHROPIC_BASE_URL to the shell profile.

    Idempotent -- does nothing if the marker is already present.
    ANTHROPIC_BASE_URL is only exported when the proxy health check passes,
    so Claude Code falls back to api.anthropic.com directly if the proxy
    fails to start.
    """
    existing = profile.read_text(encoding="utf-8") if profile.exists() else ""
    if HOOK_MARKER in existing:
        return
    block = _SHELL_BLOCK_TEMPLATE.format(marker=HOOK_MARKER, proxy_py=str(proxy_py))
    profile.write_text(existing + block, encoding="utf-8")


def unpatch_shell_profile(profile: Path) -> None:
    """Remove the claude-proxy block from the shell profile -- idempotent."""
    if not profile.exists():
        return
    text = profile.read_text(encoding="utf-8")
    if HOOK_MARKER not in text:
        return
    pattern = (
        r"\n# " + re.escape(HOOK_MARKER) + r".*?# end " + re.escape(HOOK_MARKER) + r"\n"
    )
    cleaned = re.sub(pattern, "", text, flags=re.DOTALL)
    profile.write_text(cleaned, encoding="utf-8")


def kill_proxy(state_dir: Path) -> None:
    """Send SIGTERM to the running proxy via its PID file, then remove the file."""
    pid_file = state_dir / "proxy.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        pass
    try:
        pid_file.unlink(missing_ok=True)
    except OSError:
        pass


_LAUNCHAGENT_LABEL = "com.claude-proxy.env"
_LAUNCHAGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHAGENT_LABEL}.plist"
_LAUNCHAGENT_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/launchctl</string>
        <string>setenv</string>
        <string>ANTHROPIC_BASE_URL</string>
        <string>http://127.0.0.1:18019</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
"""


def install_launchagent() -> None:
    """Install a LaunchAgent that sets ANTHROPIC_BASE_URL system-wide at login.

    This ensures GUI apps (Claude Code) always route through the proxy,
    even when they don't inherit shell environment variables.
    Idempotent -- does nothing if already installed.
    """
    if sys.platform != "darwin":
        return
    if _LAUNCHAGENT_PATH.exists():
        return
    _LAUNCHAGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LAUNCHAGENT_PATH.write_text(
        _LAUNCHAGENT_PLIST.format(label=_LAUNCHAGENT_LABEL), encoding="utf-8"
    )
    try:
        subprocess.run(
            ["launchctl", "load", str(_LAUNCHAGENT_PATH)],
            check=False, capture_output=True,
        )
        subprocess.run(
            ["launchctl", "setenv", "ANTHROPIC_BASE_URL", "http://127.0.0.1:18019"],
            check=False, capture_output=True,
        )
    except FileNotFoundError:
        pass  # launchctl not available


def uninstall_launchagent() -> None:
    """Remove the claude-proxy LaunchAgent -- idempotent."""
    if sys.platform != "darwin":
        return
    if not _LAUNCHAGENT_PATH.exists():
        return
    try:
        subprocess.run(
            ["launchctl", "unload", str(_LAUNCHAGENT_PATH)],
            check=False, capture_output=True,
        )
    except FileNotFoundError:
        pass
    try:
        _LAUNCHAGENT_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _set_toml_enabled(text: str, enabled: bool) -> str:
    """Set or update the ``enabled`` flag in a TOML string."""
    val = "true" if enabled else "false"
    if re.search(r"^enabled\s*=", text, re.MULTILINE):
        return re.sub(r"^enabled\s*=\s*\S+", f"enabled = {val}", text, count=1, flags=re.MULTILINE)
    # No enabled line -- prepend one
    return f"enabled = {val}\n{text}"


def enable_plugin(state_dir: Path, project_dir: Path, name: str) -> None:
    """Install and enable a plugin by name.

    Copies .py (always) and .toml (only if missing) from project_dir/plugins/
    to state_dir/plugins/, then sets ``enabled = true`` in the .toml.
    """
    src_py = project_dir / "plugins" / f"{name}.py"
    if not src_py.exists():
        raise FileNotFoundError(f"Plugin '{name}' not found at {src_py}")

    dst_plugins = state_dir / "plugins"
    dst_plugins.mkdir(parents=True, exist_ok=True)

    # Always overwrite .py (code update)
    (dst_plugins / f"{name}.py").write_bytes(src_py.read_bytes())

    # Copy .toml only if not already present (preserve user config)
    src_toml = project_dir / "plugins" / f"{name}.toml"
    dst_toml = dst_plugins / f"{name}.toml"
    if src_toml.exists() and not dst_toml.exists():
        dst_toml.write_bytes(src_toml.read_bytes())

    # Ensure .toml exists (create minimal one if no source toml)
    if not dst_toml.exists():
        dst_toml.write_text("enabled = true\n", encoding="utf-8")
    else:
        text = dst_toml.read_text(encoding="utf-8")
        dst_toml.write_text(_set_toml_enabled(text, True), encoding="utf-8")


def disable_plugin(state_dir: Path, name: str) -> None:
    """Disable a plugin by setting ``enabled = false`` in its .toml.

    Does NOT delete plugin files -- user can re-enable later.
    """
    dst_toml = state_dir / "plugins" / f"{name}.toml"
    if not dst_toml.exists():
        raise FileNotFoundError(f"Plugin '{name}' config not found at {dst_toml}")

    text = dst_toml.read_text(encoding="utf-8")
    dst_toml.write_text(_set_toml_enabled(text, False), encoding="utf-8")


def list_plugins(state_dir: Path) -> list[tuple[str, bool]]:
    """Return a sorted list of (name, enabled) for all installed plugins."""
    plugins_dir = state_dir / "plugins"
    if not plugins_dir.exists():
        return []

    result = []
    for py_file in sorted(plugins_dir.glob("*.py")):
        if py_file.name.startswith("__"):
            continue
        name = py_file.stem
        toml_file = plugins_dir / f"{name}.toml"
        enabled = False
        if toml_file.exists():
            text = toml_file.read_text(encoding="utf-8")
            match = re.search(r"^enabled\s*=\s*(\S+)", text, re.MULTILINE)
            if match:
                enabled = match.group(1).lower() == "true"
        result.append((name, enabled))
    return result


def proxy_status(port: int = 18019) -> dict | None:
    """Query the proxy health endpoint. Returns parsed JSON or None."""
    try:
        url = f"http://127.0.0.1:{port}/status"
        with urllib.request.urlopen(url, timeout=2) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


# ── Install / Uninstall orchestration ─────────────────────────────────────

PRETOOLUSE_HOOK_MARKER = "claude-proxy-hook"


def patch_pretooluse_hook(settings_path: Path, proxy_py: Path) -> None:
    """Register the proxy's unified PreToolUse dispatcher in settings.json.

    A single entry: ``proxy.py --hook pre-tool`` dispatches to all plugin
    hook scripts in ~/.claude/claude-proxy/hooks/.  Plugins are added/removed
    without touching settings.json — only their hook scripts and .toml configs.

    Idempotent — does nothing if the marker is already present.
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
    pretooluse = hooks.setdefault("PreToolUse", [])

    # Idempotency
    if any(PRETOOLUSE_HOOK_MARKER in json.dumps(entry) for entry in pretooluse):
        return

    command = f"python3 {proxy_py} --hook pre-tool  # {PRETOOLUSE_HOOK_MARKER}"
    pretooluse.append({
        "hooks": [{"type": "command", "command": command, "timeout": 660}],
    })

    settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def unpatch_pretooluse_hook(settings_path: Path) -> None:
    """Remove the proxy's PreToolUse dispatcher from settings.json."""
    if not settings_path.exists():
        return
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    pretooluse = data.get("hooks", {}).get("PreToolUse", [])
    filtered = [e for e in pretooluse if PRETOOLUSE_HOOK_MARKER not in json.dumps(e)]
    data.setdefault("hooks", {})["PreToolUse"] = filtered

    if not filtered:
        data["hooks"].pop("PreToolUse", None)

    settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


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

    print("[claude-proxy] Registering PreToolUse hook dispatcher...")
    patch_pretooluse_hook(settings_path, proxy_py)

    print(f"[claude-proxy] Patching {shell_profile}...")
    patch_shell_profile(shell_profile, proxy_py)

    print("[claude-proxy] Installing LaunchAgent (ANTHROPIC_BASE_URL for GUI apps)...")
    install_launchagent()

    print("""
[claude-proxy] Installation complete.

Next steps:
  1. Reload your shell:   source {profile}
  2. Restart Claude Code -- the proxy will start automatically.
  3. Verify:              curl -s http://127.0.0.1:18019/status

To enable Telegram notifications:
  python3 setup.py add-plugin telegram
  Edit ~/.claude/claude-proxy/plugins/telegram.toml with your credentials.
""".format(profile=shell_profile))


def uninstall(
    state_dir: Path | None = None,
    settings_path: Path | None = None,
    shell_profile: Path | None = None,
) -> None:
    """Full removal: kill proxy, delete runtime dir, clean settings + shell profile."""
    state_dir = state_dir or Path("~/.claude/claude-proxy").expanduser()
    settings_path = settings_path or Path("~/.claude/settings.json").expanduser()
    shell_profile = shell_profile or detect_shell_profile()

    print("[claude-proxy] Stopping proxy...")
    kill_proxy(state_dir)

    print(f"[claude-proxy] Removing runtime directory {state_dir}...")
    if state_dir.exists():
        shutil.rmtree(state_dir)

    unpatch_pretooluse_hook(settings_path)
    print(f"[claude-proxy] Removing hook from {settings_path}...")
    unpatch_settings_json(settings_path)

    print(f"[claude-proxy] Removing shell profile block from {shell_profile}...")
    unpatch_shell_profile(shell_profile)

    print("[claude-proxy] Removing LaunchAgent...")
    uninstall_launchagent()

    print(f"\n[claude-proxy] Uninstalled. Reload your shell:  source {shell_profile}")


# ── CLI ───────────────────────────────────────────────────────────────────

def _default_state_dir() -> Path:
    return Path("~/.claude/claude-proxy").expanduser()


def _default_project_dir() -> Path:
    return Path(__file__).resolve().parent


def cmd_install(args: argparse.Namespace) -> None:
    """Run the full install flow."""
    install(project_dir=_default_project_dir())


def cmd_uninstall(args: argparse.Namespace) -> None:
    """Full removal: kill proxy, delete runtime dir, clean settings + shell profile."""
    uninstall()


def cmd_add_plugin(args: argparse.Namespace) -> None:
    """Enable a plugin by name."""
    state_dir = _default_state_dir()
    project_dir = _default_project_dir()
    try:
        enable_plugin(state_dir, project_dir, args.name)
        toml_path = state_dir / "plugins" / (args.name + ".toml")
        print(f"[claude-proxy] Plugin '{args.name}' enabled.")
        print(f"  Config: {toml_path}")
        print(f"  Edit {toml_path} to configure credentials/settings.")
        print("  The proxy will hot-reload the plugin automatically.")
    except FileNotFoundError as e:
        print(f"[claude-proxy] Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_remove_plugin(args: argparse.Namespace) -> None:
    """Disable a plugin by name (does not delete files)."""
    state_dir = _default_state_dir()
    try:
        disable_plugin(state_dir, args.name)
        print(f"[claude-proxy] Plugin '{args.name}' disabled.")
        print("  The proxy will hot-reload the change automatically.")
        print(f"  To re-enable: python3 setup.py add-plugin {args.name}")
    except FileNotFoundError as e:
        print(f"[claude-proxy] Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_list_plugins(args: argparse.Namespace) -> None:
    """Show installed plugins and their status."""
    state_dir = _default_state_dir()
    plugins = list_plugins(state_dir)
    if not plugins:
        print("[claude-proxy] No plugins installed.")
        return
    print("[claude-proxy] Installed plugins:")
    for name, enabled in plugins:
        marker = "\u2713" if enabled else "\u2717"
        status = "enabled" if enabled else "disabled"
        print(f"  {marker} {name:20s} [{status}]")


def cmd_restart(args: argparse.Namespace) -> None:
    """Kill running proxy and start a fresh one."""
    state_dir = _default_state_dir()
    project_dir = _default_project_dir()
    proxy_py = project_dir / "proxy.py"

    # Try hot-reload first — faster than a full restart for plugin edits.
    if proxy_status() is not None:
        try:
            url = "http://127.0.0.1:18019/reload"
            with urllib.request.urlopen(url, timeout=3) as resp:
                result = json.loads(resp.read())
            if result.get("status") == "reloaded":
                print("[claude-proxy] Hot-reloaded plugins (no restart needed).")
                status = proxy_status()
                if status:
                    plugins = status.get("plugins", [])
                    print(f"  Plugins: {', '.join(plugins) if plugins else 'none'}")
                return
        except Exception:
            pass  # Fall through to full restart

    print("[claude-proxy] Stopping proxy...")
    kill_proxy(state_dir)
    time.sleep(0.5)
    subprocess.Popen(
        [sys.executable, str(proxy_py), "--daemon"],
        stdout=subprocess.DEVNULL,
        stderr=open(state_dir / "proxy.log", "a"),
    )
    # Wait for health check
    for _ in range(10):
        time.sleep(0.3)
        if proxy_status() is not None:
            break
    result = proxy_status()
    if result:
        print("[claude-proxy] Proxy restarted successfully.")
        plugins = result.get("plugins", [])
        print(f"  Plugins: {', '.join(plugins) if plugins else 'none'}")
    else:
        print("[claude-proxy] Proxy failed to start. Check ~/.claude/claude-proxy/proxy.log")
        sys.exit(1)


def cmd_status(args: argparse.Namespace) -> None:
    """Show proxy health status."""
    result = proxy_status()
    if result is None:
        print("[claude-proxy] Proxy is not running.")
        sys.exit(1)
    print("[claude-proxy] Proxy is running.")
    print(f"  Status:    {result.get('status', 'unknown')}")
    plugins = result.get("plugins", [])
    print(f"  Plugins:   {', '.join(plugins) if plugins else 'none'}")
    print(f"  Sideload:  {result.get('sideload_pending', 0)} pending")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="setup.py",
        description="claude-proxy -- manage the local Claude Code proxy.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("install", help="Full install: dirs, plugins, hooks, shell profile")
    sub.add_parser("uninstall", help="Full removal: kill proxy, clean all traces")

    add_p = sub.add_parser("add-plugin", help="Enable a plugin")
    add_p.add_argument("name", help="Plugin name (e.g. telegram)")

    rm_p = sub.add_parser("remove-plugin", help="Disable a plugin")
    rm_p.add_argument("name", help="Plugin name (e.g. telegram)")

    sub.add_parser("list-plugins", help="Show installed plugins and status")
    sub.add_parser("restart", help="Kill and restart the proxy")
    sub.add_parser("status", help="Show proxy health status")

    return parser


_COMMANDS = {
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "add-plugin": cmd_add_plugin,
    "remove-plugin": cmd_remove_plugin,
    "list-plugins": cmd_list_plugins,
    "restart": cmd_restart,
    "status": cmd_status,
}


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    _COMMANDS[args.command](args)


if __name__ == "__main__":
    main()

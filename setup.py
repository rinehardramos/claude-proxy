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
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import install


def _default_state_dir() -> Path:
    return Path("~/.claude/claude-proxy").expanduser()


def _default_project_dir() -> Path:
    return Path(__file__).resolve().parent


def cmd_install(args: argparse.Namespace) -> None:
    """Run the full install flow."""
    install.install(project_dir=_default_project_dir())


def cmd_uninstall(args: argparse.Namespace) -> None:
    """Full removal: kill proxy, delete runtime dir, clean settings + shell profile."""
    install.uninstall()


def cmd_add_plugin(args: argparse.Namespace) -> None:
    """Enable a plugin by name."""
    state_dir = _default_state_dir()
    project_dir = _default_project_dir()
    try:
        install.enable_plugin(state_dir, project_dir, args.name)
        print(f"[claude-proxy] Plugin '{args.name}' enabled.")
        print(f"  Config: {state_dir / 'plugins' / (args.name + '.toml')}")
        print("  The proxy will hot-reload the plugin automatically.")
    except FileNotFoundError as e:
        print(f"[claude-proxy] Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_remove_plugin(args: argparse.Namespace) -> None:
    """Disable a plugin by name (does not delete files)."""
    state_dir = _default_state_dir()
    try:
        install.disable_plugin(state_dir, args.name)
        print(f"[claude-proxy] Plugin '{args.name}' disabled.")
        print("  The proxy will hot-reload the change automatically.")
        print(f"  To re-enable: python3 setup.py add-plugin {args.name}")
    except FileNotFoundError as e:
        print(f"[claude-proxy] Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_list_plugins(args: argparse.Namespace) -> None:
    """Show installed plugins and their status."""
    state_dir = _default_state_dir()
    plugins = install.list_plugins(state_dir)
    if not plugins:
        print("[claude-proxy] No plugins installed.")
        return
    print("[claude-proxy] Installed plugins:")
    for name, enabled in plugins:
        marker = "✓" if enabled else "✗"
        status = "enabled" if enabled else "disabled"
        print(f"  {marker} {name:20s} [{status}]")


def cmd_status(args: argparse.Namespace) -> None:
    """Show proxy health status."""
    result = install.proxy_status()
    if result is None:
        print("[claude-proxy] Proxy is not running.")
        sys.exit(1)
    print(f"[claude-proxy] Proxy is running.")
    print(f"  Status:    {result.get('status', 'unknown')}")
    plugins = result.get("plugins", [])
    print(f"  Plugins:   {', '.join(plugins) if plugins else 'none'}")
    print(f"  Sideload:  {result.get('sideload_pending', 0)} pending")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="setup.py",
        description="claude-proxy — manage the local Claude Code proxy.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("install", help="Full install: dirs, plugins, hooks, shell profile")
    sub.add_parser("uninstall", help="Full removal: kill proxy, clean all traces")

    add_p = sub.add_parser("add-plugin", help="Enable a plugin")
    add_p.add_argument("name", help="Plugin name (e.g. telegram)")

    rm_p = sub.add_parser("remove-plugin", help="Disable a plugin")
    rm_p.add_argument("name", help="Plugin name (e.g. telegram)")

    sub.add_parser("list-plugins", help="Show installed plugins and status")
    sub.add_parser("status", help="Show proxy health status")

    return parser


_COMMANDS = {
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "add-plugin": cmd_add_plugin,
    "remove-plugin": cmd_remove_plugin,
    "list-plugins": cmd_list_plugins,
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

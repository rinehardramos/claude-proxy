#!/bin/sh
# claude-proxy SessionStart banner.
# Probes /status; prints a one-line status. If dead and no supervisor,
# attempts a best-effort start.

PORT="${CLAUDE_PROXY_PORT:-18019}"
URL="http://127.0.0.1:${PORT}/status"

# Try the probe. 2s timeout — we must not block Claude Code startup.
BODY=$(curl -sS --max-time 2 "$URL" 2>/dev/null)
RC=$?

if [ "$RC" -ne 0 ] || [ -z "$BODY" ]; then
    # Dead. Is a supervisor installed?
    if [ -f "$HOME/Library/LaunchAgents/com.claude-proxy.proxy.plist" ] \
       || [ -f "$HOME/.config/systemd/user/claude-proxy.service" ]; then
        echo "[claude-proxy] ⚠ proxy not responding; supervisor should respawn within 5s"
    else
        echo "[claude-proxy] starting proxy... (no supervisor installed; run 'proxy install' for auto-restart)"
        PROXY_PY="$(dirname "$0")/../proxy.py"
        if [ -f "$PROXY_PY" ]; then
            python3 "$PROXY_PY" --daemon >/dev/null 2>&1 &
        fi
    fi
    exit 0
fi

# Healthy — parse JSON with python3 (present on every target OS).
python3 - "$BODY" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    print("[claude-proxy] ok")
    sys.exit(0)
status = d.get("status", "ok")
uptime = d.get("uptime_s", 0)
rss = d.get("rss_mb", 0)
reloads = d.get("plugin_reloads", 0)
warnings = d.get("warnings", []) or []

def fmt_uptime(s):
    if s < 60: return f"{s}s"
    if s < 3600: return f"{s // 60}m"
    return f"{s // 3600}h"

if status == "warning" or warnings:
    detail = warnings[0] if warnings else f"uptime {fmt_uptime(uptime)}"
    print(f"[claude-proxy] ⚠ {detail} · {reloads} reloads · consider: proxy restart")
else:
    print(f"[claude-proxy] ok · uptime {fmt_uptime(uptime)} · rss {rss}MB · {reloads} reloads")
PY

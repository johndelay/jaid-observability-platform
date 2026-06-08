#!/usr/bin/env bash
# Deploy the watcher on a REMOTE Claude Code machine so it reports into a central dashboard.
# Run this ON the machine you want to monitor (it has Claude Code + python3).
#
#   CC_COLLECTOR_URL=http://<dashboard-host>:8099/ingest ./deploy-watcher.sh
#   # or pass the URL as the first argument:
#   ./deploy-watcher.sh http://localhost:8099/ingest
#
# For a laptop that travels, point CC_COLLECTOR_URL at the dashboard's Tailscale IP so it
# reports from anywhere on your tailnet (e.g. http://<tailscale-ip>:8099/ingest).
#
# Installs watcher.py + a systemd --user service that survives logout/reboot.
set -euo pipefail

COLLECTOR_URL="${CC_COLLECTOR_URL:-${1:-}}"
HOST_LABEL="${CC_HOST:-$(hostname)}"
INGEST_TOKEN="${CC_INGEST_TOKEN:-}"   # optional; required only if the dashboard has CC_INGEST_TOKEN set
if [ -z "$COLLECTOR_URL" ]; then
  echo "usage: CC_COLLECTOR_URL=http://<dashboard>:8099/ingest $0   (or pass the URL as arg1)" >&2
  echo "       set CC_INGEST_TOKEN=<token> too if the dashboard requires an ingest token" >&2
  exit 1
fi
command -v python3 >/dev/null || { echo "python3 required" >&2; exit 1; }

SRC="$(cd "$(dirname "$0")" && pwd)/watcher.py"
DEST="$HOME/.local/share/cc-observability"
mkdir -p "$DEST"
cp "$SRC" "$DEST/watcher.py"

UNIT="$HOME/.config/systemd/user/cc-watcher.service"
mkdir -p "$(dirname "$UNIT")"
TOKEN_LINE=""
[ -n "$INGEST_TOKEN" ] && TOKEN_LINE="Environment=CC_INGEST_TOKEN=$INGEST_TOKEN"
cat > "$UNIT" <<EOF
[Unit]
Description=cc-observability remote watcher
After=network-online.target

[Service]
Type=simple
Environment=CC_COLLECTOR_URL=$COLLECTOR_URL
Environment=CC_HOST=$HOST_LABEL
$TOKEN_LINE
ExecStart=/usr/bin/python3 $DEST/watcher.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"   # needed for systemctl --user over SSH
systemctl --user daemon-reload
systemctl --user enable cc-watcher.service
systemctl --user restart cc-watcher.service
loginctl enable-linger "$USER" 2>/dev/null || true

echo "✓ watcher deployed on '$HOST_LABEL' → $COLLECTOR_URL"
echo "  logs:   systemctl --user status cc-watcher.service"
echo "  remove: systemctl --user disable --now cc-watcher.service && rm -rf '$DEST' '$UNIT'"

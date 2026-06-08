#!/usr/bin/env bash
# Install + enable cc-observability as systemd --user services on this host.
set -euo pipefail
cd "$(dirname "$0")"

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cp systemd/cc-collector.service systemd/cc-watcher.service systemd/cc-responder.service "$UNIT_DIR/"

systemctl --user daemon-reload
# cc-responder enables answer-from-phone; it idles harmlessly until you set CC_ACCESS_PIN on the server.
systemctl --user enable --now cc-collector.service cc-watcher.service cc-responder.service

# Survive logout (optional but recommended for an always-on desktop):
loginctl enable-linger "$USER" 2>/dev/null || true

echo
systemctl --user --no-pager status cc-collector.service cc-watcher.service cc-responder.service | grep -E 'Active:|cc-' || true
echo
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):8099"
echo "Answer-from-phone: set CC_ACCESS_PIN (+ CC_INGEST_TOKEN) in .env, then restart the collector."

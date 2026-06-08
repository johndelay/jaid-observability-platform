#!/usr/bin/env bash
# Enable answer-from-phone (E5) on THIS host: install + start the cc-responder systemd --user service.
# Use this when the collector runs in Docker (so install-user-services.sh isn't used), e.g. my-desktop.
# The responder must run NATIVELY (not in the container) because it drives the host's tmux.
#
#   ./deploy-responder.sh
#
# It reuses the repo .env for CC_HOST + CC_INGEST_TOKEN (keep the token there — .env is gitignored).
# CC_DASHBOARD_URL defaults to http://localhost:8099. Answer-from-phone only activates once the server
# has CC_ACCESS_PIN set (the server refuses /reply without a PIN — injecting a reply is RCE into your shell).
set -euo pipefail
cd "$(dirname "$0")"

command -v python3 >/dev/null || { echo "python3 required" >&2; exit 1; }
command -v tmux    >/dev/null || echo "WARNING: tmux not found — replies can't be injected until it's installed" >&2

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cp systemd/cc-responder.service "$UNIT_DIR/"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"   # needed for systemctl --user over SSH
systemctl --user daemon-reload
systemctl --user enable --now cc-responder.service
loginctl enable-linger "$USER" 2>/dev/null || true

echo
systemctl --user --no-pager status cc-responder.service | grep -E 'Active:|cc-' || true
echo
if ! grep -q '^CC_ACCESS_PIN=' .env 2>/dev/null; then
  echo "NEXT: set CC_ACCESS_PIN (and CC_INGEST_TOKEN) in .env, then: docker compose up -d --force-recreate"
  echo "      (until a PIN is set, /reply is refused and the responder just idles)"
fi
echo "logs:   systemctl --user status cc-responder.service   /   journalctl --user -u cc-responder -f"
echo "remove: systemctl --user disable --now cc-responder.service && rm '$UNIT_DIR/cc-responder.service'"

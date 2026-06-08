#!/usr/bin/env bash
# Enable the in-app coach (V8 Slice B) on THIS host: install + start the cc-coach-worker systemd --user service.
# Use this when the collector runs in Docker (so install-user-services.sh isn't used), e.g. my-desktop.
# The worker must run NATIVELY (not in the container) because it drives the user's OWN `claude` CLI (or a
# local Ollama) — neither of which exists inside the container.
#
#   ./deploy-coach-worker.sh
#
# Reuses the repo .env for CC_INGEST_TOKEN (keep it there — .env is gitignored). Defaults:
#   COACH_ENGINE=auto   -> `claude -p` if the CLI is on PATH, else Ollama if CC_OLLAMA_URL is set, else off
#   CC_DASHBOARD_URL=http://localhost:8099
# Override by adding COACH_ENGINE / CC_OLLAMA_URL / CC_OLLAMA_MODEL to .env, then re-running this script.
set -euo pipefail
cd "$(dirname "$0")"

command -v python3 >/dev/null || { echo "python3 required" >&2; exit 1; }
if command -v claude >/dev/null; then
  echo "engine check: 'claude' CLI found -> the coach can use YOUR Claude (costs your tokens; kept lean)"
elif grep -q '^CC_OLLAMA_URL=' .env 2>/dev/null; then
  echo "engine check: no 'claude' CLI, but CC_OLLAMA_URL is set -> the coach will use Ollama"
else
  echo "WARNING: no 'claude' CLI and no CC_OLLAMA_URL in .env -> the worker idles (engine=off) until one exists" >&2
fi

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cp systemd/cc-coach-worker.service "$UNIT_DIR/"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"   # needed for systemctl --user over SSH
systemctl --user daemon-reload
systemctl --user enable --now cc-coach-worker.service
loginctl enable-linger "$USER" 2>/dev/null || true

echo
systemctl --user --no-pager status cc-coach-worker.service | grep -E 'Active:|cc-' || true
echo
echo "logs:   systemctl --user status cc-coach-worker.service   /   journalctl --user -u cc-coach-worker -f"
echo "remove: systemctl --user disable --now cc-coach-worker.service && rm '$UNIT_DIR/cc-coach-worker.service'"

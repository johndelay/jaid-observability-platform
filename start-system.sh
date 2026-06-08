#!/usr/bin/env bash
# Run collector + watcher in the foreground (Ctrl-C stops both). For testing.
# For persistent operation use ./install-user-services.sh instead.
set -u
cd "$(dirname "$0")"

export CC_PORT="${CC_PORT:-8099}"
export CC_WINDOW_1M_MODELS="${CC_WINDOW_1M_MODELS:-claude-opus-4-8}"
export CC_COLLECTOR_URL="${CC_COLLECTOR_URL:-http://localhost:${CC_PORT}/ingest}"

python3 server.py &
SPID=$!
sleep 1
python3 watcher.py &
WPID=$!

echo "collector + watcher up. Dashboard: http://$(hostname -I | awk '{print $1}'):${CC_PORT}"
trap 'kill $WPID $SPID 2>/dev/null' INT TERM
wait

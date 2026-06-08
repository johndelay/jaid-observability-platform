#!/usr/bin/env python3
"""cc-observability MCP-cost probe (MCP tax, Slice 2).

Runs on the HOST where `claude` lives (NOT inside the dashboard container — that has no `claude` binary and
can't see your config). It asks Claude Code for its configured MCP servers and reports the loadout to the
dashboard so the 🧩 MCP scene can show which servers are enabled vs. actually used ("dead weight").

🚫 HARD INVARIANT (non-negotiable): this probe ships server NAMES + connection STATUS only. It NEVER sends a
server's launch command, URL, headers, or env — `claude mcp list` prints those (e.g. a stdio server's argv can
embed `${API_TOKEN}` / a company URL) and `parse_mcp_list()` discards everything between the name and the
status. We never call `claude mcp get` (which can surface env/headers). No secrets ever leave the host.

Stdlib-only, zero third-party deps. Configure via env: CC_URL (dashboard base), CC_INGEST_TOKEN (same token the
watcher uses), CC_HOST (defaults to the hostname). Exit 0 on success, non-zero on failure.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

# Connection-status keywords as printed by `claude mcp list`, mapped to stable values. Order matters:
# the first keyword found in the trailing status text wins.
_STATUS = (("connected", "connected"), ("needs authentication", "needs_auth"), ("failed", "failed"))


def classify_status(tail):
    t = (tail or "").lower()
    for kw, val in _STATUS:
        if kw in t:
            return val
    return "unknown"


def parse_mcp_list(text):
    """Parse `claude mcp list` stdout into [{name, status}], keeping NOTHING but name + status.

    Each server line looks like:  ``<name>: <target> - <status>``  e.g.
        ``github: https://api.githubcopilot.com/mcp/ (HTTP) - ✗ Failed to connect``
        ``jira: mcp-atlassian --jira-token ${JIRA_API_TOKEN} ... - ✓ Connected``
    We take the name (before the first ``": "``) and the status (after the last ``" - "``); the ``<target>``
    in between — which may hold secrets — is dropped and never returned. Non-server lines (the health header,
    the diagnostics footer, the config-location line) don't end in a known status, so they're skipped.
    """
    servers, seen = [], set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or ": " not in line or " - " not in line:
            continue
        name = line.split(": ", 1)[0].strip()
        status = classify_status(line.rsplit(" - ", 1)[1])
        if status == "unknown" or not name or name in seen:
            continue
        seen.add(name)
        servers.append({"name": name, "status": status})
    return servers


def collect():
    """Run `claude mcp list` and return the parsed (secret-free) server list. Raises on subprocess failure."""
    out = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True, timeout=90)
    return parse_mcp_list(out.stdout)


def main():
    url = os.environ.get("CC_URL", "http://localhost:8099").rstrip("/")
    token = os.environ.get("CC_INGEST_TOKEN", "")
    host = os.environ.get("CC_HOST") or socket.gethostname()
    try:
        servers = collect()
    except Exception as e:  # noqa: BLE001
        print("mcp-probe: `claude mcp list` failed:", e, file=sys.stderr)
        return 1
    payload = {"host": host, "ts": time.time(), "servers": servers}
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-CC-Token"] = token
    req = urllib.request.Request(url + "/mcp-ingest", data=json.dumps(payload).encode(), headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=15)
        print("mcp-probe: reported %d server(s) to %s -> %s" % (len(servers), url, r.status))
    except Exception as e:  # noqa: BLE001
        print("mcp-probe: POST /mcp-ingest failed:", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

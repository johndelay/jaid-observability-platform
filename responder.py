#!/usr/bin/env python3
"""
cc-observability — responder (the host-side of answer-from-phone, E5)
====================================================================
Long-polls the dashboard's /outbox for replies addressed to THIS host, injects each
into the right tmux pane (via tmux_inject), and reports the outcome back to /outbox/result
(which unblocks the waiting phone request). Stdlib only.

Why a separate daemon (not the watcher, not the server):
  - tmux lives on the host; the dashboard is a Docker container that can't reach the host's
    tmux socket. The watcher is read-only by design. So the WRITE path is its own opt-in
    process with its own auth — run it only on hosts where you want to answer from your phone.

Security: this turns a network reply into keystrokes in your shell. Run it only behind the
PIN/token gates (the server refuses /reply without CC_ACCESS_PIN) and on a trusted network
(LAN / Tailscale) — never expose the dashboard to the public edge.

Enable on a host:
  CC_DASHBOARD_URL=http://<dashboard-host>:8099 CC_HOST=$(hostname) \
  CC_INGEST_TOKEN=<same-as-server> python3 responder.py
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import tmux_inject

# Base dashboard URL. Default reuses CC_COLLECTOR_URL (the watcher's .../ingest) with /ingest stripped.
def _default_base():
    cu = os.environ.get("CC_COLLECTOR_URL", "")
    if cu.endswith("/ingest"):
        return cu[: -len("/ingest")]
    return "http://localhost:8099"


BASE        = os.environ.get("CC_DASHBOARD_URL", _default_base()).rstrip("/")
HOST        = os.environ.get("CC_HOST", socket.gethostname())
TOKEN       = os.environ.get("CC_INGEST_TOKEN", "")
# Client read timeout must exceed the server's long-poll hold (CC_OUTBOX_WAIT, default 25s).
POLL_TIMEOUT = float(os.environ.get("CC_RESPONDER_POLL_TIMEOUT", "35"))
BACKOFF      = float(os.environ.get("CC_RESPONDER_BACKOFF", "3"))   # sleep after a connection error


def log(*a):
    print(f"[responder {time.strftime('%H:%M:%S')}]", *a, file=sys.stderr, flush=True)


def _headers():
    h = {"Content-Type": "application/json"}
    if TOKEN:
        h["X-CC-Token"] = TOKEN
    return h


def poll_once():
    """Block on /outbox until a reply arrives or the server's hold expires. Returns the reply dict or None."""
    req = urllib.request.Request(f"{BASE}/outbox?host={urllib.parse.quote(HOST)}", headers=_headers())
    with urllib.request.urlopen(req, timeout=POLL_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", "replace")
    d = json.loads(body) if body.strip() else {}
    return d if d.get("reply_id") else None


def report(reply_id, ok, detail, target=None):
    payload = json.dumps({"reply_id": reply_id, "ok": ok, "detail": detail, "target": target}).encode()
    req = urllib.request.Request(f"{BASE}/outbox/result", data=payload, headers=_headers())
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # noqa: BLE001 — best effort; phone falls back to its own timeout
        log("result POST failed:", e)


def handle(reply):
    rid, sid, text = reply["reply_id"], reply.get("session_id"), reply.get("text", "")
    try:
        res = tmux_inject.deliver(sid, text)
        log(f"injected reply {rid} -> {res['target']} ({res['chars']} chars)")
        report(rid, True, "delivered", res["target"])
    except tmux_inject.InjectError as e:
        log(f"inject failed for {rid}: {e.reason} ({e.detail})")
        report(rid, False, f"{e.reason}: {e.detail}".strip(": "))
    except Exception as e:  # noqa: BLE001 — never let one bad reply hang the phone request
        log(f"inject error for {rid}: {e}")
        report(rid, False, f"error: {e}")


def main():
    log(f"host={HOST} -> {BASE}/outbox  (token={'set' if TOKEN else 'none'})")
    while True:
        try:
            reply = poll_once()
            if reply:
                handle(reply)
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            # server down / network blip / poll timed out with nothing — back off briefly and retry.
            time.sleep(BACKOFF)
        except Exception as e:  # noqa: BLE001
            log("unexpected poll error:", e)
            time.sleep(BACKOFF)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass

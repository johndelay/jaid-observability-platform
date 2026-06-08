#!/usr/bin/env python3
"""
cc-observability — coach worker (the host-side of the V8 in-app coach)
=====================================================================
Polls the dashboard's /coach-job for a pending narrative request, generates plain-English coaching from the
**content-free metrics bundle** the server hands it, and POSTs the result to /coach-result (which the 🧠 Coach
scene then shows). Stdlib only.

Why a separate host process (not the container, not the watcher):
  - The dashboard is a Docker container with neither the `claude` CLI nor the user's Claude login. The whole
    point of this slice is to coach with the USER'S OWN Claude — which only exists here on the host. So the
    generate step runs natively (like cc-responder runs tmux), and only the finished text crosses back.

Engines (COACH_ENGINE):
  auto  (default) — `claude -p` if the CLI is on PATH, else Ollama if CC_OLLAMA_URL is set & reachable, else off
  claude          — the user's own Claude Code CLI (`claude -p`); costs the user's tokens (keep prompts lean)
  ollama          — a local Ollama (CC_OLLAMA_URL / CC_OLLAMA_MODEL); free/offline, the opt-in pivot
  off             — never generate (the scene still shows metrics + the /claude-coach handoff)

INVARIANTS: content-free in (only the server's aggregate bundle — never transcripts); both engines are LOCAL
(the user's Claude / a LAN Ollama), never a third-party relay. Graceful no-op on every failure.

Enable on a host (reuses the repo .env for CC_INGEST_TOKEN, same as cc-responder):
  CC_DASHBOARD_URL=http://localhost:8099 CC_INGEST_TOKEN=<same-as-server> python3 coach_worker.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import coach   # build_prompt / flatten — shared with the server so prompt == what the server intends

BASE      = os.environ.get("CC_DASHBOARD_URL", "http://localhost:8099").rstrip("/")
HOST      = os.environ.get("CC_HOST", socket.gethostname())
TOKEN     = os.environ.get("CC_INGEST_TOKEN", "")
ENGINE    = os.environ.get("COACH_ENGINE", "auto").lower()
POLL      = float(os.environ.get("CC_COACH_POLL", "12"))          # seconds between /coach-job polls
CLAUDE_BIN   = os.environ.get("CC_CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.environ.get("CC_COACH_CLAUDE_MODEL", "")        # optional --model for `claude -p`
GEN_TIMEOUT  = float(os.environ.get("CC_COACH_GEN_TIMEOUT", "180"))
OLLAMA_URL   = os.environ.get("CC_OLLAMA_URL", "").rstrip("/")
OLLAMA_MODEL = os.environ.get("CC_OLLAMA_MODEL", "hermes3:8b-16k")


def log(*a):
    print(f"[coach-worker {time.strftime('%H:%M:%S')}]", *a, file=sys.stderr, flush=True)


def _headers():
    h = {"Content-Type": "application/json"}
    if TOKEN:
        h["X-CC-Token"] = TOKEN
    return h


# ---------------- engines ----------------
def _claude_available():
    return shutil.which(CLAUDE_BIN) is not None


def gen_claude(prompt):
    """Generate via the user's own Claude Code CLI (`claude -p`). Returns text or None."""
    cmd = [CLAUDE_BIN, "-p", coach.flatten(prompt)]
    if CLAUDE_MODEL:
        cmd[1:1] = ["--model", CLAUDE_MODEL]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=GEN_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as e:
        log("claude engine failed:", e)
        return None
    if r.returncode != 0:
        log("claude engine non-zero:", (r.stderr or "").strip()[:200])
        return None
    return (r.stdout or "").strip() or None


def _ollama_available():
    if not OLLAMA_URL:
        return False
    try:
        urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=4).read()
        return True
    except Exception:  # noqa: BLE001
        return False


def gen_ollama(prompt):
    """Generate via a local Ollama (/api/generate, non-streaming). Returns text or None."""
    body = json.dumps({"model": OLLAMA_MODEL, "system": prompt["system"], "prompt": prompt["user"],
                       "stream": False}).encode()
    req = urllib.request.Request(OLLAMA_URL + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=GEN_TIMEOUT) as resp:
            d = json.loads(resp.read().decode("utf-8", "replace"))
        return (d.get("response") or "").strip() or None
    except Exception as e:  # noqa: BLE001
        log("ollama engine failed:", e)
        return None


def pick_engine():
    """Resolve COACH_ENGINE=auto to a concrete engine name, honoring availability. Returns one of
    'claude' | 'ollama' | 'off'."""
    if ENGINE == "off":
        return "off"
    if ENGINE == "claude":
        return "claude" if _claude_available() else "off"
    if ENGINE == "ollama":
        return "ollama" if _ollama_available() else "off"
    # auto: prefer the user's own Claude (everyone has it), fall back to a local Ollama, else off
    if _claude_available():
        return "claude"
    if _ollama_available():
        return "ollama"
    return "off"


def generate(bundle):
    """Build the prompt and run the resolved engine. Returns (narrative, engine_name) — narrative None on
    no-op/failure (the scene then just keeps showing metrics + the /claude-coach handoff)."""
    eng = pick_engine()
    if eng == "off":
        return None, "off"
    prompt = coach.build_prompt(bundle)
    text = gen_claude(prompt) if eng == "claude" else gen_ollama(prompt)
    return text, eng


# ---------------- dashboard I/O ----------------
def claim_job():
    """Poll /coach-job. Returns the job dict {account, bundle} when one is pending, else None."""
    req = urllib.request.Request(f"{BASE}/coach-job?host={urllib.parse.quote(HOST)}", headers=_headers())
    with urllib.request.urlopen(req, timeout=10) as resp:
        d = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    return d if d.get("pending") else None


def post_result(narrative, engine):
    payload = json.dumps({"narrative": narrative, "engine": engine,
                          "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}).encode()
    req = urllib.request.Request(f"{BASE}/coach-result", data=payload, headers=_headers())
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # noqa: BLE001
        log("result POST failed:", e)


def handle(job):
    bundle = job.get("bundle") or {}
    narrative, eng = generate(bundle)
    if not narrative:
        log(f"no narrative (engine={eng}) — leaving the scene on metrics + handoff")
        return
    log(f"generated narrative via {eng} ({len(narrative)} chars)")
    post_result(narrative, eng)


def main():
    log(f"host={HOST} -> {BASE}  engine={ENGINE} (resolved={pick_engine()})  token={'set' if TOKEN else 'none'}")
    while True:
        try:
            job = claim_job()
            if job:
                handle(job)
        except (urllib.error.URLError, socket.timeout, ConnectionError):
            pass     # dashboard down / blip — just retry next tick
        except Exception as e:  # noqa: BLE001
            log("unexpected poll error:", e)
        time.sleep(POLL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass

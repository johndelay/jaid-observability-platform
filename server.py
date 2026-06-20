#!/usr/bin/env python3
"""
cc-observability collector + web server
=======================================
Receives session snapshots from watcher.py, applies window/threshold policy,
and serves a mobile-first fuel-gauge dashboard over SSE. Stdlib only.

Routes:
  POST /ingest   <- watcher snapshots (JSON body)
  GET  /state    -> JSON list of computed session states
  GET  /events   -> Server-Sent Events stream (live push)
  GET  /         -> mobile dashboard (web/index.html)
  GET  /health   -> "ok"
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import version
import crypto
import fsperms

# ---- config (env-overridable) ------------------------------------------------
PORT             = int(os.environ.get("CC_PORT", "8099"))
WINDOW_DEFAULT   = int(os.environ.get("CC_WINDOW_DEFAULT", "200000"))
# Comma-separated model-id substrings to treat as 1M-window. The transcript records
# the model WITHOUT the [1m] marker, so e.g. a 1M Opus session looks like a 200k one
# until it crosses 200k. Declare your 1M models here (e.g. "claude-opus-4-8").
WINDOW_1M_MODELS = [m.strip() for m in os.environ.get("CC_WINDOW_1M_MODELS", "").split(",") if m.strip()]
COMPACT_THRESH   = float(os.environ.get("CC_COMPACT_THRESHOLD", "0.95"))
STALE_SECS       = float(os.environ.get("CC_STALE_SECS", "120"))      # no update -> dim
# A "waiting" session whose last turn finished longer ago than this is an old parked terminal, not
# "needs me now" → demote it from the NEEDS-YOU hero to idle. Applies to derived + enrichment uniformly.
WAITING_RECENT_SECS = float(os.environ.get("CC_WAITING_RECENT_SECS", "1800"))   # 30 min
# "Needs you" = Claude is actually blocked on you (permission Notification or an AskUserQuestion), NOT just
# that a turn finished (the Stop hook fires "waiting" on every completion). Set CC_STRICT_NEEDS_ME=0 to
# restore the old behavior where any finished turn surfaces in the hero.
STRICT_NEEDS_ME  = os.environ.get("CC_STRICT_NEEDS_ME", "1") not in ("0", "false", "False", "")
EXPIRE_SECS      = float(os.environ.get("CC_EXPIRE_SECS", "21600"))   # 6h -> drop from view
# P4 fleet reporting-health. A host is GREEN while we've heard from it recently (a session POST, a beacon,
# or — for the local-scan host — a scan pass), AMBER if it's gone quiet (missed a beat), RED if silent past
# the stale bound (watcher down / host off / erroring). This is independent of whether the host currently
# has any active sessions — the whole point is to tell "idle but healthy" apart from "broken".
HOST_FRESH_SECS    = float(os.environ.get("CC_HOST_FRESH_SECS", "45"))     # < this since last contact -> green
HOST_STALE_SECS    = float(os.environ.get("CC_HOST_STALE_SECS", "180"))    # >= this -> red (not reporting)
REJECT_WINDOW_SECS = float(os.environ.get("CC_REJECT_WINDOW_SECS", "300")) # only surface auth-rejected POSTs this recent
# Read this host's own transcripts directly, in-process (no separate watcher). This is the
# single-host default. Remote hosts instead run watcher.py and POST to /ingest.
LOCAL_SCAN       = os.environ.get("CC_LOCAL_SCAN", "").lower() in ("1", "true", "yes")
POLL_INTERVAL    = float(os.environ.get("CC_POLL_INTERVAL", "10"))    # transcript scan cadence (light by default)
SSE_INTERVAL     = float(os.environ.get("CC_SSE_INTERVAL", "5"))      # how often SSE checks for changes (sends only on change)
SSE_HEARTBEAT    = float(os.environ.get("CC_SSE_HEARTBEAT", "30"))    # keep-alive ping when nothing changes
WEB_DIR          = os.environ.get("CC_WEB_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
# Basic security (opt-in, Local-First). CC_ACCESS_PIN gates the browser UI behind a PIN→cookie;
# unset = open (LAN trust). CC_INGEST_TOKEN gates /ingest from remote watchers (X-CC-Token header).
ACCESS_PIN       = os.environ.get("CC_ACCESS_PIN", "")
INGEST_TOKEN     = os.environ.get("CC_INGEST_TOKEN", "")
# CC_AUTH_SALT lets an operator INVALIDATE all existing login cookies without changing the PIN: the cookie is
# HMAC(PIN, "cc-observability-v1|" + salt), so rotating the salt (any new value) logs every device out. Leave
# it unset for the default; set/change it to revoke a leaked cookie. (Empty salt also changes the v1 cookie
# once, on first deploy of this build — a harmless one-time re-login.)
AUTH_SALT        = os.environ.get("CC_AUTH_SALT", "")
AUTH_COOKIE      = hmac.new(ACCESS_PIN.encode(), b"cc-observability-v1|" + AUTH_SALT.encode(), hashlib.sha256).hexdigest() if ACCESS_PIN else ""
# Answer-from-phone (E5). The reply travels: phone -> POST /reply (PIN-gated) -> _outbox queued under the
# session's host -> the host's responder.py long-polls GET /outbox -> injects via tmux -> POST /outbox/result
# -> unblocks /reply with the outcome. /reply REFUSES unless a PIN is set (injecting = RCE into the shell).
REPLY_TIMEOUT    = float(os.environ.get("CC_REPLY_TIMEOUT", "20"))   # /reply blocks this long for a responder
OUTBOX_WAIT      = float(os.environ.get("CC_OUTBOX_WAIT", "25"))     # responder long-poll hold time
REPLY_MAX_LEN    = int(os.environ.get("CC_REPLY_MAX_LEN", "4000"))

WINDOW_TIERS = [200_000, 1_000_000]
DB_POLL = float(os.environ.get("CC_DB_POLL", "60"))   # transcript→store ingest cadence (history; light)
# V9 local transcript search (the ONE content-bearing feature). A SECOND listener on CONTENT_PORT is
# published to host loopback only (compose: 127.0.0.1:8100:8100), so raw content is reachable solely from
# a browser ON the host. The bound port — not a spoofable header or a Docker-masqueraded source IP — is the
# local-only boundary (see Handler._content_local). cc-content.db is opt-in + isolated from the cost DB.
CONTENT_PORT     = int(os.environ.get("CC_CONTENT_PORT", "8100"))
CONTENT_DB_PATH  = os.environ.get("CC_CONTENT_DB_PATH", "")          # blank → content_index.DEFAULT_PATH
CONTENT_POLL     = float(os.environ.get("CC_CONTENT_POLL", "45"))    # index-pass cadence (only acts while enabled)
# Runtime (docker|native) drives the content-firewall bind. In Docker the container binds 0.0.0.0 and the
# compose publishes the content port to host loopback only; native has no Docker masquerade, so we bind the
# content listener to 127.0.0.1 directly (the LAN main port stays 0.0.0.0 for the phone). Either way the
# port-based _content_local check is the real boundary.
RUNTIME          = version.runtime()
CONTENT_BIND     = "127.0.0.1" if RUNTIME == "native" else "0.0.0.0"
MAINT_INTERVAL   = float(os.environ.get("CC_MAINT_INTERVAL", "300"))   # auto WAL-checkpoint cadence (lossless)
EXPORT_DIR       = os.environ.get("CC_EXPORT_DIR", "/data/exports")    # where export bundles land (host-mounted)
# Self-update check (V13 Slice 6) — OPT-IN: only polls when CC_UPDATE_URL is set. It's an outbound GET of a
# tiny version manifest ({"version":"x.y.z"}); ZERO user data is ever sent. Decoupled from the code repo's
# visibility (point it at any public version.json) so it never forces a premature public flip. Never auto-applies.
UPDATE_URL       = os.environ.get("CC_UPDATE_URL", "").strip()
UPDATE_INTERVAL  = float(os.environ.get("CC_UPDATE_INTERVAL", "21600"))   # 6h
_update = {"current": version.VERSION, "latest": None, "update_available": False,
           "verb": version.upgrade_verb(), "checked_ts": None}

_state = {}            # session_id -> snapshot (+ reported_at)
_lock = threading.Lock()
_store = None          # usage/cost store (Sprint 3); set up in main(), stays None in unit tests
_content = None        # V9 content search index (cc-content.db); set up in main(), None in unit tests
_hosts = {}            # host -> {last_post_ok_at, source}; per-host reporting-health rollup (P4)
_rejected = {}         # source_ip -> {count, last_at}; auth-rejected /ingest POSTs (misconfigured watcher)

# ---- /login brute-force throttle (the PIN is a single low-entropy secret on a LAN-reachable port; without
# this a 4-digit PIN falls in seconds). Per-IP failed-attempt counter → temporary lockout. -------------------
LOGIN_MAX_FAILS = int(os.environ.get("CC_LOGIN_MAX_FAILS", "10"))   # fails within the window → lockout
LOGIN_WINDOW    = float(os.environ.get("CC_LOGIN_WINDOW", "300"))   # rolling window (s) for counting fails
LOGIN_BLOCK     = float(os.environ.get("CC_LOGIN_BLOCK", "300"))    # lockout duration (s) once tripped
_login_fails = {}                      # client_ip -> {count, first_at, blocked_until}
_login_lock = threading.Lock()

def _login_blocked_for(ip):
    """Seconds remaining if this IP is currently locked out, else 0."""
    now = time.time()
    with _login_lock:
        rec = _login_fails.get(ip)
        if rec and rec.get("blocked_until", 0) > now:
            return rec["blocked_until"] - now
    return 0

def _login_note_fail(ip):
    now = time.time()
    with _login_lock:
        if len(_login_fails) > 4096:     # bound memory against spoofed-IP floods
            _login_fails.clear()
        rec = _login_fails.get(ip)
        if not rec or now - rec.get("first_at", 0) > LOGIN_WINDOW:
            rec = {"count": 0, "first_at": now, "blocked_until": 0}
        rec["count"] += 1
        if rec["count"] >= LOGIN_MAX_FAILS:
            rec["blocked_until"] = now + LOGIN_BLOCK
        _login_fails[ip] = rec

def _login_note_ok(ip):
    with _login_lock:
        _login_fails.pop(ip, None)

# ---- answer-from-phone reply queue (the write path) --------------------------
_outbox = {}                          # reply_id -> {host, session_id, text, status, result, created}
_outbox_cond = threading.Condition()  # guards _outbox; wakes long-pollers on both ends


def enqueue_reply(host, session_id, text):
    """Queue a reply for the host's responder; returns the record (poll its result via wait_reply_result)."""
    rid = secrets.token_urlsafe(8)
    rec = {"reply_id": rid, "host": host, "session_id": session_id, "text": text,
           "status": "queued", "result": None, "created": time.time()}
    with _outbox_cond:
        _outbox[rid] = rec
        _outbox_cond.notify_all()
    return rec


def wait_reply_result(rec, timeout):
    """Block until a responder reports this reply's outcome, or timeout (-> None, reply expired)."""
    deadline = time.time() + timeout
    with _outbox_cond:
        while rec["result"] is None and time.time() < deadline:
            _outbox_cond.wait(deadline - time.time())
        if rec["result"] is None:
            rec["status"] = "expired"
            _outbox.pop(rec["reply_id"], None)
        return rec["result"]


def claim_reply(host, timeout):
    """Responder side: return the oldest queued reply for `host` (marking it dispatched), or None on timeout."""
    deadline = time.time() + timeout
    with _outbox_cond:
        while True:
            cands = [r for r in _outbox.values() if r["host"] == host and r["status"] == "queued"]
            if cands:
                rec = min(cands, key=lambda r: r["created"])
                rec["status"] = "dispatched"
                return rec
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            _outbox_cond.wait(remaining)


def complete_reply(reply_id, result):
    """Responder side: attach the inject outcome and wake the blocked /reply. False if it already expired."""
    with _outbox_cond:
        rec = _outbox.get(reply_id)
        if rec is None:
            return False
        rec["result"] = result
        rec["status"] = "done"
        _outbox_cond.notify_all()
        return True


# --- V8 Slice B: coach narrative job (phone requests → host coach-worker generates → posts back) ---
# The container can't run the user's `claude` (no CLI, no login), so the narrative is produced by a native
# host coach-worker. Flow mirrors /outbox: phone POST /coach-request (PIN) sets pending; the worker polls
# GET /coach-job (token), generates with `claude -p` (default) or Ollama, POSTs /coach-result (token); the
# 🧠 scene polls GET /coach and shows it. Single in-flight job (not a queue) — coaching is a whole-account view.
_coach_lock = threading.Lock()
_coach_job = {"pending": False, "requested_at": None, "account": None, "claimed_at": None,
              "narrative": None, "engine": None, "generated_at": None}
COACH_CLAIM_TTL = float(os.environ.get("CC_COACH_CLAIM_TTL", "180"))   # a stale claim re-opens (dead worker)


def coach_request(account):
    with _coach_lock:
        _coach_job.update(pending=True, requested_at=time.time(), account=account, claimed_at=None)


def coach_claim():
    """Worker side: claim the pending job (None if none / already claimed within the TTL). Returns {account}."""
    with _coach_lock:
        if not _coach_job["pending"]:
            return None
        ca = _coach_job["claimed_at"]
        if ca and (time.time() - ca) < COACH_CLAIM_TTL:
            return None                      # another worker pass already owns it
        _coach_job["claimed_at"] = time.time()
        return {"account": _coach_job["account"]}


def coach_complete(narrative, engine, generated_at, persist=None):
    with _coach_lock:
        _coach_job.update(pending=False, claimed_at=None, narrative=narrative, engine=engine,
                          generated_at=generated_at)
    if persist is not None:
        try:
            persist.set_meta("coach_last", json.dumps(
                {"narrative": narrative, "engine": engine, "generated_at": generated_at}))
        except Exception:  # noqa: BLE001 — caching is best-effort
            pass


def coach_state():
    with _coach_lock:
        return dict(_coach_job)


def coach_load(persist):
    """Restore the last narrative from the store meta on startup so the scene isn't blank after a restart."""
    try:
        raw = persist.get_meta("coach_last") if persist else None
        if raw:
            d = json.loads(raw)
            with _coach_lock:
                _coach_job.update(narrative=d.get("narrative"), engine=d.get("engine"),
                                  generated_at=d.get("generated_at"))
    except Exception:  # noqa: BLE001
        pass


def window_for(context_tokens, model):
    """Pick the context-window size. Transcript model lacks the [1m] marker, so
    auto-escalate: exceeding 200k can only happen on a 1M-window session."""
    w = WINDOW_DEFAULT
    if model and ("[1m]" in model or any(m in model for m in WINDOW_1M_MODELS)):
        w = max(w, 1_000_000)
    for tier in WINDOW_TIERS:
        if context_tokens <= tier:
            w = max(w, tier)
            break
    else:
        w = max(w, WINDOW_TIERS[-1])
    if context_tokens > w:
        w = WINDOW_TIERS[-1]
    return w


def _wait_age_secs(iso, now):
    """Seconds since a waiting session's last turn finished, from an ISO8601 timestamp. None if unparseable."""
    if not iso:
        return None
    try:
        return now - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def last_msg(activity):
    """The most recent thing Claude said — its last text, or an AskUserQuestion (question + options).
    Drives the hero-card preview so you can see WHAT a waiting session is asking without drilling in."""
    for it in reversed(activity or []):
        k = it.get("kind")
        if k == "ask":
            return {"kind": "ask", "text": it.get("question", ""), "options": it.get("options", [])}
        if k == "text" and (it.get("text") or "").strip():
            return {"kind": "text", "text": it["text"].strip()[:200], "options": []}
    return None


def compute(snap, now):
    ctx = int(snap.get("context_tokens", 0) or 0)
    auth_window = snap.get("auth_window")          # authoritative size from the statusline feed
    window = int(auth_window) if auth_window else window_for(ctx, snap.get("model"))
    compact_at = int(window * COMPACT_THRESH)
    pct = (ctx / window) if window else 0.0
    pct_to_compact = (ctx / compact_at) if compact_at else 0.0
    age = now - snap.get("reported_at", now)
    stale = age > STALE_SECS
    state = snap.get("state") or "unknown"
    lm = last_msg(snap.get("activity"))
    last_hook = snap.get("last_hook")
    notif_kind = snap.get("notif_kind")
    needs_me = bool(snap.get("needs_me"))
    # recency bound: an old turn-complete terminal is "idle", not "needs me now"
    wait_age = _wait_age_secs(snap.get("awaiting_input_since"), now)
    if needs_me and wait_age is not None and wait_age > WAITING_RECENT_SECS:
        needs_me = False
    # "Needs you" must mean Claude is actually BLOCKED on you, not merely that a turn finished. Both the
    # transcript baseline (the last non-sidechain msg is an `assistant` turn on every completion) and the
    # Stop hook write "waiting" after EVERY turn — and the Notification hook ALSO fires on a mere idle
    # "waiting for your input" nudge — so any of those would otherwise dominate the hero. Treat it as
    # "needs you" only when:
    #   - a PERMISSION-type Notification fired (notif_kind == "permission"; the hook classifies the message
    #     so an idle nudge is excluded), or
    #   - Claude left an explicit AskUserQuestion (last_msg.kind == "ask").
    # Otherwise demote to idle. (Watcher-only hosts have no hooks → only the explicit-ask signal survives;
    # they can't observe permission waits anyway.) Tunable off via CC_STRICT_NEEDS_ME=0 for old behavior.
    blocking = (notif_kind == "permission") or (lm is not None and lm.get("kind") == "ask")
    if STRICT_NEEDS_ME and needs_me and not blocking:
        needs_me = False
    # effective state for the fleet summary: waiting (needs you) > working (fresh) > idle (stale/quiet)
    if needs_me:
        eff = "waiting"
    elif state == "working" and not stale:
        eff = "working"
    else:
        eff = "idle"
    return {
        "host": snap.get("host"),
        "account": snap.get("account"),
        "account_approx": snap.get("account_approx") is not False,   # default to approximate; exact only if explicitly so
        "extra_usage": snap.get("extra_usage"),                      # account can pay overage past the plan cap (Extra Usage)
        "session_id": snap.get("session_id"),
        "project": snap.get("project"),
        "git_branch": snap.get("git_branch"),
        "model": snap.get("model"),
        "context_tokens": ctx,
        "window": window,
        "authoritative": bool(auth_window),
        "pct": round(pct * 100, 1),                   # raw fill = ctx / full window (retained; tests + internals)
        "compact_at": compact_at,
        "tokens_until_compact": max(0, compact_at - ctx),
        "pct_to_compact": round(pct_to_compact * 100, 1),
        # The gauge shows distance-to-auto-compact, not raw fill. Prefer Claude Code's own authoritative
        # used_percentage (hits exactly 100% when compaction fires) when the statusline feed supplies it;
        # fall back to our computed pct_to_compact on watcher-only hosts that have no statusline.
        "pct_compact": (snap.get("auth_used_pct") if snap.get("auth_used_pct") is not None
                        else round(pct_to_compact * 100, 1)),
        "auth_used_pct": snap.get("auth_used_pct"),   # Claude Code's own used_percentage (None = no statusline feed)
        "cost_usd": snap.get("cost_usd"),
        "rate_limits": snap.get("rate_limits"),
        "state": state,
        "eff_state": eff,
        "needs_me": needs_me,
        "last_hook": last_hook,                       # which hook last set state (Stop/Notification/…); None on watcher-only hosts
        "notif_kind": notif_kind,                     # "permission" | "idle" | None — why a Notification fired (gates needs_me)
        # answer-from-phone affordance: a tmux target was recorded for this session (answerable) AND a PIN
        # is configured (reply_enabled). The UI shows a reply box only when both are true.
        "answerable": bool(snap.get("answerable")),
        "reply_enabled": bool(ACCESS_PIN),
        "last_msg": lm,
        "awaiting_input_since": snap.get("awaiting_input_since"),
        "sidechain_recent": snap.get("sidechain_recent", 0),
        "mcp_observed": snap.get("mcp_observed") or {},      # observed MCP usage (Slice 1, "MCP tax")
        "subagents": snap.get("subagents", []),
        "subagent_running": snap.get("subagent_running", 0),
        "age_secs": round(age, 1),
        "stale": stale,
    }


def record_host_ok(host, source="ingest", now=None):
    """Mark that we successfully heard from `host` — an accepted /ingest POST, a heartbeat beacon, or
    (for the in-process collector) a local scan pass. Drives the GREEN state in host_health."""
    if not host:
        return
    now = now if now is not None else time.time()
    with _lock:
        _hosts[host] = {"last_post_ok_at": now, "source": source}


def record_rejected(ip, now=None):
    """Count an auth-rejected /ingest POST by source IP. We can't trust the body on a 401 (so no hostname),
    but a watcher POSTing with a wrong/absent token is misconfigured and must surface as RED — exactly the
    P1 case that 401'd silently for days."""
    if not ip:
        return
    now = now if now is not None else time.time()
    with _lock:
        rec = _rejected.get(ip) or {"count": 0, "last_at": 0}
        rec["count"] += 1
        rec["last_at"] = now
        _rejected[ip] = rec


def _extra_usage_accounts():
    """Accounts currently reporting Extra Usage ENABLED (from live snapshots) — overage only bills for these."""
    with _lock:
        snaps = list(_state.values())
    return {s["account"] for s in snaps if s.get("extra_usage") and s.get("account")}


def host_health(now=None):
    """Per-host reporting-health rollup + recent auth-rejections (P4). Color reflects only whether the
    host's watcher is reaching us, independent of whether it has active sessions right now."""
    now = now if now is not None else time.time()
    rank = {"red": 0, "amber": 1, "green": 2}
    with _lock:
        counts = {}
        for v in _state.values():
            h = v.get("host")
            if h:
                counts[h] = counts.get(h, 0) + 1
        hosts = []
        for h, rec in _hosts.items():
            age = now - rec["last_post_ok_at"]
            status = "green" if age < HOST_FRESH_SECS else "amber" if age < HOST_STALE_SECS else "red"
            hosts.append({"host": h, "status": status, "age_secs": round(age, 1),
                          "sessions": counts.get(h, 0), "source": rec.get("source", "ingest")})
        rejected = [{"ip": ip, "count": r["count"], "age_secs": round(now - r["last_at"], 1)}
                    for ip, r in _rejected.items() if now - r["last_at"] < REJECT_WINDOW_SECS]
    hosts.sort(key=lambda x: (rank[x["status"]], x["host"]))   # red first (most urgent)
    rejected.sort(key=lambda x: -x["count"])
    return {"hosts": hosts, "rejected": rejected,
            "thresholds": {"fresh_secs": HOST_FRESH_SECS, "stale_secs": HOST_STALE_SECS}}


def snapshot_state():
    now = time.time()
    with _lock:
        for sid in [s for s, v in _state.items() if now - v.get("reported_at", now) > EXPIRE_SECS]:
            _state.pop(sid, None)
        items = [compute(v, now) for v in _state.values()]
    if _store:
        enrich_from_store(items, now)
        try:                                  # tag user-archived sessions so the client can hide them
            hid = _store.hidden_sessions()
            for it in items:
                it["hidden"] = it.get("session_id") in hid
        except Exception:                     # noqa: BLE001 — a DB hiccup must never break /state
            pass
    # hero first: needs-me, then non-stale, then fullest context
    items.sort(key=lambda x: (not x["needs_me"], x["stale"], -x["pct"]))
    return items


def enrich_from_store(items, now):
    """Add a time-to-compact ETA + growth rate per session, and fall back to the store's cost
    estimate when the authoritative statusline cost is absent (remote/watcher-only sessions)."""
    for it in items:
        sid = it.get("session_id")
        if not sid:
            continue
        try:
            eta = _store.compact_eta(sid, it["context_tokens"], it["compact_at"])
            if eta:
                it["eta_secs"] = eta["eta_secs"]
                it["growth_tpm"] = eta["tokens_per_min"]
            if it.get("cost_usd") is None:
                t = _store.session_total(sid)
                if t["messages"]:
                    it["cost_usd"] = round(t["cost_usd"], 4)
                    it["cost_est"] = True   # store estimate, not the authoritative statusline number
            it["prefix_tokens"] = _store.session_prefix_overhead(sid)   # MCP tax Slice 3: context-floor (real billed)
        except Exception:  # noqa: BLE001 — a DB hiccup must never break /state
            pass


def read_activity(path, n=30):
    """On-demand: tail one local transcript into a readable activity stream. Single source of truth
    is watcher.activity_from_lines (the same renderer remote watchers use to ship their tail)."""
    import watcher
    return watcher.read_activity(path, n)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _sec_headers(self):
        # Defensive headers on every response: block framing (clickjacking), MIME-sniffing, and referrer
        # leakage. CSP is frame-ancestors only — we deliberately don't restrict script/style-src because the
        # UI is a single inline <script>/<style> bundle (a 'unsafe-inline' CSP would add no real protection).
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._sec_headers()
        self.end_headers()
        self.wfile.write(body)

    def _activity(self):
        qs = parse_qs(urlparse(self.path).query)
        sid = (qs.get("id") or [""])[0]
        n = int((qs.get("n") or ["30"])[0]) if (qs.get("n") or [""])[0].isdigit() else 30
        with _lock:
            snap = _state.get(sid)
        if not snap:
            self._send_json({"items": [], "note": "session not found"})
            return
        tp = snap.get("transcript_path")
        if tp and os.path.isfile(tp):
            # transcript is local to the collector → read it fresh (most current)
            self._send_json({"items": read_activity(tp, n)})
            return
        # remote session: serve the activity tail the watcher shipped with its last report
        pushed = snap.get("activity")
        if pushed:
            self._send_json({"items": pushed[-n:], "remote": True})
            return
        self._send_json({"items": [], "note": "waiting for activity from " + (snap.get("host") or "remote host")})

    def _history(self):
        """Per-session context-token trajectory for the drill-in sparkline (from the usage store)."""
        qs = parse_qs(urlparse(self.path).query)
        sid = (qs.get("id") or [""])[0]
        if not _store:
            self._send_json({"points": [], "note": "store disabled"})
            return
        if not sid:
            self._send_json({"points": []}, 400)
            return
        try:
            pts = _store.session_context_series(sid)
        except Exception:  # noqa: BLE001 — a DB hiccup must never break the drill-in
            pts = []
        self._send_json({"points": pts})

    def _events(self):
        """Per-session behavioral events + counts for the drill-in (Phase 6, from the events store)."""
        qs = parse_qs(urlparse(self.path).query)
        sid = (qs.get("id") or [""])[0]
        if not _store:
            self._send_json({"counts": {}, "items": [], "note": "store disabled"})
            return
        if not sid:
            self._send_json({"counts": {}, "items": []}, 400)
            return
        try:
            self._send_json({"counts": _store.event_counts(sid), "items": _store.events_for_session(sid, 50)})
        except Exception:  # noqa: BLE001
            self._send_json({"counts": {}, "items": []})

    def _craft(self):
        """Craft Score (Phase 7): a rate-normalized score rewarding skilled, responsible Claude Code use,
        NOT raw spend. Aggregates from the usage + events store, scored by craft.py. Fleet-wide by default,
        or one ?account=. Honor-system, local-only, zero egress."""
        if not _store:
            self._send_json({"enabled": False, "note": "store disabled"})
            return
        qs = parse_qs(urlparse(self.path).query)
        acct = (qs.get("account") or [""])[0] or None
        try:
            import craft
            import medals
            agg, ze, te = self._craft_aggregates(acct)
            out = craft.score(agg)
            out.update(medals.evaluate(agg))                  # medals + level (Phase 8), same aggregates
            # Track 2 — self-comparison: your recent form vs your own baseline (windowed scores, no new storage)
            agg7, _, _ = self._craft_aggregates(acct, days=7)
            agg30, _, _ = self._craft_aggregates(acct, days=30)
            out["compare"] = {"w7": craft.score(agg7), "w30": craft.score(agg30)}
            # Track 2b — daily score series + personal best (computed on demand from retained raw data)
            ser = _store.daily_craft_aggregates(acct, days=60)
            series = sorted(({"day": d, "score": craft.score(a)["score"]} for d, a in ser.items()),
                            key=lambda p: p["day"])
            series = [p for p in series if p["score"] is not None]
            out["series"] = series
            if series:
                best = max(series, key=lambda p: p["score"])
                out["best"] = best
                out["new_high"] = len(series) > 1 and series[-1]["day"] == best["day"] and series[-1]["score"] == best["score"]
            out.update({"enabled": True, "account": acct, "zone_time": ze,
                        "model_mix": te["model_mix"], "accounts": _store.craft_accounts()})
            self._send_json(out)
        except Exception as e:  # noqa: BLE001
            self._send_json({"enabled": True, "error": str(e), "score": None})

    def _craft_aggregates(self, acct, days=None):
        """The shared rate-normalized aggregate bundle behind the Craft Score AND the medals — one scan of
        usage + events. `days` bounds it to a trailing window (None = all-time) for the Track 2 self-comparison.
        Returns (agg, zone_time, token_economics)."""
        ev = _store.event_totals(days=days, account=acct)
        ze = _store.zone_time(acct, days)
        te = _store.token_economics(acct, days)
        agg = {
            "cache_ratio": te["cache_ratio"],
            "messages": te["messages"],
            "compaction_manual": ev.get("compaction_manual", 0),
            "compaction_auto": ev.get("compaction_auto", 0),
            "healthy_pct": ze["healthy_pct"],
            "tool_errors": ev.get("tool_error", 0),
            "skill_use": ev.get("skill_use", 0),
            "subagent_spawn": ev.get("subagent_spawn", 0),
        }
        return agg, ze, te

    def _drift(self):
        """Dumb-zone v2 (data-derived): tool-error rate vs accumulated context, overall + per model, with the
        'knee' where degradation measurably kicks in. Correlational, honestly labelled. Optional ?account=."""
        if not _store:
            self._send_json({"enabled": False, "note": "store disabled"})
            return
        qs = parse_qs(urlparse(self.path).query)
        acct = (qs.get("account") or [""])[0] or None
        try:
            d = _store.drift_curve(account=acct)
            d.update({"enabled": True, "account": acct})
            self._send_json(d)
        except Exception as e:  # noqa: BLE001
            self._send_json({"enabled": True, "error": str(e), "overall": None})

    def _ingest_authed(self):
        return (not INGEST_TOKEN) or (self.headers.get("X-CC-Token", "") == INGEST_TOKEN)

    def _control_authed(self):
        # Inject/coach control-plane (outbox + coach-worker handshake) moves reply TEXT and AI narratives —
        # so, unlike Local-First metrics /ingest (open-by-default), it REQUIRES a configured token and refuses
        # outright when CC_INGEST_TOKEN is unset (mirroring how /reply hard-refuses without a PIN). This closes
        # the fail-open gap where a LAN host could drain/redirect queued replies or forge a coach narrative.
        return bool(INGEST_TOKEN) and hmac.compare_digest(self.headers.get("X-CC-Token", ""), INGEST_TOKEN)

    def _read_json_body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n)) if n else {}
        except (ValueError, TypeError):
            return None

    def _ingest_usage(self):
        """Remote watcher POSTs token-usage events (counts only, NO conversation content) → store, with
        server-side cost + per-session account attribution. Token-gated like /ingest. This is what lets a
        watcher-only host's spend (e.g. a work account on another machine) show in the per-account cost."""
        if INGEST_TOKEN and self.headers.get("X-CC-Token", "") != INGEST_TOKEN:
            record_rejected(self.client_address[0])
            self._send_text("unauthorized", 401)
            return
        body = self._read_json_body()
        if body is None:
            self._send_text("bad json", 400)
            return
        host = body.get("host")
        n = _store.record_remote_usage(body.get("events") or [], host=host) if _store else 0
        record_host_ok(host, source="ingest")
        self._send_json({"recorded": n})

    def _mcp_ingest(self):
        """Host-side MCP probe POSTs its server loadout (MCP tax Slice 2). Token-gated like /ingest. Defense in
        depth: we keep ONLY name + status even though the probe already strips commands/URLs/env — no secrets
        are accepted into the store regardless of what a (possibly-modified) probe sends."""
        if INGEST_TOKEN and self.headers.get("X-CC-Token", "") != INGEST_TOKEN:
            record_rejected(self.client_address[0])
            self._send_text("unauthorized", 401)
            return
        body = self._read_json_body()
        if body is None:
            self._send_text("bad json", 400)
            return
        host = body.get("host")
        servers = [{"name": s.get("name"), "status": s.get("status")}
                   for s in (body.get("servers") or []) if isinstance(s, dict) and s.get("name")]
        n = _store.record_mcp_report(host, servers, body.get("ts")) if _store else 0
        record_host_ok(host, source="mcp")
        self._send_json({"recorded": n})

    def _mcp(self):
        """🧩 MCP scene data: each host's configured servers (from the probe) crossed with OVER-TIME usage from
        `mcp_call` events (last 30d, backfilled from full transcripts) → true dead-weight ('configured but not
        called in 30d', not just the live-session scope of Slice 2). Each server also carries a `live` flag if a
        current session is calling it. Auth-gated. `floor` (Slice 3) = real billed context-prefix overhead."""
        if not _store:
            self._send_json({"enabled": False})
            return
        window = 30
        reports = _store.mcp_reports()
        used = _store.mcp_servers_used(days=window)          # {host: {server: {calls, last_ts}}} — over time
        used_all = _store.mcp_servers_used(days=0)           # all-time → "never" vs "last used Nd ago" for dead weight
        with _lock:
            snaps = list(_state.values())
        live = {}                                            # host -> {servers called in a CURRENT session}
        for s in snaps:
            h = s.get("host")
            if not h:
                continue
            for srv, info in (s.get("mcp_observed") or {}).items():
                if (info or {}).get("calls", 0) > 0:
                    live.setdefault(h, set()).add(srv)
        now = time.time()
        hosts = []
        for h, rep in reports.items():
            u = used.get(h, {})
            ua = used_all.get(h, {})
            lv = live.get(h, set())
            servers, used_count = [], 0
            for sv in rep.get("servers", []):
                name = sv.get("name")
                info = u.get(name)
                calls = info["calls"] if info else 0
                if calls > 0:
                    used_count += 1
                ever = ua.get(name)
                last_ever = ever["last_ts"] if ever else None
                # tier drives the "turn-off candidate" framing: active (keep) → stale (used once, long ago) →
                # never (configured but never called in any recorded transcript — the strongest candidate).
                tier = "active" if calls > 0 else ("stale" if last_ever else "never")
                servers.append({"name": name, "status": sv.get("status"), "calls": calls,
                                "last_ts": info["last_ts"] if info else None,
                                "last_ts_ever": last_ever, "tier": tier,
                                "used": calls > 0, "live": name in lv})
            # active first (busiest on top); then turn-off candidates worst-first: "never" then oldest-stale.
            servers.sort(key=lambda x: (not x["used"], -x["calls"] if x["used"] else (x["last_ts_ever"] or 0),
                                        x["name"] or ""))
            hosts.append({"host": h, "ts": rep.get("ts"),
                          "age_secs": round(now - rep["ts"], 1) if rep.get("ts") else None,
                          "configured": len(servers), "used": used_count, "dead": len(servers) - used_count,
                          "window_days": window, "servers": servers})
        hosts.sort(key=lambda x: x["host"] or "")
        # Slice 3 — the context-floor "token tax": REAL billed cached prefix per turn (tools+system+memory+MCP
        # defs), typical across recent sessions. Honest: this is the WHOLE prefix, not MCP-only (isolating MCP
        # needs server schemas the probe refuses to read) — the UI says so. estimate:False (these are billed).
        self._send_json({"enabled": True, "hosts": hosts, "window_days": window,
                         "floor": _store.prefix_overhead_stats(30), "estimate": False})

    def _outbox_poll(self):
        """Responder long-poll: hand the host's oldest queued reply to its responder (token-gated)."""
        if not self._control_authed():
            self._send_text("unauthorized", 401)
            return
        qs = parse_qs(urlparse(self.path).query)
        host = (qs.get("host") or [""])[0]
        if not host:
            self._send_json({"error": "host required"}, 400)
            return
        rec = claim_reply(host, OUTBOX_WAIT)
        if rec is None:
            self._send_json({})  # nothing within the hold window; responder re-polls
        else:
            self._send_json({"reply_id": rec["reply_id"], "session_id": rec["session_id"], "text": rec["text"]})

    def _outbox_result(self):
        """Responder reports an inject outcome -> unblock the waiting /reply (token-gated)."""
        if not self._control_authed():
            self._send_text("unauthorized", 401)
            return
        body = self._read_json_body()
        if body is None or not body.get("reply_id"):
            self._send_text("bad request", 400)
            return
        complete_reply(body["reply_id"],
                       {"ok": bool(body.get("ok")), "detail": body.get("detail", ""), "target": body.get("target")})
        self._send_text("ok")

    def _reply(self):
        """Phone -> inject a reply into a session's tmux pane. PIN-gated; refused entirely without a PIN
        because this is RCE into the user's shell."""
        if not ACCESS_PIN:
            self._send_json({"ok": False, "reason": "disabled",
                             "detail": "answer-from-phone requires CC_ACCESS_PIN to be set"}, 403)
            return
        if not self._authed():
            self._send_json({"ok": False, "reason": "unauthorized"}, 401)
            return
        body = self._read_json_body()
        if body is None:
            self._send_json({"ok": False, "reason": "bad_json"}, 400)
            return
        sid = body.get("session_id")
        text = (body.get("text") or "").strip()
        if not sid or not text:
            self._send_json({"ok": False, "reason": "missing", "detail": "session_id and text required"}, 400)
            return
        if len(text) > REPLY_MAX_LEN:
            self._send_json({"ok": False, "reason": "too_long", "detail": f"max {REPLY_MAX_LEN} chars"}, 413)
            return
        with _lock:
            snap = _state.get(sid)
        if not snap:
            self._send_json({"ok": False, "reason": "unknown_session"}, 404)
            return
        if not snap.get("answerable"):
            self._send_json({"ok": False, "reason": "not_answerable",
                             "detail": "no tmux target recorded for this session on " + str(snap.get("host"))}, 409)
            return
        rec = enqueue_reply(snap.get("host"), sid, text)
        res = wait_reply_result(rec, REPLY_TIMEOUT)
        if res is None:
            self._send_json({"ok": False, "reason": "no_responder",
                             "detail": "no responder on " + str(snap.get("host")) + " picked it up"}, 504)
        elif res.get("ok"):
            self._send_json(res, 200)
        else:
            self._send_json({"ok": False, "reason": "inject_failed", **res}, 502)

    def _pref(self):
        """Set a per-session UI pref (hide/archive, nickname) — server-persisted so it follows the user
        across devices. Gated by the same auth as the UI (open on LAN when no PIN). Not RCE like /reply,
        so it doesn't hard-require a PIN. Body: {session_id, hidden?:bool, nickname?:str}."""
        if not self._authed():
            self._send_json({"ok": False, "reason": "unauthorized"}, 401)
            return
        if not _store:
            self._send_json({"ok": False, "reason": "no_store",
                             "detail": "usage store is disabled; prefs can't persist"}, 503)
            return
        body = self._read_json_body()
        if body is None:
            self._send_json({"ok": False, "reason": "bad_json"}, 400)
            return
        # V11 ROI: per-ACCOUNT monthly plan price (no session_id). Body: {account, plan_price} (null/0 clears).
        if "plan_price" in body:
            acct = body.get("account")
            if not acct:
                self._send_json({"ok": False, "reason": "missing", "detail": "account required"}, 400)
                return
            ok = _store.set_plan_price(acct, body.get("plan_price"))
            self._send_json({"ok": bool(ok), "account": acct, "plan_prices": _store.get_plan_prices()})
            return
        sid = body.get("session_id")
        if not sid:
            self._send_json({"ok": False, "reason": "missing", "detail": "session_id required"}, 400)
            return
        hidden = body.get("hidden")
        nickname = body.get("nickname")
        if hidden is None and nickname is None:
            self._send_json({"ok": False, "reason": "noop", "detail": "nothing to set"}, 400)
            return
        ok = _store.set_session_pref(sid, hidden=hidden, nickname=nickname)
        self._send_json({"ok": bool(ok), "session_id": sid,
                         "hidden": sid in _store.hidden_sessions()})

    def _cost(self):
        """Cost history + current 5h-block burn (Sprint 3). Empty-but-valid if the store is off."""
        if not _store:
            self._send_json({"enabled": False, "burn": None, "daily": [], "projects": [], "total_usd": 0})
            return
        try:
            self._send_json({
                "enabled": True,
                "burn": _store.block_burn(),
                "daily": _store.daily_totals(14),
                "projects": _store.project_totals()[:8],
                "total_usd": round(_store.total_cost(), 2),
                "by_account": _store.cost_by_account("today"),     # per-account spend, today (local day)
                # burn-based ETA to Extra Usage per account+window (UI picks the binding window). null = not
                # climbing / too little history / window resets before 100%.
                "rate_eta": {a: {"five_hour": _store.extra_usage_eta(a, "five_hour"),
                                 "seven_day": _store.extra_usage_eta(a, "seven_day")}
                             for a in _store.accounts_with_rate_history()},
                # approximate Extra-Usage overage $ today — cost accrued while over the cap, for
                # extra-usage-ENABLED accounts only (a capped account without it is blocked, not billed).
                "overage": _store.overage_by_account(_extra_usage_accounts(), "today"),
                "events": _store.event_totals(),                   # fleet-wide behavioral-event rollup (Phase 6b)
            })
        except Exception as e:  # noqa: BLE001
            self._send_json({"enabled": True, "error": str(e), "burn": None, "daily": [], "projects": [], "total_usd": 0})

    def _report(self):
        """Trophy Room reporting (V10 Slice 1): lifetime vanity totals + active-day streaks + a per-bucket
        cost split and a 'saved by caching' estimate. Heavy rollups, polled ~60s off the live path. The cost
        split + savings are ESTIMATES (costing.py rates, model-aware) — total_usd stays the authoritative sum."""
        if not _store:
            self._send_json({"enabled": False})
            return
        try:
            import costing
            bd = _store.cost_breakdown()
            cost = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_creation": 0.0}
            savings = 0.0
            for b in bd["by_model"]:
                r = costing.rate_for(b["model"])
                if not r:
                    continue
                cost["input"] += b["input"] * r["input"]
                cost["output"] += b["output"] * r["output"]
                cost["cache_read"] += b["cache_read"] * r["cache_read"]
                cost["cache_creation"] += b["cache_creation"] * r["cache_create"]
                # what those cached reads WOULD have cost at the full input rate, minus what they did cost
                savings += b["cache_read"] * (r["input"] - r["cache_read"])
            breakdown = {k: {"tokens": bd["totals"][k], "cost_usd": round(cost[k], 4)} for k in cost}
            # dumb-zone-time trend (Slice 5): healthy-zone % per day (already computed by daily_craft_aggregates;
            # plain field, no craft import). The "am I keeping context cleaner over time?" self-improvement hook.
            zt = _store.daily_craft_aggregates(days=60)
            zone_trend = sorted(({"day": d, "healthy_pct": a["healthy_pct"]} for d, a in zt.items()
                                 if a.get("healthy_pct") is not None), key=lambda p: p["day"])
            self._send_json({
                "enabled": True,
                "all_time": _store.all_time_totals(),
                "streak": _store.active_days_streak(),
                "cache_savings_usd": round(savings, 2),
                "breakdown": breakdown,
                "daily": _store.daily_totals(365),     # calendar-heatmap substrate (Slice 2)
                "hourly": _store.hourly_histogram(30),  # day-of-week × hour rhythm grid (Slice 4)
                "zone_trend": zone_trend,               # healthy-zone % per day (Slice 5)
                "model_mix_daily": _store.daily_totals_by_model(30),  # model-mix-over-time stack (Slice 3)
            })
        except Exception as e:  # noqa: BLE001
            self._send_json({"enabled": True, "error": str(e)})

    def _efficiency(self):
        """V11 efficiency coach: what-if model-downgrade savings (S1) + subscription ROI (S2) + cap headroom
        (S3) + reducible-spend estimate (S4) over a 30-day window. All arithmetic on stored tokens/rates (no
        quality claim) — opportunities, never judgments; every figure a labelled estimate. Auth-gated."""
        if not _store:
            self._send_json({"enabled": False})
            return
        try:
            # Slice 2 ROI: API-equivalent value (Σ cost_usd, last 30d) per account vs its monthly plan price.
            prices = _store.get_plan_prices()
            month = {r["account"]: r["cost_usd"] for r in _store.cost_by_account("month")}
            roi = []
            for a in sorted(set(month) | set(prices)):
                val = round(month.get(a, 0.0), 2)
                pp = prices.get(a)
                roi.append({"account": a, "value_usd": val, "plan_price": pp,
                            "multiple": round(val / pp, 1) if pp else None})
            roi.sort(key=lambda x: x["value_usd"], reverse=True)
            # Slice 3 cap headroom: per-account plan-cap usage from rate_history (wired hosts only).
            caps = []
            for a in _store.accounts_with_rate_history():
                cu = _store.cap_usage(a, 30)
                if cu:
                    caps.append({"account": a, "windows": cu})
            self._send_json({"enabled": True, "estimate": True,
                             "savings": _store.model_savings(days=30),
                             "savings_breakdown": _store.model_savings_breakdown(days=30),
                             "roi": roi, "caps": caps,
                             "reducible": _store.reducible_spend(days=30)})
        except Exception as e:  # noqa: BLE001
            self._send_json({"enabled": True, "error": str(e)})

    def _coach_bundle(self, acct):
        """The content-free coaching bundle (Craft score/dims + hygiene/efficiency signals + a compact summary
        of the most-recent session). Shared by GET /coach (UI) and GET /coach-job (the host worker). No text."""
        import craft
        agg, ze, te = self._craft_aggregates(acct)
        scored = craft.score(agg)
        bundle = {
            "score": scored.get("score"), "grade": scored.get("grade"), "dims": scored.get("dims"),
            "cache_ratio": agg.get("cache_ratio"), "healthy_pct": ze.get("healthy_pct"),
            "compaction_manual": agg.get("compaction_manual", 0),
            "compaction_auto": agg.get("compaction_auto", 0),
            "skill_use": agg.get("skill_use", 0), "subagent_spawn": agg.get("subagent_spawn", 0),
            "tool_errors": agg.get("tool_errors", 0), "messages": agg.get("messages", 0),
        }
        recent = _store.recent_sessions(1)
        if recent:
            ap = _store.session_autopsy(recent[0]["session_id"])
            if ap:
                zt = ap.get("zone_time") or {}
                bundle["autopsy"] = {
                    "messages": ap["messages"], "cost_usd": ap["cost_usd"], "duration_secs": ap["duration_secs"],
                    "cache_ratio": ap["cache_ratio"], "healthy_pct": zt.get("healthy_pct"),
                    "danger_turns": zt.get("danger"), "model_mix": ap["model_mix"], "events": ap["events"]}
        return bundle

    def _coach(self):
        """V8 Coach: the content-free coaching bundle + recent-session list (Autopsy picker) + a 'coach me with
        Claude Code' handoff (the /jaid-coach Skill), PLUS the latest in-app narrative when the host coach-worker
        has produced one (Slice B). The container itself never calls a model — it has neither the claude CLI nor
        the user's login; the worker generates with `claude -p` (or Ollama) and POSTs back. Auth-gated."""
        if not _store:
            self._send_json({"enabled": False, "note": "store disabled"})
            return
        qs = parse_qs(urlparse(self.path).query)
        acct = (qs.get("account") or [""])[0] or None
        try:
            bundle = self._coach_bundle(acct)
            cs = coach_state()
            self._send_json({
                "enabled": True, "engine": "handoff", "estimate": True,
                "account": acct, "accounts": _store.craft_accounts(), "bundle": bundle,
                "recent_sessions": _store.recent_sessions(12),
                "handoff": {"cmd": "/jaid-coach",
                            "note": "Run this in your own Claude Code — it reads your local metrics and coaches "
                                    "you, no server or egress."},
                "pending": cs["pending"], "narrative": cs["narrative"],
                "narrative_engine": cs["engine"], "generated_at": cs["generated_at"],
            })
        except Exception as e:  # noqa: BLE001
            self._send_json({"enabled": True, "error": str(e), "narrative": None})

    def _coach_request(self):
        """V8 Slice B: the phone asks for a fresh in-app narrative (PIN-gated). Sets the single pending job the
        host coach-worker will pick up. Costs the user's own tokens (claude engine) — so it's explicit, never auto."""
        if not self._authed():
            self._send_text("unauthorized", 401)
            return
        body = self._read_json_body() or {}
        coach_request(body.get("account") or None)
        self._send_json({"ok": True, "pending": True})

    def _coach_job(self):
        """V8 Slice B: the host coach-worker claims the pending narrative job (token-gated) and gets the
        content-free bundle to feed its engine. {} when nothing is pending (or already claimed)."""
        if not self._control_authed():
            self._send_text("unauthorized", 401)
            return
        claim = coach_claim()
        if not claim:
            self._send_json({"pending": False})
            return
        bundle = None
        if _store:
            try:
                bundle = self._coach_bundle(claim.get("account"))
            except Exception:  # noqa: BLE001
                bundle = None
        self._send_json({"pending": True, "account": claim.get("account"), "bundle": bundle})

    def _coach_result(self):
        """V8 Slice B: the worker submits the generated narrative (token-gated, re-validated). Cached in memory
        + persisted to store meta so the scene shows it immediately + survives a restart."""
        if not self._control_authed():
            self._send_text("unauthorized", 401)
            return
        body = self._read_json_body()
        if body is None or not body.get("narrative"):
            self._send_text("bad request", 400)
            return
        coach_complete(str(body["narrative"]), body.get("engine") or "?",
                       body.get("generated_at") or time.strftime("%Y-%m-%d %H:%M:%S"), persist=_store)
        self._send_text("ok")

    def _autopsy(self):
        """V8 Session Autopsy: a content-free per-session report card (zone timeline, model mix, cache ratio,
        compaction/skill/subagent/error counts, cost, duration) from store.session_autopsy. Real-billed, not an
        estimate; no conversation text. Auth-gated. 400 on missing/unknown ?sid=."""
        if not _store:
            self._send_json({"enabled": False, "note": "store disabled"})
            return
        sid = (parse_qs(urlparse(self.path).query).get("sid") or [""])[0]
        if not sid:
            self._send_json({"error": "sid required"}, 400)
            return
        a = _store.session_autopsy(sid)
        if a is None:
            self._send_json({"error": "no such session"}, 404)
            return
        self._send_json(a)

    # ---- V9 local transcript search (content firewall) ----
    def _content_local(self):
        """True ONLY for connections that arrived on the loopback-published content port. Under Docker
        bridge the source IP is the gateway for both host and phone traffic (fails open), so the bound
        port — reachable only via the host's 127.0.0.1 publish — is the honest local-only boundary."""
        try:
            return self.server.server_address[1] == CONTENT_PORT
        except Exception:  # noqa: BLE001
            return False

    def _search_meta(self):
        """Capability probe — served on BOTH ports so the SPA can render the local-only wall on the LAN
        port and the real search UI on the loopback content port. No content leaves here."""
        st = (_content.stats() if _content else
              {"enabled": False, "docs": 0, "sessions": 0, "files": 0, "last_indexed_ts": None})
        embed = _content.embed_stats() if _content else {"available": False, "model": None,
                                                         "embedded": 0, "embeddable": 0, "error": None}
        self._send_json({"local": self._content_local(), "content_port": CONTENT_PORT,
                         "enabled": bool(st.get("enabled")), "stats": st, "embed": embed,
                         "config": _content.config() if _content else {}})

    def _search(self):
        """Full-text search over raw transcript content. LOCAL-ONLY: the LAN port walls it; only the
        loopback content port returns results, so raw content never crosses the network."""
        if not self._content_local():
            self._send_json({"local": False, "results": [],
                             "detail": f"search is local-only — open http://localhost:{CONTENT_PORT} on this machine"}, 403)
            return
        if not _content:
            self._send_json({"local": True, "enabled": False, "results": []})
            return
        qs = parse_qs(urlparse(self.path).query)
        q = (qs.get("q") or [""])[0]
        kinds = [k for k in (qs.get("kinds") or [""])[0].split(",") if k] or None   # validated in search()
        since = (qs.get("since") or [""])[0]
        since_ts = float(since) if since.replace(".", "", 1).isdigit() else None
        mode = (qs.get("mode") or ["keyword"])[0]
        if mode == "semantic":
            results = _content.semantic_search(q, kinds=kinds, since_ts=since_ts)
        else:
            results = _content.search(q, kinds=kinds, since_ts=since_ts)
        self._send_json({"local": True, "enabled": _content.enabled(), "query": q,
                         "mode": mode, "results": results})

    def _search_context(self):
        """Open the conversation around a search hit (raw content). LOCAL-ONLY like /search. Window by an
        anchor uuid, or page with before_pos/after_pos rowid cursors."""
        if not self._content_local():
            self._send_json({"local": False, "blocks": []}, 403)
            return
        if not _content:
            self._send_json({"local": True, "blocks": []})
            return
        qs = parse_qs(urlparse(self.path).query)
        sid = (qs.get("sid") or [""])[0]
        if not sid:
            self._send_json({"error": "sid required", "blocks": []}, 400)
            return
        anchor = (qs.get("anchor") or [None])[0]
        bp = (qs.get("before_pos") or [None])[0]
        ap = (qs.get("after_pos") or [None])[0]
        n = (qs.get("n") or ["20"])[0]
        self._send_json(_content.context(
            sid, anchor=anchor,
            before_pos=int(bp) if bp and bp.lstrip("-").isdigit() else None,
            after_pos=int(ap) if ap and ap.lstrip("-").isdigit() else None,
            n=int(n) if str(n).isdigit() else 20))

    def _search_enable(self):
        """Turn on content indexing — the first time this app writes raw content to disk. Mirrors /reply's
        hard PIN gate (a deliberate, sensitive action) and is local-only. Kicks an immediate backfill."""
        if not ACCESS_PIN:
            self._send_json({"ok": False, "reason": "disabled",
                             "detail": "content search requires CC_ACCESS_PIN to be set"}, 403)
            return
        if not self._authed():
            self._send_json({"ok": False, "reason": "unauthorized"}, 401)
            return
        if not self._content_local():
            self._send_json({"ok": False, "reason": "not_local",
                             "detail": f"enable from http://localhost:{CONTENT_PORT} on this machine"}, 403)
            return
        if not _content:
            self._send_json({"ok": False, "reason": "unavailable"}, 503)
            return
        _content.enable()
        threading.Thread(target=_content_index_once, daemon=True).start()
        self._send_json({"ok": True, "enabled": True, "stats": _content.stats()})

    def _search_delete(self):
        """Wipe the entire content index and turn search back off (the 'Delete index' button). Local-only."""
        if not self._authed():
            self._send_json({"ok": False, "reason": "unauthorized"}, 401)
            return
        if not self._content_local():
            self._send_json({"ok": False, "reason": "not_local"}, 403)
            return
        if not _content:
            self._send_json({"ok": False, "reason": "unavailable"}, 503)
            return
        _content.wipe()
        self._send_json({"ok": True, "enabled": False, "stats": _content.stats()})

    def _content_config(self):
        """Set content-index privacy config (V13 2B): retention_days / redact / scope. Local-only + PIN-gated
        (extending retention or disabling redaction is a deliberate, sensitive change). Body keys all optional:
        {retention_days:int, redact:bool, scope:'all'|'conversation'}."""
        if not ACCESS_PIN:
            self._send_json({"ok": False, "reason": "disabled",
                             "detail": "content config requires CC_ACCESS_PIN to be set"}, 403)
            return
        if not self._authed():
            self._send_json({"ok": False, "reason": "unauthorized"}, 401)
            return
        if not self._content_local():
            self._send_json({"ok": False, "reason": "not_local"}, 403)
            return
        if not _content:
            self._send_json({"ok": False, "reason": "unavailable"}, 503)
            return
        body = self._read_json_body()
        if body is None:
            self._send_json({"ok": False, "reason": "bad_json"}, 400)
            return
        cfg = _content.set_config(retention_days=body.get("retention_days"),
                                  redact=body.get("redact"), scope=body.get("scope"))
        self._send_json({"ok": True, "config": cfg})

    # ---- V13 Slice 3: maintenance + export/import (content-free → allowed on the LAN port, still authed) ----
    def _maintenance(self):
        """DB sizes + row counts + projections + last-export info. Read-only, content-free."""
        if not self._authed():
            self._send_text("unauthorized", 401)
            return
        import maintenance
        import portable
        last = portable.latest_export(EXPORT_DIR)
        self._send_json({
            "stats": maintenance.db_stats(_store, _content),
            "export_dir": EXPORT_DIR,
            "runtime": RUNTIME,
            "last_export": ({"path": last, "bytes": os.path.getsize(last),
                             "ts": os.path.getmtime(last)} if last and os.path.exists(last) else None),
        })

    def _export(self):
        """Write a portable, content-free bundle to EXPORT_DIR. Encrypted by default (passphrase in body);
        a plaintext export needs an explicit {plaintext:true}. Read-only on the DB."""
        if not self._authed():
            self._send_json({"ok": False, "reason": "unauthorized"}, 401)
            return
        if not _store:
            self._send_json({"ok": False, "reason": "no_store"}, 503)
            return
        body = self._read_json_body() or {}
        import portable
        encrypt = not body.get("plaintext")
        passphrase = body.get("passphrase") or ""
        if encrypt and not passphrase:
            self._send_json({"ok": False, "reason": "passphrase_required",
                             "detail": "an encrypted export needs a passphrase (store it in 1Password)"}, 400)
            return
        if encrypt and not crypto.available():
            self._send_json({"ok": False, "reason": "no_crypto",
                             "detail": "cryptography not installed"}, 503)
            return
        try:
            res = portable.write_export(_store, EXPORT_DIR, passphrase=passphrase, encrypt=encrypt)
        except Exception as e:  # noqa: BLE001
            self._send_json({"ok": False, "reason": "export_failed", "detail": str(e)[:200]}, 500)
            return
        self._send_json({"ok": True, **res})

    def _export_download(self):
        """Stream the freshest export file as an attachment (phone download)."""
        if not self._authed():
            self._send_text("unauthorized", 401)
            return
        import portable
        path = portable.latest_export(EXPORT_DIR)
        if not path or not os.path.exists(path):
            self._send_text("no export found", 404)
            return
        with open(path, "rb") as f:
            blob = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(path)}"')
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _import(self):
        """Merge a portable bundle into the store (INSERT OR IGNORE → idempotent). PIN-gated (it writes data).
        Body: {data:{...}} or {path:"...", passphrase?}, plus {dry:bool}."""
        if not ACCESS_PIN:
            self._send_json({"ok": False, "reason": "disabled",
                             "detail": "import requires CC_ACCESS_PIN to be set"}, 403)
            return
        if not self._authed():
            self._send_json({"ok": False, "reason": "unauthorized"}, 401)
            return
        if not _store:
            self._send_json({"ok": False, "reason": "no_store"}, 503)
            return
        body = self._read_json_body()
        if body is None:
            self._send_json({"ok": False, "reason": "bad_json"}, 400)
            return
        import portable
        data = body.get("data")
        if data is None and body.get("path"):
            # Confine the path to EXPORT_DIR (realpath prefix) — never let /import open an arbitrary file,
            # which would be a file-read / decrypt-oracle over the whole container FS (e.g. /proc/self/environ).
            base = os.path.realpath(EXPORT_DIR)
            rp = os.path.realpath(body["path"])
            if not (rp == base or rp.startswith(base + os.sep)) or not os.path.isfile(rp):
                self._send_json({"ok": False, "reason": "bad_path",
                                 "detail": f"path must be a file under {EXPORT_DIR}"}, 400)
                return
            try:
                with open(rp, "rb") as f:
                    data = portable.load_export(f.read(), passphrase=body.get("passphrase"))
            except Exception:  # noqa: BLE001 — generic reason only (no str(e) echo → no content/type oracle)
                self._send_json({"ok": False, "reason": "load_failed"}, 400)
                return
        if not isinstance(data, dict):
            self._send_json({"ok": False, "reason": "no_data"}, 400)
            return
        try:
            res = portable.import_bundle(_store, data, dry=bool(body.get("dry")))
        except ValueError as e:
            self._send_json({"ok": False, "reason": "bad_bundle", "detail": str(e)[:200]}, 400)
            return
        self._send_json({"ok": True, "dry": bool(body.get("dry")), "tables": res})

    def _maint_action(self):
        """Run a maintenance action (checkpoint / prune / vacuum). Prune & vacuum are destructive → PIN-gated
        + preview-first (dry). Checkpoint is lossless. Body shape depends on the path."""
        path = self.path.split("?")[0]
        destructive = path in ("/maintenance-prune", "/maintenance-vacuum")
        if destructive and not ACCESS_PIN:
            self._send_json({"ok": False, "reason": "disabled",
                             "detail": "destructive maintenance requires CC_ACCESS_PIN"}, 403)
            return
        if not self._authed():
            self._send_json({"ok": False, "reason": "unauthorized"}, 401)
            return
        import maintenance
        body = self._read_json_body() or {}
        if path == "/maintenance-checkpoint":
            self._send_json({"ok": True, "result": maintenance.checkpoint(_store, _content)})
        elif path == "/maintenance-prune":
            policy = body.get("policy") or {}
            dry = bool(body.get("dry", True))
            live = None
            if policy.get("content_orphans"):
                try:
                    import watcher
                    live = watcher.all_transcript_files()
                except Exception:  # noqa: BLE001
                    live = []
            res = maintenance.prune(_store, _content, policy, live_paths=live, dry=dry)
            self._send_json({"ok": True, "dry": dry, "result": res})
        elif path == "/maintenance-vacuum":
            self._send_json({"ok": True, "result": maintenance.vacuum(_store, _content, body.get("which", "both"))})
        else:
            self._send_json({"ok": False, "reason": "unknown"}, 404)

    def _health(self):
        """Liveness + version + runtime. Content-free and UNAUTHENTICATED (a probe endpoint). Drives the UI
        footer + Slice 6's update check (which picks the upgrade verb from the runtime)."""
        self._send_json({"ok": True, "version": version.VERSION, "runtime": RUNTIME,
                         "content_port": CONTENT_PORT})

    def _authed(self):
        if not ACCESS_PIN:
            return True
        ck = SimpleCookie(self.headers.get("Cookie", ""))
        v = ck["cc_auth"].value if "cc_auth" in ck else ""
        return bool(v) and hmac.compare_digest(v, AUTH_COOKIE)

    def _login_page(self, error=False):
        msg = '<p class="err">Wrong PIN — try again.</p>' if error else ''
        html = (
            '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>body{background:#0b0e14;color:#e6edf3;font-family:-apple-system,sans-serif;'
            'display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}'
            'form{background:#151a23;border:1px solid #222b3a;border-radius:16px;padding:28px;width:280px;text-align:center}'
            'h1{font-size:16px;margin:0 0 16px}input{width:100%;padding:12px;font-size:18px;text-align:center;'
            'border-radius:10px;border:1px solid #222b3a;background:#1b2230;color:#e6edf3;box-sizing:border-box}'
            'button{margin-top:12px;width:100%;padding:12px;font-size:15px;font-weight:700;border:0;border-radius:10px;'
            'background:#3fb950;color:#0b0e14}.err{color:#f85149;font-size:13px}</style>'
            '<form method="POST" action="/login"><h1>Claude Code · Triage</h1>' + msg +
            '<input name="pin" type="password" inputmode="numeric" autofocus placeholder="PIN" autocomplete="current-password">'
            '<button>Unlock</button></form>'
        )
        body = html.encode()
        self.send_response(200 if not error else 401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._sec_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/login":
            self._login_page()
            return
        if path == "/health":
            self._health()
            return
        if path == "/update":
            self._send_json(dict(_update))
            return
        if path == "/outbox":
            self._outbox_poll()
            return
        if path == "/coach-job":          # host coach-worker claims a pending narrative job (token-gated)
            self._coach_job()
            return
        if path in ("/", "/index.html", "/state", "/events", "/activity", "/cost", "/hosts", "/history", "/session-events", "/craft", "/drift", "/report", "/efficiency", "/mcp", "/coach", "/autopsy", "/search", "/search-meta", "/search-context", "/maintenance", "/export") and not self._authed():
            if path in ("/", "/index.html"):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send_text("unauthorized", 401)
            return
        if path == "/state":
            self._send_json(snapshot_state())
        elif path == "/activity":
            self._activity()
        elif path == "/cost":
            self._cost()
        elif path == "/history":
            self._history()
        elif path == "/session-events":
            self._events()
        elif path == "/craft":
            self._craft()
        elif path == "/drift":
            self._drift()
        elif path == "/report":
            self._report()
        elif path == "/efficiency":
            self._efficiency()
        elif path == "/mcp":
            self._mcp()
        elif path == "/coach":
            self._coach()
        elif path == "/autopsy":
            self._autopsy()
        elif path == "/search-meta":
            self._search_meta()
        elif path == "/search":
            self._search()
        elif path == "/search-context":
            self._search_context()
        elif path == "/maintenance":
            self._maintenance()
        elif path == "/export":
            if parse_qs(urlparse(self.path).query).get("download"):
                self._export_download()      # GET /export?download=1 streams the freshest bundle
            else:
                self._send_json({"detail": "POST /export to create; GET /export?download=1 to fetch"}, 400)
        elif path == "/hosts":
            self._send_json(host_health())
        elif path == "/events":
            self._sse()
        elif path in ("/", "/index.html"):
            self._send_file("index.html", "text/html; charset=utf-8")
        elif path == "/manifest.webmanifest":
            self._send_file("manifest.webmanifest", "application/manifest+json")
        elif path == "/sw.js":
            self._send_file("sw.js", "application/javascript")
        elif path == "/icon.svg":
            self._send_file("icon.svg", "image/svg+xml")
        elif path == "/nosleep.mp4":
            self._send_file("nosleep.mp4", "video/mp4")
        elif path == "/nosleep.webm":
            self._send_file("nosleep.webm", "video/webm")
        elif path == "/thedelay-logo.gif":
            # vendored locally (NOT hotlinked) so opening the ❓ Help modal stays zero-egress
            self._send_file("thedelay-logo.gif", "image/gif")
        else:
            self._send_text("not found", 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/login":
            ip = self.client_address[0] if self.client_address else "?"
            blocked = _login_blocked_for(ip)
            if blocked:
                self._send_json({"ok": False, "reason": "locked_out",
                                 "detail": f"too many attempts; retry in {int(blocked)}s"}, 429)
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                pin = (parse_qs(self.rfile.read(n).decode("utf-8", "replace")).get("pin") or [""])[0]
            except (ValueError, TypeError):
                pin = ""
            if ACCESS_PIN and hmac.compare_digest(pin, ACCESS_PIN):
                _login_note_ok(ip)
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", f"cc_auth={AUTH_COOKIE}; Max-Age=2592000; Path=/; HttpOnly; SameSite=Lax")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                _login_note_fail(ip)
                time.sleep(0.5)              # slow scripted guessing even before the lockout trips
                self._login_page(error=True)
            return
        if path == "/reply":
            self._reply()
            return
        if path == "/pref":
            self._pref()
            return
        if path == "/outbox/result":
            self._outbox_result()
            return
        if path == "/coach-request":      # phone asks for a fresh in-app narrative (PIN-gated)
            self._coach_request()
            return
        if path == "/coach-result":       # host coach-worker submits the generated narrative (token-gated)
            self._coach_result()
            return
        if path == "/search-enable":      # V9: turn on content indexing (local-only + PIN-gated)
            self._search_enable()
            return
        if path == "/search-delete":      # V9: wipe the content index (local-only)
            self._search_delete()
            return
        if path == "/content-config":     # V13 2B: set retention/redaction/scope (local-only + PIN-gated)
            self._content_config()
            return
        if path == "/export":             # V13 3: write a portable bundle (encrypted by default)
            self._export()
            return
        if path == "/import":             # V13 3: merge a portable bundle (PIN-gated)
            self._import()
            return
        if path in ("/maintenance-checkpoint", "/maintenance-prune", "/maintenance-vacuum"):
            self._maint_action()
            return
        if path == "/ingest-usage":
            self._ingest_usage()
            return
        if path == "/mcp-ingest":
            self._mcp_ingest()
            return
        if path != "/ingest":
            self._send_text("not found", 404)
            return
        if INGEST_TOKEN and self.headers.get("X-CC-Token", "") != INGEST_TOKEN:
            record_rejected(self.client_address[0])   # surface a misconfigured watcher as RED
            self._send_text("unauthorized", 401)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            snap = json.loads(self.rfile.read(n))
        except (ValueError, TypeError):
            self._send_text("bad json", 400)
            return
        host = snap.get("host")
        # Heartbeat beacon: a watcher with zero active sessions still says "I'm alive and authenticating",
        # so an idle host is distinguishable from a dead/erroring one (the zero-sessions blind spot).
        if snap.get("beacon"):
            record_host_ok(host, source="beacon")
            self._send_text("ok")
            return
        sid = snap.get("session_id")
        if not sid:
            self._send_text("no session_id", 400)
            return
        snap["reported_at"] = time.time()
        with _lock:
            _state[sid] = snap
        # persist this remote session's account (point-in-time hook value, else host-current guess) so cost
        # ingested for it — local or remote — attributes to the right account even after the snapshot ages out
        if _store and snap.get("account"):
            _store.set_session_account(sid, snap["account"], host=host,
                                       source=("hook" if snap.get("account_approx") is False else "host"))
        record_host_ok(host, source="ingest")
        self._send_text("ok")

    # ---- helpers ----
    def _send_text(self, s, code=200):
        body = s.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self._sec_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, name, ctype):
        try:
            with open(os.path.join(WEB_DIR, name), "rb") as f:
                body = f.read()
        except OSError:
            self._send_text("not found", 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._sec_headers()
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_payload = None
        last_beat = 0.0
        try:
            while True:
                payload = json.dumps(snapshot_state())
                now = time.monotonic()
                if payload != last_payload or (now - last_beat) > SSE_HEARTBEAT:
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    last_payload = payload
                    last_beat = now
                time.sleep(SSE_INTERVAL)
        except (BrokenPipeError, ConnectionResetError):
            pass


def local_scan_loop():
    """Populate _state from this host's own transcripts (reuses watcher.py parsing)."""
    import watcher
    print(f"[collector] local scan ON  host={watcher.HOSTNAME} roots={watcher.PROJECTS_DIRS} "
          f"state={watcher.STATE_DIR}", flush=True)
    while True:
        start = time.monotonic()
        for snap in watcher.collect_snapshots():
            snap["reported_at"] = time.time()
            with _lock:
                _state[snap["session_id"]] = snap
        record_host_ok(watcher.HOSTNAME, source="local")   # the collector host stays GREEN even with 0 sessions
        time.sleep(max(0.0, POLL_INTERVAL - (time.monotonic() - start)))


def ingest_loop():
    """Persist this host's per-message token usage into the store for cost history + burn + ETA.
    Re-reads each active transcript's tail every DB_POLL; INSERT OR IGNORE makes that idempotent."""
    import watcher
    import os as _os
    print(f"[collector] usage ingest ON  db={_store.path}  every {DB_POLL}s", flush=True)
    first = True
    while True:
        start = time.monotonic()
        # 1) refresh the per-session account map from the hook's status files (point-in-time exact).
        for sid, st in watcher.read_state_files(time.time()).items():
            if st.get("account"):
                _store.set_session_account(sid, st["account"], host=watcher.HOSTNAME, source="hook")
        # 2) attribute usage rows from the map. First pass rewrites ALL rows (clears any prior blind
        #    backfill); later passes only fill newly-NULL rows for sessions we've since learned (cheap).
        n = _store.reattribute_accounts(only_null=not first)
        if first:
            print(f"[collector] re-attributed account on {n} rows from the per-session map", flush=True)
            # one-time event backfill: scan ALL transcripts in full (not just tails) so event history is
            # complete. Guarded by a meta flag (Phase 6a already tail-ingested some, so can't guard on empty).
            try:
                if not _store.get_meta("event_backfill_v1"):
                    bf = 0
                    for path in watcher.all_transcript_files():
                        try:
                            with open(path, encoding="utf-8", errors="replace") as fh:
                                bf += _store.ingest_event_lines(fh, host=watcher.HOSTNAME)
                        except OSError:
                            pass
                    _store.set_meta("event_backfill_v1", "done")
                    print(f"[collector] event backfill: {bf} events from full transcripts", flush=True)
                # v2: re-scan for mcp_call events (a new type added after v1 ran). Idempotent — stable event_id
                # + INSERT OR IGNORE means only the new mcp_call rows insert; existing events are skipped.
                if not _store.get_meta("event_backfill_v2_mcp"):
                    bf2 = 0
                    for path in watcher.all_transcript_files():
                        try:
                            with open(path, encoding="utf-8", errors="replace") as fh:
                                bf2 += _store.ingest_event_lines(fh, host=watcher.HOSTNAME)
                        except OSError:
                            pass
                    _store.set_meta("event_backfill_v2_mcp", "done")
                    print(f"[collector] mcp_call backfill: {bf2} new events (idempotent re-scan)", flush=True)
            except Exception:  # noqa: BLE001
                pass
            first = False
        # 3) ingest each active transcript, tagged with ITS OWN session's account (per-session, not host).
        #    Same tail feeds BOTH the usage rows and the behavioral-event extractor (Phase 6).
        inserted = ev_inserted = 0
        for path in watcher.find_active_files():
            sid = _os.path.basename(path)[:-6] if path.endswith(".jsonl") else None
            acct = _store.account_for_session(sid) if sid else None
            try:
                lines = list(watcher.tail_lines(path, watcher.TAIL_BYTES))
                inserted += _store.ingest_lines(lines, host=watcher.HOSTNAME, account=acct)
                ev_inserted += _store.ingest_event_lines(lines, host=watcher.HOSTNAME)
            except Exception:  # noqa: BLE001
                pass
        if inserted:
            print(f"[collector] ingested {inserted} new usage rows", flush=True)
        if ev_inserted:
            print(f"[collector] ingested {ev_inserted} new events", flush=True)
        # 4) record per-account rate-limit points (binding read per window) for the Extra-Usage burn ETA.
        seen = {}   # (account, window) -> [max used_pct, resets_at]
        with _lock:
            snaps = list(_state.values())
        for s in snaps:
            rl, acct = s.get("rate_limits"), s.get("account")
            if not rl or not acct:
                continue
            for win in ("five_hour", "seven_day"):
                w = rl.get(win)
                if w and w.get("used_percentage") is not None:
                    cur = seen.get((acct, win))
                    if cur is None or w["used_percentage"] > cur[0]:
                        seen[(acct, win)] = [w["used_percentage"], w.get("resets_at")]
        for (acct, win), (pct, reset) in seen.items():
            _store.record_rate_point(acct, win, pct, reset)
        time.sleep(max(0.0, DB_POLL - (time.monotonic() - start)))


def _content_index_once():
    """One incremental index pass over THIS host's own transcripts. No-op unless search is enabled."""
    if not _content:
        return
    try:
        import watcher
        _content.index_pass(watcher.all_transcript_files(), host=os.environ.get("CC_HOST"))
    except Exception as e:  # noqa: BLE001
        print(f"[content] index pass failed: {e}", flush=True)


def content_index_loop():
    """Background indexer for V9 search. Cheap flag-check when disabled (the default); only reads/writes
    content once the user opts in via /search-enable."""
    while True:
        try:
            if _content and _content.enabled():
                _content_index_once()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(CONTENT_POLL)


def embed_loop():
    """Background semantic-embedding drain (V9 Slice 5). When semantic is configured + available, embed
    un-embedded conversation blocks in batches back-to-back until caught up, then idle. No-op (cheap check)
    when no embed model is set or search is off — and graceful if Ollama is unreachable (errors recorded)."""
    while True:
        worked = False
        try:
            if _content and _content.semantic_available():
                r = _content.embed_pass(batch=64)
                worked = (r.get("embedded", 0) + r.get("skipped", 0)) > 0   # skips count as progress (drain moves on)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5 if worked else 30)   # drain fast, then idle (re-checks for newly-indexed blocks)


def update_check_loop():
    """Poll the version manifest (opt-in via CC_UPDATE_URL) and cache whether a newer version exists. Outbound
    GET only — no user data sent. Never auto-applies; the UI just surfaces the per-runtime upgrade command."""
    import urllib.request
    while True:
        try:
            req = urllib.request.Request(UPDATE_URL, headers={"User-Agent": "cc-observability"})
            with urllib.request.urlopen(req, timeout=8) as r:
                manifest = json.loads(r.read().decode("utf-8", "replace"))
            latest = str(manifest.get("version") or manifest.get("latest") or "").strip()
            if latest:
                _update["latest"] = latest
                _update["update_available"] = version.is_newer(latest, version.VERSION)
            _update["checked_ts"] = time.time()
        except Exception:  # noqa: BLE001 — network/parse errors are non-fatal; just retry next cycle
            pass
        time.sleep(UPDATE_INTERVAL)


def maintenance_loop():
    """Automatic WAL checkpoint (V13 Slice 2) — the ONLY automatic maintenance op, and it's lossless (it just
    truncates the -wal file, which SQLite's passive auto-checkpoint never shrinks). Prune/vacuum are always
    user-triggered with a preview. Runs both DBs every MAINT_INTERVAL."""
    while True:
        time.sleep(MAINT_INTERVAL)
        try:
            import maintenance
            maintenance.checkpoint(_store, _content)
            if _content:                       # V13 2B: auto-purge content older than the retention window
                _content.apply_retention()     # (default 14d; 0 = off). Opt-in + rebuildable → user-chosen default.
        except Exception:  # noqa: BLE001
            pass


def main():
    global _store, _content
    # POSIX blanket: everything this process creates (DBs, SQLite -wal/-shm, exports) is owner-only by
    # default — no per-file chmod race, and covers files we don't explicitly touch. (Windows ignores umask;
    # there the per-user-profile ACL governs access — see fsperms.py.)
    if os.name != "nt":
        os.umask(0o077)
    # Nudge if the secrets file is group/other-readable (it holds CC_ACCESS_PIN / CC_INGEST_TOKEN). POSIX-only.
    fsperms.warn_if_exposed(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
                            ".env (CC_ACCESS_PIN / CC_INGEST_TOKEN)", log=lambda m: print(m, flush=True))
    # Misconfig guard: equal ports would collapse the content firewall onto the LAN listener (the
    # local-only check is `bound_port == CONTENT_PORT`, which would then be true for 0.0.0.0:PORT too).
    if PORT == CONTENT_PORT:
        raise SystemExit(f"[fatal] CC_PORT ({PORT}) must differ from CC_CONTENT_PORT ({CONTENT_PORT}) — "
                         "equal ports would expose raw transcript search on the LAN. Aborting.")
    if ACCESS_PIN and len(ACCESS_PIN) < 8:
        print(f"[security] CC_ACCESS_PIN is short ({len(ACCESS_PIN)} chars) — it gates /reply (shell inject) "
              "on a LAN port. Use a long, non-numeric PIN/passphrase.", flush=True)
    try:
        import store as _store_mod
        import portable
        _store = _store_mod.Store(
            pre_migrate_hook=lambda s: portable.pre_upgrade_backup(s, EXPORT_DIR))   # Slice 3 safety net
    except Exception as e:  # noqa: BLE001 — dashboard still works without cost history
        print(f"[collector] usage store disabled: {e}", flush=True)
        _store = None
    coach_load(_store)        # restore the last coach narrative (V8 Slice B) so the 🧠 scene isn't blank after restart
    try:
        import content_index
        _content = content_index.ContentIndex(CONTENT_DB_PATH or content_index.DEFAULT_PATH,
                                              host=os.environ.get("CC_HOST"))
    except Exception as e:  # noqa: BLE001 — dashboard still works without search
        print(f"[content] search index disabled: {e}", flush=True)
        _content = None
    if LOCAL_SCAN:
        threading.Thread(target=local_scan_loop, daemon=True).start()
        if _store:
            threading.Thread(target=ingest_loop, daemon=True).start()
        if _content:
            threading.Thread(target=content_index_loop, daemon=True).start()
            threading.Thread(target=embed_loop, daemon=True).start()
    if _store or _content:
        threading.Thread(target=maintenance_loop, daemon=True).start()
    if UPDATE_URL:                                 # opt-in self-update check (Slice 6)
        threading.Thread(target=update_check_loop, daemon=True).start()
    # Second listener: raw-content endpoints (search). Bound on 0.0.0.0 inside the container but published
    # ONLY to host loopback (compose 127.0.0.1:8100:8100), so a connection landing here is provably local.
    content_httpd = ThreadingHTTPServer((CONTENT_BIND, CONTENT_PORT), Handler)
    threading.Thread(target=content_httpd.serve_forever, daemon=True).start()
    print(f"[collector] content port {CONTENT_BIND}:{CONTENT_PORT} (loopback-only transcript search, "
          f"runtime={RUNTIME})", flush=True)
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[collector] listening on :{PORT}  (window_default={WINDOW_DEFAULT} "
          f"threshold={COMPACT_THRESH} local_scan={LOCAL_SCAN})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()

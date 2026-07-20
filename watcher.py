#!/usr/bin/env python3
"""
cc-observability watcher
========================
Passive Claude Code transcript watcher. Polls ~/.claude/projects/**/*.jsonl,
extracts the live context-window occupancy + subagent activity for each active
session, and POSTs raw facts to the collector. Stdlib only — no pip deps.

Design: fire-and-forget POST with a short timeout so an unreachable collector
(e.g. a travel laptop off-LAN) NEVER stalls anything. Off Claude Code's hot path.

Token math (verified 2026-06-04, see PLAN.md in the project docs):
  context occupancy = latest non-sidechain assistant message's
      input_tokens + cache_creation_input_tokens + cache_read_input_tokens
The watcher ships RAW numbers; the collector owns window/threshold policy.
"""
import json
import os
import socket
import sys
import time
import urllib.request

# ---- config (env-overridable) ------------------------------------------------
COLLECTOR_URL = os.environ.get("CC_COLLECTOR_URL", "http://localhost:8099/ingest")
INGEST_TOKEN  = os.environ.get("CC_INGEST_TOKEN", "")               # if set, sent as X-CC-Token (server may require it)
PROJECTS_DIR  = os.path.expanduser(os.environ.get("CC_PROJECTS_DIR", "~/.claude/projects"))
# Extra transcript roots: Claude Desktop "Cowork"/local-agent-mode runs the Code engine and writes the
# SAME JSONL. Auto-include if present; override with CC_PROJECTS_DIRS (comma-separated).
_DEFAULT_EXTRA = [os.path.expanduser("~/.config/Claude/local-agent-mode-sessions")]
if os.environ.get("CC_PROJECTS_DIRS"):
    PROJECTS_DIRS = [os.path.expanduser(p.strip()) for p in os.environ["CC_PROJECTS_DIRS"].split(",") if p.strip()]
else:
    PROJECTS_DIRS = [PROJECTS_DIR] + [d for d in _DEFAULT_EXTRA if os.path.isdir(d)]
# Per-session state/status files written by the hooks + statusline wrapper.
STATE_DIR     = os.path.expanduser(os.environ.get("CC_STATE_DIR", "~/.cache/cc-dashboard"))
HOSTNAME      = os.environ.get("CC_HOST", socket.gethostname())
# Which Claude account this host is logged into (rate limits + cost are account-scoped, and a fleet can
# span >1 account). Prefer the per-session value the statusline hook writes into status.json; this is the
# host-level fallback for watcher-only hosts (no hooks). CC_ACCOUNT overrides; else read ~/.claude.json
# (native read on the host — for the dashboard CONTAINER the hook's status.json supplies it instead, so
# the OAuth tokens in ~/.claude.json never need to be mounted in).
def _host_account():
    acct = os.environ.get("CC_ACCOUNT")
    if acct:
        return acct
    try:
        with open(os.path.expanduser(os.environ.get("CC_CLAUDE_JSON", "~/.claude.json"))) as f:
            return (json.load(f).get("oauthAccount") or {}).get("emailAddress")
    except Exception:
        return None
ACCOUNT = _host_account()
POLL_INTERVAL = float(os.environ.get("CC_POLL_INTERVAL", "10"))     # seconds (context creeps slowly; keep it light)
ACTIVE_HOURS  = float(os.environ.get("CC_ACTIVE_HOURS", "6"))       # only report sessions touched within this window
TAIL_BYTES    = int(os.environ.get("CC_TAIL_BYTES", str(3 * 1024 * 1024)))  # bytes to read from EOF
POST_TIMEOUT  = float(os.environ.get("CC_POST_TIMEOUT", "1.0"))     # short → never blocks
# Recent-activity tail shipped with each report so the collector can serve a REMOTE session's drill-in
# stream (the transcript lives here, not on the dashboard host). Kept small to stay light on every poll.
ACT_TAIL_N    = int(os.environ.get("CC_ACT_TAIL_N", "20"))          # items shipped per session
ACT_TAIL_BYTES= int(os.environ.get("CC_ACT_TAIL_BYTES", str(256 * 1024)))  # EOF bytes scanned for activity
ACT_TEXT_CAP  = int(os.environ.get("CC_ACT_TEXT_CAP", "600"))       # per-item text truncation


def log(*a):
    print(f"[watcher {time.strftime('%H:%M:%S')}]", *a, file=sys.stderr, flush=True)


SUBAGENT_RUNNING_SECS = float(os.environ.get("CC_SUBAGENT_RUNNING_SECS", "30"))   # mtime fresher than this = running
SUBAGENT_WINDOW       = float(os.environ.get("CC_SUBAGENT_WINDOW", "900"))        # show agents active within 15 min


def _first_prompt(path):
    """The subagent's task prompt = first line's message content."""
    try:
        with open(path) as f:
            d = json.loads(f.readline())
        c = (d.get("message", {}) or {}).get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            for it in c:
                if isinstance(it, dict) and it.get("type") == "text":
                    return it.get("text", "")
                if isinstance(it, str):
                    return it
    except Exception:  # noqa: BLE001
        pass
    return ""


def scan_subagents(session_path, meta, now):
    """List recent/active subagents from <session>/subagents/agent-*.jsonl (live mtime).
    Enrich type+label from `meta` (main-transcript Task/Agent tool_use inputs)."""
    base = session_path[:-6] if session_path.endswith(".jsonl") else session_path
    sdir = os.path.join(base, "subagents")
    if not os.path.isdir(sdir):
        return None  # signal "no per-agent files here" -> caller falls back to main transcript
    files = []
    try:
        for n in os.listdir(sdir):
            if n.startswith("agent-") and n.endswith(".jsonl"):
                p = os.path.join(sdir, n)
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                if now - mt <= SUBAGENT_WINDOW:
                    files.append((mt, p, n))
    except OSError:
        return None
    files.sort(reverse=True)
    rows = []
    for mt, p, n in files[:12]:
        prompt = _first_prompt(p)
        type_, label = None, None
        for m in meta:
            mp = m.get("prompt") or ""
            if mp and prompt and mp[:80] == prompt[:80]:
                type_, label = m.get("type"), m.get("description")
                break
        rows.append({
            "id": n[6:14],
            "type": type_ or "subagent",
            "label": label or (prompt[:60] or "subagent"),
            "status": "running" if (now - mt) < SUBAGENT_RUNNING_SECS else "done",
        })
    rows.sort(key=lambda s: 0 if s["status"] == "running" else 1)  # running first (already mtime-desc)
    return rows


def tail_lines(path, nbytes):
    """Return the last `nbytes` of a file decoded into complete-ish lines."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
                f.readline()  # discard partial first line
            data = f.read()
        return data.decode("utf-8", "replace").splitlines()
    except OSError:
        return []


# ---- activity stream (shared by the remote watcher's push and the collector's local read) -----------
_TOOL_KEYS = ("command", "file_path", "pattern", "path", "url", "query", "description", "prompt")


def _tool_summary(inp):
    if not isinstance(inp, dict):
        return ""
    for k in _TOOL_KEYS:
        v = inp.get(k)
        if v:
            return str(v).replace("\n", " ")[:140]
    return ""


def _ask_fields(inp):
    """Extract the human-readable question(s) + option labels from an AskUserQuestion tool_use input,
    so a multiple-choice prompt is readable (and answerable by number) in the activity stream — the
    generic tool summary drops them entirely (none of its keys match AskUserQuestion's shape)."""
    qs = (inp or {}).get("questions") if isinstance(inp, dict) else None
    questions, options = [], []
    for q in qs or []:
        if not isinstance(q, dict):
            continue
        if q.get("question"):
            questions.append(str(q["question"]).replace("\n", " "))
        for o in q.get("options") or []:
            if isinstance(o, dict) and o.get("label"):
                options.append(str(o["label"]).replace("\n", " "))
    return {"question": " / ".join(questions)[:280], "options": options[:8]}


def activity_from_lines(lines, n=ACT_TAIL_N, text_cap=ACT_TEXT_CAP):
    """Render transcript JSONL lines into a readable activity stream — assistant text/thinking,
    tool calls, tool results. Chronological; returns the last n items."""
    items = []
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        typ = d.get("type")
        msg = d.get("message", {}) or {}
        content = msg.get("content")
        ts = d.get("timestamp")
        side = bool(d.get("isSidechain"))
        if typ == "assistant":
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    t = c.get("type")
                    if t == "text" and (c.get("text") or "").strip():
                        items.append({"kind": "text", "text": c["text"][:text_cap], "ts": ts, "side": side})
                    elif t == "thinking" and (c.get("thinking") or "").strip():
                        items.append({"kind": "thinking", "text": c["thinking"][:text_cap], "ts": ts, "side": side})
                    elif t == "tool_use":
                        if c.get("name") == "AskUserQuestion":
                            items.append({"kind": "ask", **_ask_fields(c.get("input")), "ts": ts, "side": side})
                        else:
                            items.append({"kind": "tool", "name": c.get("name"),
                                          "summary": _tool_summary(c.get("input")), "ts": ts, "side": side})
            elif isinstance(content, str) and content.strip():
                items.append({"kind": "text", "text": content[:text_cap], "ts": ts, "side": side})
        elif typ == "user" and isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    txt = c.get("content")
                    if isinstance(txt, list):
                        txt = " ".join(x.get("text", "") for x in txt if isinstance(x, dict))
                    items.append({"kind": "result", "text": (str(txt) if txt else "")[:text_cap // 2],
                                  "error": bool(c.get("is_error")), "ts": ts, "side": side})
    return items[-n:]


def read_activity(path, n=30, text_cap=ACT_TEXT_CAP):
    """Tail one transcript file (last 512 KB) into the last n activity items.
    Used by the collector for sessions whose transcript is local to the dashboard host."""
    return activity_from_lines(tail_lines(path, 512 * 1024), n, text_cap)


def parse_mcp_name(name):
    """MCP tool_use names look like 'mcp__<server>__<tool>' (e.g. 'mcp__github__create_issue').
    Returns (server, tool) for an MCP call, or (None, None) for a normal tool / junk. The "MCP tax"
    detector (Slice 1) uses this to attribute observed tool calls to the server that injected them."""
    if not isinstance(name, str) or not name.startswith("mcp__"):
        return None, None
    parts = name.split("__", 2)
    if len(parts) < 2 or not parts[1]:
        return None, None
    return parts[1], (parts[2] if len(parts) > 2 else "")


def parse_session(path):
    """Extract a snapshot dict from one transcript file, or None."""
    lines = tail_lines(path, TAIL_BYTES)
    if not lines:
        return None

    context_tokens = None
    output_tokens = None
    model = None
    last_ts = None
    cwd = None
    git_branch = None
    sidechain_recent = 0
    started = {}        # tool_use_id -> {label, type, name, ts}  (subagents/workflows launched)
    completed = set()   # tool_use_ids that have a tool_result (finished)
    meta = []           # {prompt, type, description} for enriching the per-agent files
    mcp = {}            # server -> {"tools": set(), "calls": int}  — observed MCP usage (Slice 1, "MCP tax")
    mcp_ids = {}        # mcp tool_use_id -> server (to match error tool_results back to a server)
    mcp_errored = set() # tool_use_ids whose tool_result carried is_error

    # Walk newest-first; first non-sidechain assistant w/ usage wins the gauge.
    for line in reversed(lines):
        if '"usage"' not in line and '"isSidechain"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        is_side = d.get("isSidechain")
        if is_side:
            sidechain_recent += 1
        msg = d.get("message", {}) or {}
        content = msg.get("content")
        typ = d.get("type")

        if typ == "assistant" and not is_side:
            u = msg.get("usage") or {}
            if context_tokens is None and u:
                context_tokens = (
                    int(u.get("input_tokens", 0))
                    + int(u.get("cache_creation_input_tokens", 0))
                    + int(u.get("cache_read_input_tokens", 0))
                )
                output_tokens = int(u.get("output_tokens", 0))
                model = msg.get("model")
                last_ts = d.get("timestamp")
                cwd = d.get("cwd")
                git_branch = d.get("gitBranch")
            if isinstance(content, list):       # subagent / workflow launches + MCP tool calls
                for c in content:
                    if not (isinstance(c, dict) and c.get("type") == "tool_use"):
                        continue
                    srv, tool = parse_mcp_name(c.get("name"))
                    if srv:                     # MCP tool call → observed usage (Slice 1)
                        ent = mcp.setdefault(srv, {"tools": set(), "calls": 0})
                        ent["calls"] += 1
                        if tool:
                            ent["tools"].add(tool)
                        if c.get("id"):
                            mcp_ids[c["id"]] = srv
                    if c.get("name") in ("Task", "Agent", "Workflow"):
                        cid = c.get("id")
                        if cid and cid not in started:
                            inp = c.get("input") or {}
                            label = inp.get("description") or inp.get("name") \
                                or (inp.get("prompt", "")[:60]) or c["name"]
                            started[cid] = {"id": cid, "name": c["name"], "label": label,
                                            "type": inp.get("subagent_type"), "ts": d.get("timestamp")}
                            meta.append({"prompt": inp.get("prompt"),
                                         "type": inp.get("subagent_type"),
                                         "description": inp.get("description")})
        elif typ == "user" and isinstance(content, list):   # tool_results mark completion
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    tid = c.get("tool_use_id")
                    if tid:
                        completed.add(tid)
                        if c.get("is_error"):
                            mcp_errored.add(tid)

    if context_tokens is None:
        return None

    # Prefer the per-agent files (live mtime → true "running" even mid-turn). Fall back to the
    # main transcript's tool_use/tool_result pairing when there's no subagents/ dir.
    subs = scan_subagents(path, meta, time.time())
    if subs is None:
        subs = []
        for cid, info in started.items():
            info["status"] = "done" if cid in completed else "running"
            subs.append(info)
        subs.sort(key=lambda s: s.get("ts") or "", reverse=True)
        subs.sort(key=lambda s: 0 if s["status"] == "running" else 1)
        subs = subs[:12]
    running = sum(1 for s in subs if s["status"] == "running")

    # Roll observed MCP usage into {server: {tools, calls, errors}} (errors = mcp tool_use_ids that errored).
    mcp_observed = {}
    for srv, ent in mcp.items():
        errs = sum(1 for cid0, s in mcp_ids.items() if s == srv and cid0 in mcp_errored)
        mcp_observed[srv] = {"tools": sorted(ent["tools"]), "calls": ent["calls"], "errors": errs}

    session_id = os.path.splitext(os.path.basename(path))[0]
    project = os.path.basename(cwd) if cwd else "?"
    # Ship a small recent-activity tail so the collector can serve this session's drill-in stream even
    # when its transcript lives only here (remote host). Separate small tail keeps the per-poll cost low.
    activity = activity_from_lines(tail_lines(path, ACT_TAIL_BYTES))

    # Baseline state derived from the transcript alone (no hooks needed → works on watcher-only hosts):
    #   running subagents -> working;  last non-sidechain msg is a completed assistant turn -> waiting
    #   (Claude finished, your move);  last is a user/tool msg -> working;  nothing -> idle.
    # On hosts that ALSO run the state hooks, collect_snapshots overrides this with the precise signal
    # (which additionally catches permission-prompt waits).
    derived_state, derived_awaiting = "idle", None
    if running > 0:
        derived_state = "working"
    else:
        for line in reversed(lines):
            try:
                d2 = json.loads(line)
            except ValueError:
                continue
            if d2.get("isSidechain"):
                continue
            t = d2.get("type")
            if t == "assistant":
                derived_state, derived_awaiting = "waiting", d2.get("timestamp")
                break
            if t == "user":
                derived_state = "working"
                break

    return {
        "host": HOSTNAME,
        "account": ACCOUNT,          # host-current login; overridden by the per-session hook value if present
        "account_approx": True,      # host-derived guess unless a hook status.json supplies the exact account
        "session_id": session_id,
        "model": model,
        "context_tokens": context_tokens,
        "output_tokens": output_tokens,
        "project": project,
        "cwd": cwd,
        "git_branch": git_branch,
        "transcript_ts": last_ts,
        "transcript_path": path,
        "sidechain_recent": sidechain_recent,
        "mcp_observed": mcp_observed,                 # {server: {tools, calls, errors}} — MCP tax Slice 1
        "subagents": subs,
        "subagent_running": running,
        "activity": activity,
        "state": derived_state,                       # transcript-derived baseline (no hooks)
        "needs_me": derived_state == "waiting",
        "awaiting_input_since": derived_awaiting,
        "file_mtime": os.path.getmtime(path),
    }


def find_active_files():
    cutoff = time.time() - ACTIVE_HOURS * 3600
    out = []
    for base in PROJECTS_DIRS:
        for root, _dirs, files in os.walk(base):
            if "subagents" in root.split(os.sep):
                continue  # subagent transcripts aren't sessions; handled via the main file's tool_use
            for name in files:
                if not name.endswith(".jsonl") or name == "audit.jsonl":
                    continue  # audit.jsonl (Cowork) isn't a session transcript
                p = os.path.join(root, name)
                try:
                    if os.path.getmtime(p) >= cutoff:
                        out.append(p)
                except OSError:
                    pass
    return out


def all_transcript_files():
    """Every session transcript (no recency cutoff) — for a one-time historical event backfill."""
    out = []
    for base in PROJECTS_DIRS:
        for root, _dirs, files in os.walk(base):
            if "subagents" in root.split(os.sep):
                continue
            for name in files:
                if name.endswith(".jsonl") and name != "audit.jsonl":
                    out.append(os.path.join(root, name))
    return out


def read_state_files(now):
    """Return {session_id: {...}} merged from the hooks' <sid>.state.json + statusline <sid>.status.json."""
    out = {}
    if not os.path.isdir(STATE_DIR):
        return out
    try:
        names = os.listdir(STATE_DIR)
    except OSError:
        return out
    for n in names:
        if n.startswith("."):
            continue
        if n.endswith(".state.json"):
            sid, kind = n[:-len(".state.json")], "state"
        elif n.endswith(".status.json"):
            sid, kind = n[:-len(".status.json")], "status"
        elif n.endswith(".target.json"):
            sid, kind = n[:-len(".target.json")], "target"
        else:
            continue
        p = os.path.join(STATE_DIR, n)
        try:
            mt = os.path.getmtime(p)
            with open(p) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        rec = out.setdefault(sid, {})
        rec.setdefault("cwd", d.get("cwd"))
        if kind == "state":
            rec.update(state=d.get("state"), awaiting_input_since=d.get("awaiting_input_since"),
                       last_hook=d.get("last_hook"), notif_kind=d.get("notif_kind"), state_mtime=mt)
        elif kind == "target":
            # a tmux pane was recorded for this session -> answer-from-phone can reach it (this host).
            rec.update(answerable=True, tmux_pane=d.get("tmux_pane"), target_mtime=mt)
        else:
            rec.update(auth_window=d.get("context_window_size"), auth_used_pct=d.get("used_percentage"),
                       cost_usd=d.get("cost_usd"), rate_limits=d.get("rate_limits"),
                       model_display=d.get("model"), account=d.get("account"),
                       extra_usage=d.get("extra_usage"), status_mtime=mt)
    return out


def collect_snapshots():
    """Union of transcript-derived sessions and state/status-file sessions, merged by session_id.
    State-only sessions (waiting, but transcript idle) still surface if their state file is fresh."""
    now = time.time()
    cutoff = now - ACTIVE_HOURS * 3600
    snaps = {}
    for path in find_active_files():
        s = parse_session(path)
        if s:
            snaps[s["session_id"]] = s
    for sid, st in read_state_files(now).items():
        fresh = max(st.get("state_mtime", 0), st.get("status_mtime", 0), st.get("target_mtime", 0))
        base = snaps.get(sid)
        if base is None:
            if fresh < cutoff:
                continue  # stale state-only session — drop
            proj = os.path.basename(st["cwd"]) if st.get("cwd") else "?"
            base = {"host": HOSTNAME, "session_id": sid, "model": st.get("model_display"),
                    "context_tokens": 0, "output_tokens": 0, "project": proj, "cwd": st.get("cwd"),
                    "git_branch": None, "transcript_ts": None, "sidechain_recent": 0,
                    "subagents": [], "subagent_running": 0, "file_mtime": fresh}
            snaps[sid] = base
        base["answerable"] = bool(st.get("answerable"))
        # Only a .state.json carries an explicit `state` (read_state_files sets it only for kind=="state").
        # A statusline-only .status.json has no `state` key, so st.get("state") is None — it must NOT clobber
        # the transcript-derived 'waiting'/'working'. (Bug: hosts that run the statusline hook but not the
        # state hooks — e.g. a host with only the statusline installed — had every session forced to
        # "unknown"/needs_me=False, so a session
        # genuinely awaiting input never surfaced in Triage.)
        if st.get("state") is not None:
            base["state"] = st.get("state")
            base["awaiting_input_since"] = st.get("awaiting_input_since")
            base["needs_me"] = (st.get("state") == "waiting")
            # which hook last set the state, + (for Notifications) whether it was a permission prompt vs an
            # idle nudge — lets the server promote ONLY genuine blocks to "needs you", not plain Stop/idle.
            base["last_hook"] = st.get("last_hook")
            base["notif_kind"] = st.get("notif_kind")
        base["state_age"] = round(now - st["state_mtime"], 1) if st.get("state_mtime") else None
        base["auth_window"] = st.get("auth_window")
        base["auth_used_pct"] = st.get("auth_used_pct")
        base["cost_usd"] = st.get("cost_usd")
        base["rate_limits"] = st.get("rate_limits")
        # account: the statusline hook value (per-session, point-in-time EXACT) wins over the host-level
        # ACCOUNT fallback (current login = APPROXIMATE). account_approx flags which one we used.
        hook_acct = st.get("account")
        base["account"] = hook_acct or base.get("account") or ACCOUNT
        base["account_approx"] = not hook_acct
        base["extra_usage"] = st.get("extra_usage")   # whether the account can exceed its plan cap (overage)
        if st.get("model_display") and not base.get("model"):
            base["model"] = st.get("model_display")
        # state-only / early session: approximate token count from authoritative %
        if not base.get("context_tokens") and st.get("auth_window") and st.get("auth_used_pct") is not None:
            base["context_tokens"] = int(st["auth_window"] * st["auth_used_pct"] / 100.0)
    return list(snaps.values())


def post(payload, url=None):
    url = url or COLLECTOR_URL   # resolve at call time (tests override COLLECTOR_URL after import)
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if INGEST_TOKEN:
        headers["X-CC-Token"] = INGEST_TOKEN
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        urllib.request.urlopen(req, timeout=POST_TIMEOUT).read()
    except Exception as e:  # noqa: BLE001 — fire-and-forget, never block
        log("post failed (ok, will retry):", e)


# Remote cost ingestion: extract per-message token COUNTS only (no prompts/responses) and POST them so a
# watcher-only host's spend shows in the dashboard's per-account cost. The server computes cost + attributes
# the account per-session. Tail-bounded + dedup-safe (server INSERT OR IGNORE on message_id+request_id).
USAGE_URL = (COLLECTOR_URL + "-usage") if COLLECTOR_URL.endswith("/ingest") else (COLLECTOR_URL + "/usage")
USAGE_INTERVAL = float(os.environ.get("CC_USAGE_INTERVAL", "60"))   # seconds between usage POSTs (cost creeps slowly)


def usage_events(path):
    """Minimal token-usage events from a transcript tail — IDs + token counts + model + ts only, NEVER
    conversation content. Includes sidechain (subagent) messages (tagged is_sidechain) — that usage is
    billed, so it belongs in the cost; the server tags it so the compact ETA can still skip it."""
    out = []
    for ln in tail_lines(path, TAIL_BYTES):
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if d.get("type") != "assistant":
            continue
        m = d.get("message") or {}
        u = m.get("usage") or {}
        mid, rid = m.get("id"), d.get("requestId")
        if not u or not mid or not rid:
            continue
        out.append({
            "message_id": mid, "request_id": rid, "session_id": d.get("sessionId"),
            "project": os.path.basename(d["cwd"]) if d.get("cwd") else None,
            "model": m.get("model"), "ts": d.get("timestamp"),
            "is_sidechain": bool(d.get("isSidechain")),
            "usage": {"input_tokens": u.get("input_tokens", 0),
                      "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
                      "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
                      "output_tokens": u.get("output_tokens", 0)},
        })
    return out


def main():
    log(f"host={HOSTNAME} roots={PROJECTS_DIRS} state={STATE_DIR} -> {COLLECTOR_URL} every {POLL_INTERVAL}s")
    last_usage = 0.0
    while True:
        start = time.monotonic()
        snaps = collect_snapshots()
        for snap in snaps:
            post(snap)
        if not snaps:
            # No active sessions -> still send a heartbeat so the dashboard can tell an idle-but-healthy
            # watcher apart from a dead/erroring one (P4 fleet-health). Session POSTs already keep us GREEN.
            post({"host": HOSTNAME, "beacon": True})
        # remote cost ingestion: every USAGE_INTERVAL, ship token counts (no content) for the per-account cost
        if start - last_usage >= USAGE_INTERVAL:
            events = []
            for path in find_active_files():
                events.extend(usage_events(path))
            if events:
                post({"host": HOSTNAME, "events": events}, USAGE_URL)
            last_usage = start
        time.sleep(max(0.0, POLL_INTERVAL - (time.monotonic() - start)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass

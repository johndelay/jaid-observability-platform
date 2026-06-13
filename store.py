#!/usr/bin/env python3
"""
cc-observability — usage store (SQLite, stdlib only)
====================================================
Persists per-message token usage + computed cost so the dashboard can show cost HISTORY,
burn-rate, and a time-to-compact ETA across sessions/projects/days.

Idempotent + compaction-safe by construction: each row is keyed `(message_id, request_id)`
with INSERT OR IGNORE. Re-scanning a transcript re-inserts nothing; compaction replays
assistant messages with the SAME `message.id`, so dedup collapses them — no double-count, no
special-case logic (a ccusage-inspired insight).

WAL mode + a single guarded connection: a writer (the collector ingest loop) and readers
(HTTP handlers) coexist without locking errors under this light load.
"""
import json
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone

import costing

SESSION_GAP = 5 * 3600          # ccusage "5h block": a >5h gap starts a new block
RATE_LOOKBACK = float(os.environ.get("CC_RATE_LOOKBACK", str(3 * 3600)))   # window for the Extra-Usage burn slope

# Dumb-zone absolute context-token bands — MUST mirror ZONE_TOK in web/index.html (the UI source of truth).
# Used by zone_time() (Craft Score Hygiene) + drift_curve() (dumb-zone v2). Sharp <good ≤ Good <drift ≤ Drifting <danger ≤ Danger.
ZONE_TOK_GOOD, ZONE_TOK_DRIFT, ZONE_TOK_DANGER = 50_000, 120_000, 200_000
ZONE_BANDS = ("sharp", "good", "drift", "danger")              # ascending context-fill order
ZONE_BAND_FLOOR = {"sharp": 0, "good": ZONE_TOK_GOOD, "drift": ZONE_TOK_DRIFT, "danger": ZONE_TOK_DANGER}


def zone_band(ctx):
    """Map an absolute context-token count to its dumb-zone band name (mirrors the UI's zone())."""
    if ctx >= ZONE_TOK_DANGER:
        return "danger"
    if ctx >= ZONE_TOK_DRIFT:
        return "drift"
    if ctx >= ZONE_TOK_GOOD:
        return "good"
    return "sharp"


def model_family(model):
    """Coarse model family for the mix chart: opus | sonnet | haiku | fable | mythos | other.
    Single source of truth = costing.family_of (so pricing + the mix chart agree on families)."""
    return costing.family_of(model) or "other"


# Right-sizing tiers for the what-if downgrade analysis (by family, cheapest-target-aware):
_PREMIUM = ("opus", "fable", "mythos")               # pricier than Sonnet → candidates to downgrade → Sonnet
_ABOVE_HAIKU = ("opus", "sonnet", "fable", "mythos")  # pricier than Haiku → candidates (if trivial) → Haiku
# Must be WRITABLE. In Docker the transcript/state mounts are read-only, so the compose sets
# CC_DB_PATH to a dedicated rw volume; outside Docker we default under ~/.cache.
DEFAULT_PATH = os.environ.get("CC_DB_PATH") or os.path.expanduser("~/.cache/cc-observability/cc-usage.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
  message_id     TEXT NOT NULL,
  request_id     TEXT NOT NULL,
  session_id     TEXT,
  project        TEXT,
  host           TEXT,
  account        TEXT,
  ts             REAL,
  model          TEXT,
  input_tokens   INTEGER,
  cache_creation INTEGER,
  cache_read     INTEGER,
  output_tokens  INTEGER,
  context_tokens INTEGER,
  cost_usd       REAL,
  is_sidechain   INTEGER,            -- 1 = subagent (sidechain) message. Counted in cost (it IS billed),
                                     -- but excluded from the main-thread compact ETA's context slope.
  PRIMARY KEY (message_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_session_ts ON usage(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);

-- A session runs under ONE Claude account, but a machine can switch accounts between sessions (and a user
-- may have many accounts). Transcripts don't record the account, so we capture it per-session at runtime:
--   source='hook' -> the statusline hook read ~/.claude.json DURING the session = point-in-time exact.
--   source='host' -> derived from the host's CURRENT ~/.claude.json (watcher-only hosts, no hook) = approximate.
-- This map is the single source of truth for attributing both live state and historical cost.
CREATE TABLE IF NOT EXISTS session_account (
  session_id TEXT PRIMARY KEY,
  account    TEXT,
  host       TEXT,
  source     TEXT,
  updated_at REAL
);

-- Time series of plan rate-limit usage per (account, window), for the burn-based ETA-to-Extra-Usage.
-- window = 'five_hour' | 'seven_day'. Recorded throttled (on change or every few min). used_pct resets
-- to ~0 when the rolling window rolls over (detected via a drop or a changed resets_at).
CREATE TABLE IF NOT EXISTS rate_history (
  account   TEXT,
  window    TEXT,
  ts        REAL,
  used_pct  REAL,
  resets_at REAL
);
CREATE INDEX IF NOT EXISTS idx_rate_history ON rate_history(account, window, ts);

-- Per-session UI prefs that must follow the user across devices (unlike per-device localStorage):
-- hidden = the user archived this session out of Triage; nickname = a user-given name (reserved).
CREATE TABLE IF NOT EXISTS session_prefs (
  session_id TEXT PRIMARY KEY,
  hidden     INTEGER DEFAULT 0,
  nickname   TEXT,
  updated_at REAL
);

-- Discrete behavioral events mined from transcripts (the "be-better" substrate, Phase 6). type ∈
-- {compaction, skill_use, subagent_spawn, tool_error, mcp_call}. event_id is a stable key (line uuid + type + ref)
-- so re-tailing a transcript is idempotent (INSERT OR IGNORE). payload_json holds type-specific extras
-- (e.g. compaction → {trigger: manual|auto, pre_tokens, duration_ms}).
CREATE TABLE IF NOT EXISTS events (
  event_id     TEXT PRIMARY KEY,
  ts           REAL,
  session_id   TEXT,
  account      TEXT,
  host         TEXT,
  type         TEXT,
  ref          TEXT,
  payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts);

-- Small key/value store for one-time flags (e.g. "did the event backfill run") + future migrations.
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Latest MCP-cost probe per host (MCP tax Slice 2). servers_json holds ONLY [{name,status,...}] — the
-- probe NEVER ships launch commands / URLs / env (those can hold secrets). One row per host (upsert).
CREATE TABLE IF NOT EXISTS mcp_reports (host TEXT PRIMARY KEY, ts REAL, servers_json TEXT);
"""


def _merge_intervals(ivs):
    """Union overlapping/touching [(start, end)] intervals → sorted, merged list."""
    merged = []
    for s, e in sorted(ivs):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def parse_ts(iso):
    """ISO8601 (with Z and optional fractional secs) → epoch seconds. None if unparseable."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def extract_usage(d, project=None, host=None, account=None):
    """Transcript entry dict → a usage row dict (or None). Needs message.id + top-level requestId for the
    dedup key. Sidechain (subagent) messages ARE included — that usage is billed, so excluding it made the
    session/account cost run low (the ~5% gap). They're tagged is_sidechain=1 so the compact ETA can still
    ignore their context (subagent context doesn't count toward the main thread's auto-compact)."""
    if d.get("type") != "assistant":
        return None
    m = d.get("message") or {}
    u = m.get("usage") or {}
    mid, rid = m.get("id"), d.get("requestId")
    if not u or not mid or not rid:
        return None
    it = int(u.get("input_tokens", 0) or 0)
    cc = int(u.get("cache_creation_input_tokens", 0) or 0)
    cr = int(u.get("cache_read_input_tokens", 0) or 0)
    ot = int(u.get("output_tokens", 0) or 0)
    model = m.get("model")
    if project is None and d.get("cwd"):
        project = os.path.basename(d["cwd"])
    return {
        "message_id": mid, "request_id": rid, "session_id": d.get("sessionId"),
        "project": project, "host": host, "account": account,
        "ts": parse_ts(d.get("timestamp")), "model": model,
        "input_tokens": it, "cache_creation": cc, "cache_read": cr, "output_tokens": ot,
        "context_tokens": it + cc + cr, "cost_usd": costing.line_cost(u, model),
        "is_sidechain": 1 if d.get("isSidechain") else 0,
    }


_COLS = ("message_id", "request_id", "session_id", "project", "host", "account", "ts", "model",
         "input_tokens", "cache_creation", "cache_read", "output_tokens", "context_tokens", "cost_usd",
         "is_sidechain")

_EVENT_COLS = ("event_id", "ts", "session_id", "account", "host", "type", "ref", "payload_json")


# ---- schema migrations (forward-only, ordered) ----
# Replaces the old ad-hoc "ALTER if column missing" approach. Each migration is keyed on an integer version
# stored in meta['schema_version']; on startup we apply every migration whose version > the stored one, in
# order, bumping the stored version as we go. The full current schema is always bootstrapped first via
# executescript(_SCHEMA) (CREATE IF NOT EXISTS), so migrations only carry column adds / data transforms that
# IF NOT EXISTS can't. Migrations MUST be idempotent: a pre-versioning DB starts at stored version 0, so even
# already-satisfied migrations re-run once to baseline it.
def _mig_001_usage_columns(db):
    """Baseline: ensure usage.account + usage.is_sidechain exist (added after the original schema shipped)."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(usage)")}
    if "account" not in cols:
        db.execute("ALTER TABLE usage ADD COLUMN account TEXT")
    if "is_sidechain" not in cols:
        db.execute("ALTER TABLE usage ADD COLUMN is_sidechain INTEGER")


def _mig_002_rate_history_unique(db):
    """rate_history had no unique key, so a portable re-import duplicated its rows. Dedup exact
    (account,window,ts) rows, then add a UNIQUE index → INSERT OR IGNORE is now idempotent (import-safe)."""
    db.execute("DELETE FROM rate_history WHERE rowid NOT IN "
               "(SELECT MIN(rowid) FROM rate_history GROUP BY account, window, ts)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_rate_history ON rate_history(account, window, ts)")


_MIGRATIONS = [
    (1, _mig_001_usage_columns),
    (2, _mig_002_rate_history_unique),
]
_CODE_SCHEMA_VERSION = _MIGRATIONS[-1][0] if _MIGRATIONS else 0

# Tables a portable bundle may import into (whitelist — the import table name must be one of these).
_IMPORT_TABLES = ("usage", "events", "rate_history", "session_account", "session_prefs", "mcp_reports", "meta")

# V13 Slice 4: TTL + write-generation memoize for the heavy full-scan aggregates. As live reporting grows and
# multiple clients poll, this stops every poll from recomputing the same scan. Correctness: a write bumps the
# generation, so new data is never served stale beyond the next write or the TTL — whichever comes first.
_AGG_TTL = float(os.environ.get("CC_AGG_TTL", "8"))


def _cached_agg(ttl=_AGG_TTL):
    """Decorator: memoize a read-only aggregate on (method, args) with a TTL, invalidated on any write."""
    def deco(fn):
        def wrap(self, *args, **kw):
            key = (fn.__name__, args, tuple(sorted(kw.items())))
            return self._agg_cached(key, ttl, lambda: fn(self, *args, **kw))
        wrap.__name__ = fn.__name__
        wrap.__doc__ = fn.__doc__
        return wrap
    return deco


def extract_events(d, host=None):
    """Transcript entry dict → list of behavioral-event row dicts (Phase 6). Captures:
      - compaction      (compactMetadata.trigger = manual|auto, + pre_tokens/duration_ms)
      - skill_use       (a `Skill` tool_use → the skill name)
      - subagent_spawn  (an `Agent` tool_use → the subagent_type)   NB: TaskCreate/TaskUpdate are todo tools, not subagents
      - tool_error      (a tool_result with is_error)
      - mcp_call        (an `mcp__<server>__<tool>` tool_use → {server, tool})   MCP tax: over-time usage / dead-weight
    event_id = "<line uuid>:<type>:<ref>" so re-tailing the same transcript is idempotent."""
    out = []
    uuid = d.get("uuid") or ""
    sid = d.get("sessionId")
    ts = parse_ts(d.get("timestamp"))
    def add(type_, ref, payload):
        out.append({"event_id": f"{uuid}:{type_}:{ref}", "ts": ts, "session_id": sid,
                    "account": None, "host": host, "type": type_, "ref": str(ref),
                    "payload_json": json.dumps(payload, separators=(",", ":"))})
    cm = d.get("compactMetadata")
    if cm:
        add("compaction", "compact", {"trigger": cm.get("trigger"),
                                      "pre_tokens": cm.get("preTokens"), "duration_ms": cm.get("durationMs")})
    msg = d.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t == "tool_use":
                nm, inp, tid = c.get("name"), (c.get("input") or {}), (c.get("id") or "")
                if nm == "Skill":
                    add("skill_use", tid, {"skill": inp.get("skill")})
                elif nm == "Agent":
                    add("subagent_spawn", tid, {"subagent_type": inp.get("subagent_type")})
                elif isinstance(nm, str) and nm.startswith("mcp__"):   # MCP tax: 'mcp__<server>__<tool>'
                    parts = nm.split("__", 2)
                    if len(parts) >= 2 and parts[1]:
                        add("mcp_call", tid, {"server": parts[1], "tool": parts[2] if len(parts) > 2 else ""})
            elif t == "tool_result" and (c.get("is_error") or c.get("isError")):
                add("tool_error", c.get("tool_use_id") or uuid, {})
    return out


class Store:
    def __init__(self, path=DEFAULT_PATH, pre_migrate_hook=None):
        self.path = path
        self._pre_migrate_hook = pre_migrate_hook   # called before a REAL migration advances (Slice 3 backup)
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        # self._db is the single WRITE connection (serialized by self._lock). Reads use short-lived
        # connections (see _read_conn) so they do NOT contend on the write lock — WAL allows unlimited
        # concurrent readers. In-memory DBs (CLI/tests) can't be reopened, so reads fall back to the shared
        # connection under the lock.
        self._shared_reads = ":memory:" in path
        self._lock = threading.Lock()
        self._agg = {}                      # aggregate memoize (Slice 4): key -> (gen, expires_at, value)
        self._agg_gen = 0                   # bumped on every write → invalidates the cache
        self._agg_lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript(_SCHEMA)
            self._run_migrations()
            self._db.commit()

    def _schema_version(self):
        """Current stored schema version (0 for a pre-versioning DB). Caller holds self._lock; meta exists."""
        row = self._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        try:
            return int(row["value"]) if row else 0
        except (ValueError, TypeError):
            return 0

    def _run_migrations(self):
        """Apply forward-only migrations with version > the stored one, bumping meta.schema_version. Caller
        holds self._lock and has already bootstrapped the full schema via executescript(_SCHEMA)."""
        cur = self._schema_version()
        pending = [(v, fn) for v, fn in _MIGRATIONS if v > cur]
        # Pre-upgrade backup safety net: only for a REAL advance of an already-versioned DB (cur>=1) — the
        # v0→1 baseline is harmless/idempotent and fresh DBs need no backup. Best-effort; never blocks startup.
        if pending and cur >= 1 and self._pre_migrate_hook:
            try:
                self._pre_migrate_hook(self)
            except Exception:  # noqa: BLE001
                pass
        for ver, fn in pending:
            fn(self._db)
            self._db.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(ver),))
            cur = ver

    def _read_conn(self):
        """A short-lived read-only connection. WAL allows unlimited concurrent readers, so reads no longer
        take self._lock (which now serializes writers only). busy_timeout covers VACUUM/checkpoint's brief
        exclusive window. Python's sqlite3 doesn't open a transaction for SELECT, so the connection holds no
        lock once the statement finishes."""
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=5000")
        return c

    def _bump_agg(self):
        """Invalidate the aggregate cache — called after any write that affects the cached scans."""
        with self._agg_lock:
            self._agg_gen += 1

    def _agg_cached(self, key, ttl, producer):
        now = time.time()
        with self._agg_lock:
            gen = self._agg_gen
            e = self._agg.get(key)
            if e and e[0] == gen and e[1] > now:
                return e[2]
        val = producer()                    # compute outside the lock
        with self._agg_lock:
            if self._agg_gen == gen:         # don't cache if a write landed mid-compute
                self._agg[key] = (gen, now + ttl, val)
        return val

    # ---- writes ----
    def record_many(self, rows):
        """INSERT OR IGNORE a batch of usage row dicts. Returns the count newly inserted."""
        rows = [r for r in rows if r]
        if not rows:
            return 0
        sql = f"INSERT OR IGNORE INTO usage ({','.join(_COLS)}) VALUES ({','.join('?' for _ in _COLS)})"
        with self._lock:
            before = self._db.total_changes
            self._db.executemany(sql, [[r.get(c) for c in _COLS] for r in rows])
            self._db.commit()
            n = self._db.total_changes - before
        if n:
            self._bump_agg()
        return n

    def record_remote_usage(self, events, host=None):
        """Record usage events POSTed by a remote watcher (token counts only — NO conversation content).
        Cost is computed here (server-side costing); account is resolved per-session from the map. Same
        (message_id, request_id) PK + INSERT OR IGNORE means re-sends and cross-host overlap dedup safely.
        Returns count newly inserted."""
        rows = []
        for e in events or []:
            u = e.get("usage") or {}
            mid, rid = e.get("message_id"), e.get("request_id")
            if not u or not mid or not rid:
                continue
            it = int(u.get("input_tokens", 0) or 0)
            cc = int(u.get("cache_creation_input_tokens", 0) or 0)
            cr = int(u.get("cache_read_input_tokens", 0) or 0)
            ot = int(u.get("output_tokens", 0) or 0)
            model, sid = e.get("model"), e.get("session_id")
            rows.append({
                "message_id": mid, "request_id": rid, "session_id": sid,
                "project": e.get("project"), "host": host, "account": self.account_for_session(sid),
                "ts": parse_ts(e.get("ts")), "model": model,
                "input_tokens": it, "cache_creation": cc, "cache_read": cr, "output_tokens": ot,
                "context_tokens": it + cc + cr, "cost_usd": costing.line_cost(u, model),
                "is_sidechain": 1 if e.get("is_sidechain") else 0,
            })
        return self.record_many(rows)

    def ingest_lines(self, lines, project=None, host=None, account=None):
        """Parse transcript JSONL lines → usage rows → store. Returns count inserted."""
        import json
        rows = []
        for ln in lines:
            try:
                rows.append(extract_usage(json.loads(ln), project, host, account))
            except ValueError:
                continue
        return self.record_many(rows)

    def set_session_account(self, session_id, account, host=None, source=None):
        """Upsert a session's account. A 'hook' (exact, point-in-time) reading always wins over a later
        'host' (approximate) one for the same session, so a brief watcher-only guess can't clobber the
        precise hook value. No-op without a session_id + account. Returns True if written."""
        if not session_id or not account:
            return False
        with self._lock:
            self._db.execute(
                "INSERT INTO session_account (session_id, account, host, source, updated_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET account=excluded.account, host=excluded.host, "
                "  source=excluded.source, updated_at=excluded.updated_at "
                "  WHERE excluded.source='hook' OR session_account.source IS NOT 'hook'",
                (session_id, account, host, source, time.time()))
            self._db.commit()
        return True

    def account_for_session(self, session_id):
        r = self._one("SELECT account FROM session_account WHERE session_id=?", (session_id,))
        return r["account"] if r else None

    def set_session_pref(self, session_id, hidden=None, nickname=None):
        """Upsert a session's UI prefs. Only the fields passed (not None) change; the rest are preserved.
        hidden is coerced to 0/1. No-op without a session_id. Returns True if written."""
        if not session_id:
            return False
        with self._lock:
            row = self._db.execute(
                "SELECT hidden, nickname FROM session_prefs WHERE session_id=?", (session_id,)).fetchone()
            h = (1 if hidden else 0) if hidden is not None else (row["hidden"] if row else 0)
            nk = nickname if nickname is not None else (row["nickname"] if row else None)
            self._db.execute(
                "INSERT INTO session_prefs (session_id, hidden, nickname, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET hidden=excluded.hidden, nickname=excluded.nickname, "
                "  updated_at=excluded.updated_at",
                (session_id, h, nk, time.time()))
            self._db.commit()
        return True

    def hidden_sessions(self):
        """Set of session_ids the user has archived (hidden=1)."""
        return {r["session_id"] for r in self._all("SELECT session_id FROM session_prefs WHERE hidden=1")}

    def session_prefs_all(self):
        """All session prefs as {session_id: {hidden, nickname}} (for tooling/inspection)."""
        return {r["session_id"]: {"hidden": bool(r["hidden"]), "nickname": r["nickname"]}
                for r in self._all("SELECT session_id, hidden, nickname FROM session_prefs")}

    def reattribute_accounts(self, only_null=False):
        """Re-derive usage rows' account from the session_account map: known session → its account,
        unknown session → NULL. only_null=True touches just currently-NULL rows (cheap, for late-learned
        sessions each cycle); only_null=False rewrites ALL rows (full correction — clears any prior blind/
        host-level backfill). Idempotent. Returns rows touched."""
        where = "WHERE account IS NULL AND session_id IN (SELECT session_id FROM session_account)" if only_null else ""
        with self._lock:
            cur = self._db.execute(
                "UPDATE usage SET account = (SELECT account FROM session_account sa "
                f"WHERE sa.session_id = usage.session_id) {where}")
            self._db.commit()
            return cur.rowcount

    # ---- reads / aggregations ----
    def _one(self, sql, args=()):
        if self._shared_reads:
            with self._lock:
                return self._db.execute(sql, args).fetchone()
        c = self._read_conn()
        try:
            return c.execute(sql, args).fetchone()
        finally:
            c.close()

    def _all(self, sql, args=()):
        if self._shared_reads:
            with self._lock:
                return self._db.execute(sql, args).fetchall()
        c = self._read_conn()
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.close()

    def session_total(self, session_id):
        r = self._one("SELECT COALESCE(SUM(cost_usd),0) c, COALESCE(SUM(input_tokens+output_tokens),0) t, "
                      "COUNT(*) n FROM usage WHERE session_id=?", (session_id,))
        return {"cost_usd": r["c"], "tokens": r["t"], "messages": r["n"]}

    def session_prefix_overhead(self, session_id):
        """MCP tax Slice 3: the cached static prefix a session pays on EVERY turn (tools + system + CLAUDE.md +
        memory + any MCP tool definitions), read from its FIRST turn — cache_creation + cache_read on the
        earliest main-thread message ≈ the cacheable prefix (the user's first message is the uncached
        input_tokens). These are REAL billed tokens (no probe, no config, no secrets, not an estimate). None if
        no rows. NOTE: this is the WHOLE prefix, not MCP-only — isolating per-server MCP cost would require
        reading server schemas/config, which the HARD INVARIANT forbids."""
        r = self._one("SELECT COALESCE(cache_creation,0)+COALESCE(cache_read,0) p FROM usage "
                      "WHERE session_id=? AND COALESCE(is_sidechain,0)=0 ORDER BY ts ASC LIMIT 1", (session_id,))
        return r["p"] if r and r["p"] else None

    def prefix_overhead_stats(self, days=30):
        """Typical context-floor overhead across recent sessions — the per-session first-turn prefix (see
        session_prefix_overhead), summarized. Returns {median, p90, max, sessions} or None. SQLite's min/max
        bare-column rule makes `... p, MIN(ts) ... GROUP BY session_id` take p from each session's earliest row."""
        cutoff = time.time() - days * 86400 if days else 0
        rows = self._all(
            "SELECT COALESCE(cache_creation,0)+COALESCE(cache_read,0) p, MIN(ts) m FROM usage "
            "WHERE COALESCE(is_sidechain,0)=0 AND ts>=? GROUP BY session_id", (cutoff,))
        vals = sorted(r["p"] for r in rows if r["p"])
        if not vals:
            return None
        pick = lambda q: vals[min(len(vals) - 1, int(len(vals) * q))]
        return {"median": pick(0.5), "p90": pick(0.9), "max": vals[-1], "sessions": len(vals)}

    @_cached_agg()
    def project_totals(self):
        return [{"project": r["project"], "cost_usd": r["c"], "messages": r["n"]}
                for r in self._all("SELECT project, COALESCE(SUM(cost_usd),0) c, COUNT(*) n "
                                   "FROM usage GROUP BY project ORDER BY c DESC")]

    @_cached_agg()
    def daily_totals(self, days=14):
        cutoff = time.time() - days * 86400
        return [{"day": r["d"], "cost_usd": r["c"], "messages": r["n"], "tokens": r["t"]}
                for r in self._all(
                    "SELECT date(ts,'unixepoch','localtime') d, COALESCE(SUM(cost_usd),0) c, COUNT(*) n, "
                    "COALESCE(SUM(input_tokens+output_tokens+cache_read+cache_creation),0) t "
                    "FROM usage WHERE ts>=? GROUP BY d ORDER BY d DESC", (cutoff,))]

    def total_cost(self):
        return self._one("SELECT COALESCE(SUM(cost_usd),0) c FROM usage")["c"]

    @_cached_agg()
    def all_time_totals(self):
        """Lifetime vanity totals for the Trophy scene (V10 Slice 1): estimated spend, message count,
        distinct sessions, and total tokens that flowed (input+output+cache_read+cache_creation)."""
        r = self._one("SELECT COALESCE(SUM(cost_usd),0) c, COUNT(*) m, COUNT(DISTINCT session_id) s, "
                      "COALESCE(SUM(input_tokens+output_tokens+cache_read+cache_creation),0) t FROM usage")
        return {"cost_usd": r["c"], "messages": r["m"], "sessions": r["s"], "tokens": r["t"]}

    @_cached_agg()
    def cost_breakdown(self, days=None):
        """Per-model token sums by bucket (input/output/cache_read/cache_creation) + grand totals — pure SQL,
        no pricing (the server applies costing.py rates for the cost split + the cache-savings estimate)."""
        where, args = "", ()
        if days:
            where, args = " WHERE ts>=?", (time.time() - days * 86400,)
        by_model = [{"model": r["model"], "input": r["it"], "output": r["ot"],
                     "cache_read": r["cr"], "cache_creation": r["cc"]}
                    for r in self._all(
                        "SELECT model, COALESCE(SUM(input_tokens),0) it, COALESCE(SUM(output_tokens),0) ot, "
                        "COALESCE(SUM(cache_read),0) cr, COALESCE(SUM(cache_creation),0) cc "
                        f"FROM usage{where} GROUP BY model", args)]
        totals = {k: sum(b[k] for b in by_model) for k in ("input", "output", "cache_read", "cache_creation")}
        return {"by_model": by_model, "totals": totals}

    def active_days_streak(self):
        """Streaks over distinct active LOCAL calendar days (Duolingo effect for the Trophy scene). current =
        consecutive days ending today (or yesterday, so a streak isn't 'broken' before today's first message);
        longest = the longest consecutive run ever; active_days = total distinct days with any usage."""
        days = [r["d"] for r in self._all(
            "SELECT DISTINCT date(ts,'unixepoch','localtime') d FROM usage ORDER BY d") if r["d"]]
        if not days:
            return {"current": 0, "longest": 0, "active_days": 0}
        dset = set(days)
        dates = sorted(date.fromisoformat(d) for d in days)
        longest = cur = 1
        for i in range(1, len(dates)):
            cur = cur + 1 if (dates[i] - dates[i - 1]).days == 1 else 1
            longest = max(longest, cur)
        today = date.today()
        anchor = today if today.isoformat() in dset else today - timedelta(days=1)
        current, d = 0, anchor
        while d.isoformat() in dset:
            current += 1
            d -= timedelta(days=1)
        return {"current": current, "longest": longest, "active_days": len(days)}

    @_cached_agg()
    def hourly_histogram(self, days=30):
        """Activity by day-of-week × hour over a trailing window (V10 Slice 4 'when you code' grid). dow =
        0(Sun)..6(Sat) to match JS getDay(); hour = 0..23. LOCAL time (matches the calendar). Sparse — only
        (dow,hour) cells with activity; the UI fills the full 24×7 grid. Returns [{dow,hour,messages,cost_usd}]."""
        cutoff = time.time() - days * 86400
        return [{"dow": int(r["w"]), "hour": int(r["h"]), "messages": r["n"], "cost_usd": r["c"]}
                for r in self._all(
                    "SELECT CAST(strftime('%w', ts, 'unixepoch', 'localtime') AS INTEGER) w, "
                    "CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) h, "
                    "COUNT(*) n, COALESCE(SUM(cost_usd),0) c "
                    "FROM usage WHERE ts>=? GROUP BY w, h", (cutoff,))]

    @_cached_agg()
    def daily_totals_by_model(self, days=30):
        """Per local day, message counts grouped by model FAMILY (opus/sonnet/haiku/other) — the V10 Slice 3
        model-mix-over-time stack. Returns [{day, opus?, sonnet?, haiku?, other?}] ascending by day; absent
        families just omit the key (UI treats missing as 0)."""
        cutoff = time.time() - days * 86400
        dmap = {}
        for r in self._all("SELECT date(ts,'unixepoch','localtime') d, model, COUNT(*) n "
                           "FROM usage WHERE ts>=? GROUP BY d, model", (cutoff,)):
            fam = model_family(r["model"])
            day = dmap.setdefault(r["d"], {})
            day[fam] = day.get(fam, 0) + r["n"]
        return [dict(day=d, **fams) for d, fams in sorted(dmap.items())]

    def model_savings(self, days=30, short_output=600, trivial_output=120):
        """V11 Slice 1 what-if downgrade savings: for each non-sidechain usage row, reprice its stored token
        counts under a cheaper model per policy and sum the positive difference vs its actual model (BOTH priced
        via costing.py — a pure rate-arbitrage estimate, never a quality judgment). Returns per-policy
        {saved_usd, turns} + considered count. `estimate: True` is asserted so the UI can't present it as fact."""
        conds, args = ["COALESCE(is_sidechain,0)=0"], []
        if days:
            conds.append("ts>=?"); args.append(time.time() - days * 86400)
        pol = {"all_opus_sonnet": {"label": "All Opus/Fable → Sonnet", "saved": 0.0, "turns": 0},
               "short_opus_sonnet": {"label": "Short Opus/Fable turns → Sonnet", "saved": 0.0, "turns": 0},
               "trivial_haiku": {"label": "Trivial turns → Haiku", "saved": 0.0, "turns": 0}}
        considered = 0
        for r in self._all(
                "SELECT model, COALESCE(input_tokens,0) it, COALESCE(output_tokens,0) ot, "
                "COALESCE(cache_read,0) cr, COALESCE(cache_creation,0) cc "
                "FROM usage WHERE " + " AND ".join(conds), tuple(args)):
            considered += 1
            fam = model_family(r["model"])
            toks = dict(input_tokens=r["it"], output_tokens=r["ot"], cache_read=r["cr"], cache_creation=r["cc"])
            actual = costing.reprice(r["model"], **toks)
            if fam in _PREMIUM:   # Opus / Fable / Mythos — all pricier than Sonnet
                s = actual - costing.reprice("claude-sonnet-4-6", **toks)
                if s > 0:
                    pol["all_opus_sonnet"]["saved"] += s; pol["all_opus_sonnet"]["turns"] += 1
                    if r["ot"] < short_output:
                        pol["short_opus_sonnet"]["saved"] += s; pol["short_opus_sonnet"]["turns"] += 1
            if fam in _ABOVE_HAIKU and r["ot"] < trivial_output:
                s = actual - costing.reprice("claude-haiku-4-5", **toks)
                if s > 0:
                    pol["trivial_haiku"]["saved"] += s; pol["trivial_haiku"]["turns"] += 1
        policies = [{"id": k, "label": v["label"], "saved_usd": round(v["saved"], 2), "turns": v["turns"]}
                    for k, v in pol.items()]
        return {"policies": policies, "considered": considered, "days": days, "estimate": True}

    def model_savings_breakdown(self, days=30, short_output=600, top=5):
        """WHERE the 'short Opus/Fable → Sonnet' opportunity (model_savings' defensible low-output proxy)
        concentrates — per PROJECT and per SESSION — so coaching can point at a specific place ("default this
        project to Sonnet") instead of one fleet total. Same rate-arbitrage estimate; content-free; estimate:True."""
        conds, args = ["COALESCE(is_sidechain,0)=0"], []
        if days:
            conds.append("ts>=?"); args.append(time.time() - days * 86400)
        proj, sess = {}, {}
        for r in self._all(
                "SELECT project, session_id, COALESCE(ts,0) ts, model, COALESCE(input_tokens,0) it, "
                "COALESCE(output_tokens,0) ot, COALESCE(cache_read,0) cr, COALESCE(cache_creation,0) cc "
                "FROM usage WHERE " + " AND ".join(conds), tuple(args)):
            if model_family(r["model"]) not in _PREMIUM or r["ot"] >= short_output:
                continue
            toks = dict(input_tokens=r["it"], output_tokens=r["ot"], cache_read=r["cr"], cache_creation=r["cc"])
            s = costing.reprice(r["model"], **toks) - costing.reprice("claude-sonnet-4-6", **toks)
            if s <= 0:
                continue
            p = r["project"] or "(unknown)"
            d = proj.setdefault(p, {"saved": 0.0, "turns": 0}); d["saved"] += s; d["turns"] += 1
            sid = r["session_id"] or "(unknown)"
            e = sess.setdefault(sid, {"saved": 0.0, "turns": 0, "project": p, "last_ts": 0.0})
            e["saved"] += s; e["turns"] += 1; e["last_ts"] = max(e["last_ts"], r["ts"])
        by_project = sorted(({"project": k, "saved_usd": round(v["saved"], 2), "turns": v["turns"]}
                             for k, v in proj.items()), key=lambda x: -x["saved_usd"])[:top]
        by_session = sorted(({"session_id": k, "project": v["project"], "saved_usd": round(v["saved"], 2),
                              "turns": v["turns"], "last_ts": v["last_ts"]}
                             for k, v in sess.items()), key=lambda x: -x["saved_usd"])[:top]
        return {"policy": "short_opus_sonnet", "by_project": by_project, "by_session": by_session,
                "days": days, "estimate": True}

    def reducible_spend(self, days=30):
        """V11 Slice 4 (speculative): spend that *may* be reducible — NOT a 'wasted' figure. The one defensible
        number is pure arithmetic: sum cost_usd of non-sidechain turns whose context_tokens sat in each dumb-zone
        band; the 🔴 Danger band (>200k) is output produced with a very full context, which is at higher risk of
        rework. Also reports the auto-compaction COUNT (a count, not an invented $ — we don't store summary cost)
        as a paired signal. Re-reads are NOT estimated (we don't track per-file reads). `estimate: True` and the
        'reducible'/'at-risk' framing are load-bearing: this is an opportunity, never a judgment of work quality."""
        conds, args = ["COALESCE(is_sidechain,0)=0"], []
        if days:
            conds.append("ts>=?"); args.append(time.time() - days * 86400)
        by_band = {b: 0.0 for b in ZONE_BANDS}
        considered = 0
        for r in self._all("SELECT COALESCE(context_tokens,0) c, COALESCE(cost_usd,0) cost "
                           "FROM usage WHERE " + " AND ".join(conds), tuple(args)):
            considered += 1
            by_band[zone_band(r["c"])] += r["cost"]
        total = sum(by_band.values())
        danger = by_band["danger"]
        ev = self.event_totals(days=days)
        return {
            "by_band": {b: round(v, 2) for b, v in by_band.items()},
            "total_usd": round(total, 2),
            "at_risk_usd": round(danger, 2),                              # lead with Danger only (conservative)
            "at_risk_pct": round(danger / total * 100, 1) if total else 0.0,
            "auto_compactions": ev.get("compaction_auto", 0),
            "considered": considered, "days": days, "estimate": True,
        }

    def record_rate_point(self, account, window, used_pct, resets_at, ts=None, min_gap=300):
        """Append a rate-limit reading for the Extra-Usage ETA. Throttled: skips if a point for this
        (account, window) exists within min_gap AND the value barely moved — keeps the series informative
        without bloat. Returns True if written."""
        if not account or window not in ("five_hour", "seven_day") or used_pct is None:
            return False
        ts = ts if ts is not None else time.time()
        last = self._one("SELECT ts, used_pct FROM rate_history WHERE account=? AND window=? "
                         "ORDER BY ts DESC LIMIT 1", (account, window))
        if last and (ts - last["ts"]) < min_gap and abs((used_pct or 0) - (last["used_pct"] or 0)) < 1:
            return False
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO rate_history (account, window, ts, used_pct, resets_at) "
                             "VALUES (?,?,?,?,?)", (account, window, ts, used_pct, resets_at))
            self._db.commit()
        return True

    def accounts_with_rate_history(self):
        return [r["account"] for r in self._all("SELECT DISTINCT account FROM rate_history WHERE account IS NOT NULL")]

    def extra_usage_eta(self, account, window, now=None):
        """Burn-based ETA until this (account, window) used_pct reaches 100% — i.e. crosses into Extra
        Usage (overage) / the plan cap. Uses the slope over RATE_LOOKBACK, restarted after the most recent
        window reset (a used_pct drop or a changed resets_at). Returns None if not climbing, too little
        history, or the window resets BEFORE 100% would be hit (no overage this cycle)."""
        now = now if now is not None else time.time()
        rows = self._all("SELECT ts, used_pct, resets_at FROM rate_history WHERE account=? AND window=? "
                         "AND ts>=? ORDER BY ts", (account, window, now - RATE_LOOKBACK))
        pts = [(r["ts"], r["used_pct"], r["resets_at"]) for r in rows
               if r["ts"] is not None and r["used_pct"] is not None]
        if len(pts) < 2:
            return None
        start = 0                                  # restart the slope after the most recent reset
        for i in range(1, len(pts)):
            dropped = pts[i][1] < pts[i - 1][1] - 1
            rolled = pts[i][2] and pts[i - 1][2] and pts[i][2] != pts[i - 1][2]
            if dropped or rolled:
                start = i
        seg = pts[start:]
        if len(seg) < 2:
            return None
        (t0, p0, _), (t1, p1, reset) = seg[0], seg[-1]
        dt = t1 - t0
        if dt <= 0 or p1 <= p0:
            return None                            # flat or falling → not burning toward the cap
        slope = (p1 - p0) / dt                      # %/sec
        if p1 >= 100:
            return {"eta_secs": 0, "slope_pct_per_hr": round(slope * 3600, 2)}
        eta = (100 - p1) / slope
        if reset and (reset - now) < eta:
            return None                            # window resets first → won't reach Extra Usage this cycle
        return {"eta_secs": round(eta), "slope_pct_per_hr": round(slope * 3600, 2)}

    def _overcap_intervals(self, account, now):
        """Time intervals where `account` was AT/OVER its plan cap (used_pct >= 100 on EITHER window) — i.e.
        in Extra Usage overage. Stepped model: a reading holds until the next one; an open over-cap run
        extends to `now`. Returns merged [(start, end)]."""
        ivs = []
        for window in ("five_hour", "seven_day"):
            pts = [(r["ts"], r["used_pct"]) for r in self._all(
                "SELECT ts, used_pct FROM rate_history WHERE account=? AND window=? ORDER BY ts",
                (account, window)) if r["ts"] is not None and r["used_pct"] is not None]
            i = 0
            while i < len(pts):
                if pts[i][1] >= 100:
                    j = i + 1
                    while j < len(pts) and pts[j][1] >= 100:
                        j += 1
                    ivs.append((pts[i][0], pts[j][0] if j < len(pts) else now))
                    i = j
                else:
                    i += 1
        return _merge_intervals(ivs)

    def overage_by_account(self, accounts, period="today", now=None):
        """Approximate Extra-Usage overage $ per account: cost of usage rows whose timestamp falls inside an
        over-cap interval. Pass the set of EXTRA-USAGE-ENABLED accounts (overage only bills when enabled; a
        capped-but-not-enabled account is blocked, not billed). period='today' (local day) or 'total'."""
        now = now if now is not None else time.time()
        day = " AND date(ts,'unixepoch','localtime')=date('now','localtime')" if period == "today" else ""
        out = {}
        for acct in accounts or ():
            total = 0.0
            for s, e in self._overcap_intervals(acct, now):
                r = self._one(f"SELECT COALESCE(SUM(cost_usd),0) c FROM usage WHERE account=? AND ts>=? AND ts<?{day}",
                              (acct, s, e))
                total += r["c"] or 0.0
            if total > 0:
                out[acct] = round(total, 4)
        return out

    def cost_by_account(self, period="today"):
        """Per-account cost split. period='today' (local day), 'month' (last 30d, for V11 ROI), or 'total'.
        NULL account → '(unattributed)' (pre-account historical rows backfill didn't claim). Sorted by spend desc."""
        where, args = "", ()
        if period == "today":
            where = "WHERE date(ts,'unixepoch','localtime')=date('now','localtime')"
        elif period == "month":
            where, args = "WHERE ts>=?", (time.time() - 30 * 86400,)
        return [{"account": r["a"] or "(unattributed)", "cost_usd": round(r["c"], 4), "messages": r["n"]}
                for r in self._all(
                    "SELECT account a, COALESCE(SUM(cost_usd),0) c, COUNT(*) n "
                    f"FROM usage {where} GROUP BY account ORDER BY c DESC", args)]

    def get_plan_prices(self):
        """Per-account monthly plan price (V11 ROI), stored as one JSON meta blob {account: price}."""
        try:
            return json.loads(self.get_meta("plan_prices") or "{}")
        except (ValueError, TypeError):
            return {}

    def set_plan_price(self, account, price):
        """Set/clear an account's monthly plan price (V11 ROI). price None/≤0 removes it. Returns True on success."""
        if not account:
            return False
        try:
            p = float(price) if price is not None else 0.0
        except (ValueError, TypeError):
            return False
        prices = self.get_plan_prices()
        if p > 0:
            prices[account] = round(p, 2)
        else:
            prices.pop(account, None)
        self.set_meta("plan_prices", json.dumps(prices))
        return True

    def cap_usage(self, account, days=30, hit_pct=99):
        """V11 Slice 3: per-window plan-cap usage from rate_history for one account over a trailing window.
        Returns {window: {avg, peak, cap_hits, samples}} where cap_hits = # distinct reset-windows whose peak
        reached >= hit_pct ('times you hit the wall'). Only accounts with rate history (wired hosts) → else {}."""
        cutoff = time.time() - days * 86400
        out = {}
        for w in ("five_hour", "seven_day"):
            rows = self._all("SELECT used_pct, resets_at FROM rate_history WHERE account=? AND window=? "
                             "AND ts>=? AND used_pct IS NOT NULL", (account, w, cutoff))
            if not rows:
                continue
            pcts = [r["used_pct"] for r in rows]
            bywin = {}                                   # peak used_pct per reset window → count the maxed ones
            for r in rows:
                bywin[r["resets_at"]] = max(bywin.get(r["resets_at"], 0), r["used_pct"])
            out[w] = {"avg": round(sum(pcts) / len(pcts), 1), "peak": round(max(pcts), 1),
                      "cap_hits": sum(1 for v in bywin.values() if v >= hit_pct), "samples": len(pcts)}
        return out

    def block_burn(self, now=None):
        """Current ccusage '5h block' burn across all sessions. tokens/min + $/hr exclude cache
        tokens (they dominate and inflate the rate). Returns None if no recent activity."""
        now = now or time.time()
        rows = self._all("SELECT ts, (input_tokens+output_tokens) nc, cost_usd FROM usage "
                         "WHERE ts>=? ORDER BY ts", (now - 24 * 3600,))
        if not rows:
            return None
        cur = None
        for r in rows:
            ts = r["ts"] or 0
            if cur is None or ts - cur["start"] > SESSION_GAP or ts - cur["last"] > SESSION_GAP:
                cur = {"start": ts, "first": ts, "last": ts, "tokens": 0, "cost": 0.0}
            cur["last"] = ts
            cur["tokens"] += r["nc"] or 0
            cur["cost"] += r["cost_usd"] or 0.0
        if cur is None or now - cur["last"] > SESSION_GAP:
            return None  # the latest block is stale → not actively burning
        elapsed_min = max(1.0, (cur["last"] - cur["first"]) / 60.0)
        cost_per_hour = cur["cost"] / elapsed_min * 60.0
        return {
            "block_start": cur["start"],
            "tokens": cur["tokens"],
            "cost_usd": round(cur["cost"], 4),
            "tokens_per_min": round(cur["tokens"] / elapsed_min, 1),
            "cost_per_hour": round(cost_per_hour, 4),
            "projected_block_cost": round(cost_per_hour * 5.0, 4),   # if the 5h block keeps this pace
        }

    def compact_eta(self, session_id, current_context, compact_at, lookback=1800):
        """Estimate seconds until this session's context reaches the compact threshold, from the
        recent slope of context_tokens. None if not enough history / not growing (e.g. just compacted)."""
        now = time.time()
        rows = self._all("SELECT ts, context_tokens FROM usage WHERE session_id=? AND ts>=? "
                         "AND COALESCE(is_sidechain,0)=0 ORDER BY ts",   # main thread only — subagent context is separate
                         (session_id, now - lookback))
        pts = [(r["ts"], r["context_tokens"]) for r in rows if r["ts"] and r["context_tokens"]]
        if len(pts) < 2:
            return None
        (t0, c0), (t1, c1) = pts[0], pts[-1]
        dt = t1 - t0
        if dt <= 0 or c1 <= c0:
            return None  # flat or dropped (compaction reset) → no meaningful ETA
        slope = (c1 - c0) / dt                      # tokens/sec
        remaining = compact_at - current_context
        if remaining <= 0:
            return 0
        return {"eta_secs": round(remaining / slope), "tokens_per_min": round(slope * 60, 1)}

    def session_context_series(self, session_id, limit=240):
        """Per-session context-token trajectory: [{ts, ctx}] oldest→newest, for the drill-in sparkline.
        Main thread only (subagent/sidechain context is separate). Capped to the most recent `limit` points."""
        if not session_id:
            return []
        rows = self._all(
            "SELECT ts, context_tokens FROM usage WHERE session_id=? AND COALESCE(is_sidechain,0)=0 "
            "AND ts IS NOT NULL AND context_tokens IS NOT NULL ORDER BY ts", (session_id,))
        pts = [{"ts": r["ts"], "ctx": r["context_tokens"]} for r in rows]
        return pts[-limit:] if limit and len(pts) > limit else pts

    def session_autopsy(self, session_id):
        """V8 Session Autopsy — a content-free 'report card' for one session, assembled entirely from existing
        helpers + per-session aggregate queries over the usage/events tables. NEVER reads conversation text:
        tokens, counts, timestamps, model names, and event types only (screenshot-safe, real-billed — not an
        estimate). Returns None if the session has no usage rows. Mirrors token_economics/zone_time math but
        scoped to one session_id."""
        if not session_id:
            return None
        base = self.session_total(session_id)                      # {cost_usd, tokens, messages}
        if not base["messages"]:
            return None
        span = self._one("SELECT MIN(ts) a, MAX(ts) b FROM usage WHERE session_id=?", (session_id,))
        start, end = span["a"], span["b"]
        econ = self._one(
            "SELECT COALESCE(SUM(cache_read),0) cr, COALESCE(SUM(input_tokens),0) it, "
            "COALESCE(SUM(cache_creation),0) cc, COALESCE(SUM(output_tokens),0) ot "
            "FROM usage WHERE session_id=?", (session_id,))
        denom = (econ["cr"] + econ["it"] + econ["cc"]) or 0
        cache_ratio = round(econ["cr"] / denom, 4) if denom else None
        mix = {m["model"]: m["n"] for m in self._all(
            "SELECT model, COUNT(*) n FROM usage WHERE session_id=? GROUP BY model", (session_id,)) if m["model"]}
        series = self.session_context_series(session_id)           # [{ts, ctx}] main-thread trajectory
        z = {b: 0 for b in ZONE_BANDS}
        for p in series:
            if p["ctx"]:
                z[zone_band(p["ctx"])] += 1
        ztot = sum(z.values())
        z["total"] = ztot
        z["healthy_pct"] = round((z["sharp"] + z["good"]) / ztot * 100, 1) if ztot else None
        ev = self.event_counts(session_id)
        return {
            "session_id": session_id,
            "account": self.account_for_session(session_id),
            "cost_usd": round(base["cost_usd"], 4),
            "tokens": base["tokens"],
            "output_tokens": econ["ot"],
            "messages": base["messages"],
            "started_at": start, "ended_at": end,
            "duration_secs": (end - start) if (start and end) else None,
            "cache_ratio": cache_ratio,
            "model_mix": mix,
            "prefix_overhead": self.session_prefix_overhead(session_id),
            "zone_series": series,
            "zone_time": z,
            "events": {k: ev.get(k, 0) for k in
                       ("compaction_manual", "compaction_auto", "skill_use", "subagent_spawn", "tool_error")},
            "estimate": False,
        }

    def recent_sessions(self, limit=12):
        """Most-recently-active sessions for the Autopsy picker: [{session_id, account, project, host, ts,
        messages}] newest first. Raw values (the client masks project/host/account into codenames)."""
        rows = self._all(
            "SELECT session_id, MAX(ts) ts, COUNT(*) n, "
            "MAX(account) account, MAX(project) project, MAX(host) host "
            "FROM usage WHERE session_id IS NOT NULL GROUP BY session_id ORDER BY ts DESC LIMIT ?", (limit,))
        return [{"session_id": r["session_id"], "account": r["account"], "project": r["project"],
                 "host": r["host"], "ts": r["ts"], "messages": r["n"]} for r in rows]

    # ---- events (Phase 6) ----
    def record_events(self, rows):
        """INSERT OR IGNORE a batch of event row dicts (idempotent on event_id). Fills account from the
        session_account map when absent. Returns count newly inserted."""
        rows = [r for r in rows if r and r.get("event_id")]
        if not rows:
            return 0
        for r in rows:
            if not r.get("account") and r.get("session_id"):
                r["account"] = self.account_for_session(r["session_id"])
        sql = f"INSERT OR IGNORE INTO events ({','.join(_EVENT_COLS)}) VALUES ({','.join('?' for _ in _EVENT_COLS)})"
        with self._lock:
            before = self._db.total_changes
            self._db.executemany(sql, [[r.get(c) for c in _EVENT_COLS] for r in rows])
            self._db.commit()
            n = self._db.total_changes - before
        if n:
            self._bump_agg()
        return n

    def ingest_event_lines(self, lines, host=None):
        """Parse transcript JSONL lines → event rows → store. Returns count inserted."""
        rows = []
        for ln in lines:
            try:
                rows.extend(extract_events(json.loads(ln), host=host))
            except ValueError:
                continue
        return self.record_events(rows)

    def event_counts(self, session_id):
        """{type: count} for a session — e.g. {compaction:1, skill_use:2, subagent_spawn:3, tool_error:5}.
        compaction is split into compaction_manual / compaction_auto by the payload trigger."""
        if not session_id:
            return {}
        out = {}
        for r in self._all("SELECT type, COUNT(*) n FROM events WHERE session_id=? GROUP BY type", (session_id,)):
            out[r["type"]] = r["n"]
        for r in self._all("SELECT json_extract(payload_json,'$.trigger') trig, COUNT(*) n FROM events "
                           "WHERE session_id=? AND type='compaction' GROUP BY trig", (session_id,)):
            if r["trig"]:
                out["compaction_" + r["trig"]] = r["n"]
        return out

    def events_count(self):
        r = self._one("SELECT COUNT(*) n FROM events")
        return r["n"] if r else 0

    def event_totals(self, days=None, account=None):
        """Event counts {type: n} (all-time, or the last `days`; fleet-wide, or one `account`), with compaction
        split into compaction_manual / compaction_auto — the substrate for the History "Craft signals" card +
        the Craft Score."""
        conds, args = [], []
        if days:
            conds.append("ts>=?"); args.append(time.time() - days * 86400)
        if account:
            conds.append("account=?"); args.append(account)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        base = "FROM events" + where
        args = tuple(args)
        out = {}
        for r in self._all(f"SELECT type, COUNT(*) n {base} GROUP BY type", args):
            out[r["type"]] = r["n"]
        comp = base + (" AND" if conds else " WHERE") + " type='compaction'"
        for r in self._all(f"SELECT json_extract(payload_json,'$.trigger') trig, COUNT(*) n {comp} GROUP BY trig", args):
            if r["trig"]:
                out["compaction_" + r["trig"]] = r["n"]
        return out

    @_cached_agg()
    def zone_time(self, account=None, days=None):
        """Distribution of main-thread context samples across the dumb-zone bands (same absolute-token
        thresholds as the UI). Each usage row's context_tokens is one sample (~proxy for time-in-zone, since
        messages are roughly paced). Returns per-zone counts + healthy_pct (% of samples below the drift
        onset, i.e. Sharp+Good). Subagent/sidechain rows excluded (their context is a separate thread)."""
        conds = ["COALESCE(is_sidechain,0)=0", "context_tokens IS NOT NULL", "context_tokens>0"]
        args = []
        if account:
            conds.append("account=?"); args.append(account)
        if days:
            conds.append("ts>=?"); args.append(time.time() - days * 86400)
        sql = "SELECT context_tokens c FROM usage WHERE " + " AND ".join(conds)
        z = {b: 0 for b in ZONE_BANDS}
        for r in self._all(sql, tuple(args)):
            z[zone_band(r["c"])] += 1
        total = sum(z.values())
        z["total"] = total
        z["healthy_pct"] = round((z["sharp"] + z["good"]) / total * 100, 1) if total else None
        return z

    @_cached_agg()
    def token_economics(self, account=None, days=None):
        """Token-efficiency aggregates for the Craft Score Efficiency dim (fleet-wide or one account).
        cache_ratio = cache_read / (cache_read + input + cache_creation) — the fraction of input tokens
        served from cache (high = good prompt/context structure; genuinely hard to fake). Also returns the
        model mix (message counts per model) as an informational signal (NOT scored — right-sizing is
        unobservable without task difficulty)."""
        conds, args = [], []
        if account:
            conds.append("account=?"); args.append(account)
        if days:
            conds.append("ts>=?"); args.append(time.time() - days * 86400)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        args = tuple(args)
        r = self._one("SELECT COALESCE(SUM(cache_read),0) cr, COALESCE(SUM(input_tokens),0) it, "
                      "COALESCE(SUM(cache_creation),0) cc, COALESCE(SUM(output_tokens),0) ot, COUNT(*) n "
                      f"FROM usage{where}", args)
        denom = (r["cr"] + r["it"] + r["cc"]) or 0
        cache_ratio = round(r["cr"] / denom, 4) if denom else None
        mix = {m["model"]: m["n"] for m in
               self._all(f"SELECT model, COUNT(*) n FROM usage{where} GROUP BY model", args) if m["model"]}
        return {"cache_ratio": cache_ratio, "messages": r["n"], "model_mix": mix}

    def craft_accounts(self):
        """Accounts that have any usage rows — for the Craft scene's per-account picker."""
        return [r["a"] for r in self._all(
            "SELECT DISTINCT account a FROM usage WHERE account IS NOT NULL ORDER BY a")]

    @_cached_agg()
    def daily_craft_aggregates(self, account=None, days=60, min_msgs=50):
        """Per local calendar day, the rate-normalized aggregate bundle (same keys as the server's
        _craft_aggregates) over THAT day's usage + events — the substrate for the Track 2b score-over-time
        series + personal best. Computed on demand from retained raw data (no snapshot table). Days below
        `min_msgs` are omitted (too little to score honestly). Returns {day: agg} (day = 'YYYY-MM-DD' local)."""
        cutoff = time.time() - days * 86400
        uargs, uacc = [cutoff], ""
        if account:
            uacc = " AND account=?"; uargs.append(account)
        dmap = {}

        def slot(d):
            return dmap.setdefault(d, {"cache_read": 0, "input": 0, "cache_creation": 0, "messages": 0,
                                       "sharp": 0, "good": 0, "drift": 0, "danger": 0,
                                       "compaction_manual": 0, "compaction_auto": 0, "tool_errors": 0,
                                       "skill_use": 0, "subagent_spawn": 0})
        for r in self._all(
                f"SELECT date(ts,'unixepoch','localtime') d, COALESCE(is_sidechain,0) sc, COALESCE(cache_read,0) cr, "
                f"COALESCE(input_tokens,0) it, COALESCE(cache_creation,0) cc, context_tokens ctx "
                f"FROM usage WHERE ts>=?{uacc}", tuple(uargs)):
            s = slot(r["d"])
            s["messages"] += 1
            s["cache_read"] += r["cr"]; s["input"] += r["it"]; s["cache_creation"] += r["cc"]
            if not r["sc"] and r["ctx"] is not None and r["ctx"] > 0:
                s[zone_band(r["ctx"])] += 1
        eargs, eacc = [cutoff], ""
        if account:
            eacc = " AND account=?"; eargs.append(account)
        for r in self._all(
                f"SELECT date(ts,'unixepoch','localtime') d, type, json_extract(payload_json,'$.trigger') trig "
                f"FROM events WHERE ts>=?{eacc}", tuple(eargs)):
            s = dmap.get(r["d"])
            if not s:                      # events on a day with no usage → can't score that day; skip
                continue
            t = r["type"]
            if t == "tool_error":
                s["tool_errors"] += 1
            elif t == "skill_use":
                s["skill_use"] += 1
            elif t == "subagent_spawn":
                s["subagent_spawn"] += 1
            elif t == "compaction" and r["trig"] in ("manual", "auto"):
                s["compaction_" + r["trig"]] += 1
        out = {}
        for d, s in dmap.items():
            if s["messages"] < min_msgs:
                continue
            denom = s["cache_read"] + s["input"] + s["cache_creation"]
            zt = s["sharp"] + s["good"] + s["drift"] + s["danger"]
            out[d] = {
                "cache_ratio": round(s["cache_read"] / denom, 4) if denom else None,
                "messages": s["messages"],
                "compaction_manual": s["compaction_manual"], "compaction_auto": s["compaction_auto"],
                "healthy_pct": round((s["sharp"] + s["good"]) / zt * 100, 1) if zt else None,
                "tool_errors": s["tool_errors"], "skill_use": s["skill_use"], "subagent_spawn": s["subagent_spawn"],
            }
        return out

    def _drift_bands(self, errors_by_band, msgs_by_band, knee_factor):
        """Assemble one model's (or the overall) drift curve from per-band error + message counts: the
        tool-error RATE per band, plus the 'knee' — the first band whose rate is >= knee_factor x the
        baseline (lowest band with data) rate. Returns None if there's no message data."""
        bands, total_msgs, total_err = [], 0, 0
        for b in ZONE_BANDS:
            m, e = msgs_by_band.get(b, 0), errors_by_band.get(b, 0)
            total_msgs += m
            total_err += e
            bands.append({"band": b, "floor": ZONE_BAND_FLOOR[b], "messages": m, "errors": e,
                          "rate": round(e / m, 4) if m else None})
        if not total_msgs:
            return None
        rated = [x for x in bands if x["rate"] is not None]
        baseline = rated[0]["rate"] if rated else None
        knee = None
        if baseline is not None:
            for x in rated[1:]:
                if x["rate"] >= max(baseline, 1e-9) * knee_factor and x["rate"] > baseline:
                    knee = x
                    break
        return {"bands": bands, "messages": total_msgs, "errors": total_err,
                "baseline_rate": baseline,
                "knee_band": knee["band"] if knee else None,
                "knee_tokens": knee["floor"] if knee else None}

    def drift_curve(self, account=None, days=None, knee_factor=1.5, min_model_msgs=300, min_model_errors=8):
        """Data-derived dumb-zone v2: tool-error RATE as a function of accumulated context — overall and
        per model. For each tool_error we look up the context_tokens + model of the nearest PRECEDING
        main-thread message (the context level when the error fired), bucket it into the dumb-zone bands, and
        divide by the messages in that band to get a rate. The 'knee' is the first band where the rate jumps
        to >= knee_factor x the lowest-band baseline — i.e. where degradation measurably kicks in for THIS
        user/model. CORRELATIONAL, not causal (hard tasks both fill context AND cause errors) → an estimate.
        Per-model curves are only returned for models with enough data (min_model_msgs / min_model_errors)."""
        ucond = ["COALESCE(is_sidechain,0)=0", "context_tokens IS NOT NULL", "context_tokens>0"]
        uargs = []
        if account:
            ucond.append("account=?"); uargs.append(account)
        if days:
            ucond.append("ts>=?"); uargs.append(time.time() - days * 86400)
        uwhere = " AND ".join(ucond)
        # denominators: messages per (model, band)
        msgs = {}                       # model -> {band: n};  '*' = overall
        for r in self._all(f"SELECT model, context_tokens c FROM usage WHERE {uwhere}", tuple(uargs)):
            b = zone_band(r["c"])
            for key in ("*", r["model"] or "(unknown)"):
                msgs.setdefault(key, {}).setdefault(b, 0)
                msgs[key][b] += 1
        # numerators: each tool_error → context+model of the nearest preceding main-thread message
        econd = ["e.type='tool_error'"]
        eargs = []
        if account:
            econd.append("e.account=?"); eargs.append(account)
        if days:
            econd.append("e.ts>=?"); eargs.append(time.time() - days * 86400)
        ewhere = " AND ".join(econd)
        errs = {}
        sql = (f"SELECT u.model model, u.context_tokens c FROM events e "
               f"JOIN usage u ON u.rowid = (SELECT u2.rowid FROM usage u2 "
               f"  WHERE u2.session_id=e.session_id AND u2.ts<=e.ts AND COALESCE(u2.is_sidechain,0)=0 "
               f"    AND u2.context_tokens IS NOT NULL ORDER BY u2.ts DESC LIMIT 1) "
               f"WHERE {ewhere}")
        for r in self._all(sql, tuple(eargs)):
            if r["c"] is None:
                continue
            b = zone_band(r["c"])
            for key in ("*", r["model"] or "(unknown)"):
                errs.setdefault(key, {}).setdefault(b, 0)
                errs[key][b] += 1
        overall = self._drift_bands(errs.get("*", {}), msgs.get("*", {}), knee_factor)
        by_model = {}
        for model, mb in msgs.items():
            if model == "*":
                continue
            mtot, etot = sum(mb.values()), sum(errs.get(model, {}).values())
            if mtot >= min_model_msgs and etot >= min_model_errors:
                by_model[model] = self._drift_bands(errs.get(model, {}), mb, knee_factor)
        return {"overall": overall, "by_model": by_model, "knee_factor": knee_factor}

    def record_mcp_report(self, host, servers, ts=None):
        """MCP tax Slice 2: store a host's latest MCP-server loadout from the self-serve probe (upsert by host).
        `servers` is [{name, status, ...}] — names + status ONLY; the probe strips every launch command/URL/env
        before it ever leaves the host, so nothing secret reaches here. Returns the number of servers stored."""
        if not host:
            return 0
        clean = [s for s in (servers or []) if isinstance(s, dict) and s.get("name")]
        ts = ts if ts is not None else time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO mcp_reports(host,ts,servers_json) VALUES(?,?,?) "
                "ON CONFLICT(host) DO UPDATE SET ts=excluded.ts, servers_json=excluded.servers_json",
                (host, ts, json.dumps(clean)))
            self._db.commit()
        return len(clean)

    def mcp_servers_used(self, days=30):
        """MCP tax over-time usage from `mcp_call` events → {host: {server: {calls, last_ts}}} over the window.
        Crossed with mcp_reports() this gives TRUE 'never called in N days' dead-weight (vs. the live-session-only
        scope of Slice 2). Events carry the host that ingested them (the collector's local transcripts)."""
        cutoff = time.time() - days * 86400 if days else 0
        out = {}
        for r in self._all(
                "SELECT host, json_extract(payload_json,'$.server') srv, COUNT(*) n, MAX(ts) m "
                "FROM events WHERE type='mcp_call' AND ts>=? GROUP BY host, srv", (cutoff,)):
            if not r["srv"]:
                continue
            out.setdefault(r["host"], {})[r["srv"]] = {"calls": r["n"], "last_ts": r["m"]}
        return out

    def mcp_reports(self):
        """All hosts' latest MCP reports → {host: {ts, servers:[{name,status,...}]}}."""
        out = {}
        for r in self._all("SELECT host, ts, servers_json FROM mcp_reports"):
            try:
                servers = json.loads(r["servers_json"]) or []
            except (ValueError, TypeError):
                servers = []
            out[r["host"]] = {"ts": r["ts"], "servers": servers}
        return out

    def get_meta(self, key):
        r = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else None

    def set_meta(self, key, value):
        with self._lock:
            self._db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, str(value)))
            self._db.commit()

    def events_for_session(self, session_id, limit=100):
        """Recent events for a session, newest first: [{ts, type, ref, payload}]."""
        rows = self._all("SELECT ts, type, ref, payload_json FROM events WHERE session_id=? "
                         "ORDER BY ts DESC LIMIT ?", (session_id, limit))
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
            except ValueError:
                payload = {}
            out.append({"ts": r["ts"], "type": r["type"], "ref": r["ref"], "payload": payload})
        return out

    # ---- import (V13 Slice 3) ----
    def merge_rows(self, table, rows, dry=False):
        """INSERT OR IGNORE a list of column-named row dicts into a WHITELISTED table (idempotent merge for
        import). Columns are validated against the live schema (injection-safe + schema-drift tolerant).
        Returns (added, skipped). dry=True rolls back but still reports the exact would-add count."""
        if table not in _IMPORT_TABLES or not rows:
            return (0, 0)
        with self._lock:
            valid = {r[1] for r in self._db.execute(f'PRAGMA table_info("{table}")')}
            before = self._db.total_changes
            for r in rows:
                cols = [c for c in r.keys() if c in valid]
                if not cols:
                    continue
                collist = ",".join('"' + c + '"' for c in cols)
                ph = ",".join("?" * len(cols))
                self._db.execute(f'INSERT OR IGNORE INTO "{table}" ({collist}) VALUES ({ph})',
                                 [r[c] for c in cols])
            added = self._db.total_changes - before     # total_changes counts only real inserts, survives rollback
            if dry:
                self._db.rollback()
            else:
                self._db.commit()
        if not dry and added:
            self._bump_agg()
        return (added, len(rows) - added)

    # ---- maintenance (V13 Slice 2) ----
    def checkpoint(self):
        """Truncate the WAL (passive auto-checkpoint never shrinks the -wal file). Lossless."""
        with self._lock:
            row = self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return tuple(row) if row else None

    def prune_rate_history(self, before_ts, dry=False):
        """Remove rate_history rows older than before_ts (the densest grower). Returns rows removed."""
        if dry:
            r = self._one("SELECT COUNT(*) c FROM rate_history WHERE ts < ?", (before_ts,))
            return r["c"] if r else 0
        with self._lock:
            cur = self._db.execute("DELETE FROM rate_history WHERE ts < ?", (before_ts,))
            self._db.commit()
            n = cur.rowcount
        self._bump_agg()
        return n

    def prune_events(self, before_ts, dry=False):
        """Remove behavioral events older than before_ts. Returns rows removed."""
        if dry:
            r = self._one("SELECT COUNT(*) c FROM events WHERE ts < ?", (before_ts,))
            return r["c"] if r else 0
        with self._lock:
            cur = self._db.execute("DELETE FROM events WHERE ts < ?", (before_ts,))
            self._db.commit()
            n = cur.rowcount
        self._bump_agg()
        return n

    def vacuum(self):
        """Reclaim free pages after a prune. Briefly exclusive; busy_timeout covers concurrent readers."""
        with self._lock:
            self._db.commit()
            self._db.execute("VACUUM")
        return os.path.getsize(self.path) if os.path.exists(self.path) else 0

    def close(self):
        with self._lock:
            self._db.close()


if __name__ == "__main__":
    import sys
    s = Store(sys.argv[1] if len(sys.argv) > 1 else ":memory:")
    print("schema ok; total cost so far:", s.total_cost())

"""Maintenance orchestration over the two SQLite DBs (V13 Slice 2).

Content-free: operates on file sizes, row counts, and timestamps only — it never reads message text. The
checkpoint pass is run automatically by the server's maintenance loop (lossless: it only truncates the WAL).
prune/vacuum are user-triggered with a dry-run preview first (server endpoints, Slice 3) — nothing is deleted
without a confirm.
"""
import os
import sqlite3
import time

# Per-DB table sets for stats. ts_tables = those with a `ts` column we report oldest/newest + projection from.
_USAGE_TABLES = ("usage", "events", "rate_history", "session_account", "session_prefs", "mcp_reports", "meta")
_USAGE_TS = ("usage", "events", "rate_history")
_CONTENT_TABLES = ("docs", "seen", "files", "embeddings", "embed_skip", "meta")
_CONTENT_TS = ("docs",)


def _file_sizes(path):
    """{main, wal, shm, total} byte sizes for a SQLite DB path (missing files → 0)."""
    out = {}
    for suffix, key in (("", "main"), ("-wal", "wal"), ("-shm", "shm")):
        try:
            out[key] = os.path.getsize(path + suffix)
        except OSError:
            out[key] = 0
    out["total"] = out["main"] + out["wal"] + out["shm"]
    return out


def _conn(path):
    """A short-lived read connection (normal mode — read-only+WAL has shm-write pitfalls; SELECT-only here)."""
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _db_block(path, tables, ts_tables):
    sizes = _file_sizes(path)
    rows, oldest, newest = {}, None, None
    if ":memory:" not in path and os.path.exists(path):
        c = _conn(path)
        try:
            for t in tables:
                try:
                    rows[t] = c.execute(f'SELECT COUNT(*) AS c FROM "{t}"').fetchone()["c"]
                except sqlite3.Error:
                    rows[t] = None
            for t in ts_tables:
                try:
                    r = c.execute(f'SELECT MIN(ts) AS mn, MAX(ts) AS mx FROM "{t}"').fetchone()
                    if r and r["mn"] is not None:
                        oldest = r["mn"] if oldest is None else min(oldest, r["mn"])
                        newest = r["mx"] if newest is None else max(newest, r["mx"])
                except sqlite3.Error:
                    pass
        finally:
            c.close()
    proj = None
    if oldest and newest and newest > oldest:
        days = (newest - oldest) / 86400.0
        if days >= 1:
            proj = int(sizes["main"] / days * 365)
    return {"sizes": sizes, "rows": rows, "oldest_ts": oldest, "newest_ts": newest,
            "projected_bytes_per_year": proj}


def db_files(store=None, content=None):
    """Just the on-disk sizes (cheap; no DB open)."""
    out = {}
    if store is not None:
        out["usage"] = _file_sizes(store.path)
    if content is not None:
        out["content"] = _file_sizes(content.path)
    return out


def db_stats(store=None, content=None):
    """Sizes + per-table row counts + oldest/newest data + a naive bytes/yr projection, per DB."""
    out = {}
    if store is not None:
        out["usage"] = _db_block(store.path, _USAGE_TABLES, _USAGE_TS)
    if content is not None:
        out["content"] = _db_block(content.path, _CONTENT_TABLES, _CONTENT_TS)
    return out


def checkpoint(store=None, content=None):
    """Truncate both WALs (lossless). Returns wal_before/after bytes per DB — the automatic loop calls this."""
    res = {}
    if store is not None:
        before = _file_sizes(store.path)["wal"]
        store.checkpoint()
        res["usage"] = {"wal_before": before, "wal_after": _file_sizes(store.path)["wal"]}
    if content is not None:
        before = _file_sizes(content.path)["wal"]
        content.checkpoint()
        res["content"] = {"wal_before": before, "wal_after": _file_sizes(content.path)["wal"]}
    return res


def prune(store, content, policy, live_paths=None, dry=True):
    """Apply (or preview, dry=True) a retention policy. policy keys (each optional; absent/None = skip):
      rate_history_days, events_days, content_days  → delete rows older than N days
      content_orphans (bool)                        → drop content for transcripts no longer on disk
    Returns {target: {...counts}}."""
    now = time.time()
    out = {}
    if store is not None:
        d = policy.get("rate_history_days")
        if d:
            out["rate_history"] = {"rows": store.prune_rate_history(now - d * 86400, dry=dry)}
        d = policy.get("events_days")
        if d:
            out["events"] = {"rows": store.prune_events(now - d * 86400, dry=dry)}
    if content is not None:
        d = policy.get("content_days")
        if d:
            out["content_docs"] = {"rows": content.prune_before(now - d * 86400, dry=dry)}
        if policy.get("content_orphans"):
            out["content_orphans"] = content.prune_orphans(live_paths or [], dry=dry)
    return out


def prune_preview(store, content, policy, live_paths=None):
    return prune(store, content, policy, live_paths, dry=True)


def prune_apply(store, content, policy, live_paths=None):
    return prune(store, content, policy, live_paths, dry=False)


def vacuum(store=None, content=None, which="both"):
    """VACUUM the chosen DB(s) to reclaim freed pages. Returns before/after main-file bytes."""
    out = {}
    if store is not None and which in ("both", "usage"):
        before = _file_sizes(store.path)["main"]
        store.vacuum()
        out["usage"] = {"before": before, "after": _file_sizes(store.path)["main"]}
    if content is not None and which in ("both", "content"):
        before = _file_sizes(content.path)["main"]
        content.vacuum()
        out["content"] = {"before": before, "after": _file_sizes(content.path)["main"]}
    return out

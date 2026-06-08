"""Portable, mergeable export/import of the content-free DB (V13 Slice 3).

The migrate-to-a-new-machine bundle. It's a LOGICAL dump (not a file copy) so import merges idempotently via
INSERT OR IGNORE — safe to combine multiple machines and to re-import (re-import adds 0). Content-free by
construction: every exported table holds tokens/timestamps/counts/flags, never message text.

On disk the bundle is gzipped JSON, then (by default) encrypted before writing — see crypto.py. The encrypted
form is what the user can safely put anywhere (Drive, USB, SyncThing); a plaintext export is opt-in + local-only.
"""
import gzip
import json
import os
import time

import crypto

FORMAT = "cc-observability/portable"
VERSION = 1
# Exactly the content-free, partly-non-rebuildable tables. (rate_history/session_account/session_prefs/
# mcp_reports/meta are NOT rebuildable from transcripts; usage/events are, but exporting them lets a new
# machine inherit history instantly without re-ingest.)
EXPORT_TABLES = ("usage", "events", "rate_history", "session_account", "session_prefs", "mcp_reports", "meta")


def export_bundle(store):
    """Build the in-memory bundle dict from a Store (content-free)."""
    tables, hosts = {}, set()
    for t in EXPORT_TABLES:
        tables[t] = [dict(r) for r in store._all(f'SELECT * FROM "{t}"')]
    for r in tables.get("usage", []):
        if r.get("host"):
            hosts.add(r["host"])
    return {"format": FORMAT, "version": VERSION,
            "schema": store.get_meta("schema_version"),
            "exported_at": time.time(), "hosts": sorted(hosts), "tables": tables}


def import_bundle(store, data, dry=False):
    """Merge a bundle into a Store via INSERT OR IGNORE (idempotent). Returns {table:{added,skipped}}."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("not a cc-observability portable bundle")
    out = {}
    for t in EXPORT_TABLES:
        rows = data.get("tables", {}).get(t) or []
        added, skipped = store.merge_rows(t, rows, dry=dry)
        out[t] = {"added": added, "skipped": skipped}
    return out


def _stamp(ts=None):
    return time.strftime("%Y%m%dT%H%M%S", time.localtime(ts or time.time()))


def write_export(store, export_dir, passphrase=None, encrypt=True, ts=None, prefix="cc-obs-export"):
    """Write a bundle to export_dir. Encrypted by default — only ciphertext touches disk (we gzip+encrypt the
    bytes in memory, then write). Returns {path, bytes, encrypted}."""
    os.makedirs(export_dir, exist_ok=True)
    raw = json.dumps(export_bundle(store), separators=(",", ":")).encode("utf-8")
    gz = gzip.compress(raw)
    if encrypt:
        if not passphrase:
            raise ValueError("passphrase required for an encrypted export")
        blob = crypto.encrypt(gz, passphrase)
        name = f"{prefix}-{_stamp(ts)}.json.gz.enc"
    else:
        blob, name = gz, f"{prefix}-{_stamp(ts)}.json.gz"
    path = os.path.join(export_dir, name)
    with open(path, "wb") as f:
        f.write(blob)
    return {"path": path, "bytes": len(blob), "encrypted": bool(encrypt)}


def load_export(blob, passphrase=None):
    """Bytes from an export file → the bundle dict. Decrypts (if encrypted) then gunzips."""
    if crypto.is_encrypted(blob):
        if not passphrase:
            raise ValueError("passphrase required to decrypt")
        blob = crypto.decrypt(blob, passphrase)
    try:
        raw = gzip.decompress(blob)
    except OSError:
        raw = blob   # tolerate a plain-JSON bundle
    return json.loads(raw.decode("utf-8"))


def latest_export(export_dir):
    """Path of the most recent export file (for the download link / last-export meta), or None."""
    try:
        files = [os.path.join(export_dir, f) for f in os.listdir(export_dir)
                 if f.startswith("cc-obs-export-")]
    except OSError:
        return None
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def pre_upgrade_backup(store, export_dir):
    """Safety net before a schema migration advances: write an UNENCRYPTED content-free bundle (same data
    already in the local DB; no passphrase available at startup). Best-effort — never blocks startup."""
    old = store.get_meta("schema_version") or "0"
    return write_export(store, export_dir, encrypt=False, prefix=f"pre-upgrade-v{old}")

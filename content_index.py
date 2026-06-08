"""content_index.py — V9 local transcript full-text search (SQLite FTS5).

THE ONE PLACE in this app that stores raw transcript CONTENT. Every other module is content-free
(token counts/timestamps only). Three load-bearing rules keep that contained:

  1. OPT-IN     — `index_pass()` is a no-op until `enable()` is called. Nothing is written to disk
                  until the user explicitly flips "Enable content search". `wipe()` deletes it all.
  2. ISOLATED   — its own DB file (cc-content.db), separate from the content-free cc-usage.db, so the
                  content trove is trivially deletable and never entangled with cost history.
  3. LOCAL-ONLY — enforced in server.py: search/content endpoints are served ONLY on the loopback
                  content port. This module never knows about the network; it just indexes & searches.

Scope = EVERYTHING incl. tool I/O: user text, assistant text, assistant thinking, tool_use input,
and tool_result output. That maximizes recall for post-compaction recovery — and is exactly why the
UI must carry a do-not-screenshot banner (file dumps & command output are searchable).
"""
import os
import re
import json
import time
import sqlite3
import hashlib
import threading
from datetime import datetime

import redact

DEFAULT_PATH = os.environ.get(
    "CC_CONTENT_DB_PATH",
    os.path.expanduser("~/.cache/cc-observability/cc-content.db"),
)

# snippet() highlight delimiters — control chars, NOT html, so the client can esc() the whole snippet
# (XSS-safe) and only THEN swap the sentinels for <mark>. See web/index.html renderSearch.
MARK_A = "\x02"
MARK_B = "\x03"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta  (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, offset INTEGER);
CREATE TABLE IF NOT EXISTS seen  (uuid TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS embeddings (uuid TEXT PRIMARY KEY, vec BLOB);
CREATE TABLE IF NOT EXISTS embed_skip (uuid TEXT PRIMARY KEY);
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    uuid UNINDEXED, session_id UNINDEXED, project UNINDEXED, host UNINDEXED,
    role UNINDEXED, kind UNINDEXED, ts UNINDEXED, text,
    tokenize='porter unicode61'
);
"""
# docs column order (0-based): uuid0 session_id1 project2 host3 role4 kind5 ts6 text7
_TEXT_COL = 7


# ---- schema migrations (forward-only, ordered) — mirrors store.py ----
def _mig_001_embeddings(db):
    """Baseline: ensure the embeddings + embed_skip tables exist (added after the original content schema)."""
    db.execute("CREATE TABLE IF NOT EXISTS embeddings (uuid TEXT PRIMARY KEY, vec BLOB)")
    db.execute("CREATE TABLE IF NOT EXISTS embed_skip (uuid TEXT PRIMARY KEY)")


_MIGRATIONS = [
    (1, _mig_001_embeddings),
]
_CODE_SCHEMA_VERSION = _MIGRATIONS[-1][0] if _MIGRATIONS else 0
# the searchable block kinds (used to validate the kind filter — never trust client strings into SQL)
_DOC_KINDS = ("user", "assistant", "thinking", "tool_use", "tool_result")

# Semantic search (V9 Slice 5) — local Ollama embeddings. Configured (blank model = semantic OFF), and
# always LOCAL: Ollama is the user's own model on their own host/LAN, so embedding content there is within
# the same trust boundary as the coach engine — no third-party egress. We embed CONVERSATION only (prose),
# since keyword FTS already covers tool I/O exactly. Vectors are normalized → cosine == dot product.
EMBED_MODEL = os.environ.get("CC_EMBED_MODEL", "").strip()
OLLAMA_URL = os.environ.get("CC_OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/")
EMBED_KINDS = ("user", "assistant", "thinking")
_EMBED_TIMEOUT = float(os.environ.get("CC_EMBED_TIMEOUT", "120"))
_EMBED_TEXT_CAP = int(os.environ.get("CC_EMBED_TEXT_CAP", "2000"))   # chars/block — well under nomic's ~2048-token ctx
_EMBED_FALLBACK_CAPS = (2000, 1000, 400)   # shrink-and-retry caps before skipping a pathological block


def parse_ts(iso):
    """ISO8601 → epoch seconds, or None."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _flatten(content):
    """tool_result / nested content (str | list of blocks) → flat text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if b.get("type") == "text" and b.get("text"):
                    parts.append(str(b["text"]))
                elif "content" in b:
                    parts.append(_flatten(b["content"]))
        return "\n".join(p for p in parts if p)
    return ""


def iter_docs_from_line(d, host=None, project=None):
    """A transcript JSONL entry dict → yield searchable doc dicts (one per text-bearing block).
    Scope is EVERYTHING: user prompts, assistant text + thinking, tool_use input, tool_result output."""
    typ = d.get("type")
    if typ not in ("user", "assistant"):
        return
    msg = d.get("message")
    if not isinstance(msg, dict):
        return
    uuid = d.get("uuid") or ""
    sid = d.get("sessionId")
    ts = parse_ts(d.get("timestamp"))
    if project is None and d.get("cwd"):
        project = os.path.basename(str(d["cwd"]))
    content = msg.get("content")

    def mk(idx, role, kind, text):
        text = (text or "").strip()
        if not text:
            return None
        u = uuid or hashlib.sha1(f"{sid}:{ts}:{kind}:{idx}".encode("utf-8", "replace")).hexdigest()
        return {"uuid": f"{u}:{idx}", "session_id": sid, "project": project, "host": host,
                "role": role, "kind": kind, "ts": ts, "text": text}

    if typ == "user":
        if isinstance(content, str):
            doc = mk(0, "user", "user", content)
            if doc:
                yield doc
        elif isinstance(content, list):
            for i, c in enumerate(content):
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_result":
                    doc = mk(i, "user", "tool_result", _flatten(c.get("content")))
                elif c.get("type") == "text":
                    doc = mk(i, "user", "user", c.get("text"))
                else:
                    doc = None
                if doc:
                    yield doc
        return

    # assistant
    if isinstance(content, str):
        doc = mk(0, "assistant", "assistant", content)
        if doc:
            yield doc
        return
    if isinstance(content, list):
        for i, c in enumerate(content):
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t == "text":
                doc = mk(i, "assistant", "assistant", c.get("text"))
            elif t == "thinking":
                doc = mk(i, "assistant", "thinking", c.get("thinking"))
            elif t == "tool_use":
                nm = c.get("name") or "tool"
                try:
                    body = json.dumps(c.get("input"), ensure_ascii=False)
                except (TypeError, ValueError):
                    body = str(c.get("input"))
                doc = mk(i, "assistant", "tool_use", f"{nm} {body}")
            else:
                doc = None
            if doc:
                yield doc


class ContentIndex:
    def __init__(self, path=DEFAULT_PATH, host=None):
        self.path = path
        self.host = host
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._lock = threading.Lock()
        self._emb_cache = None   # (uuids:list, matrix:np.ndarray) of normalized embeddings; rebuilt lazily
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
        holds self._lock and has bootstrapped the full schema via executescript(_SCHEMA)."""
        cur = self._schema_version()
        for ver, fn in _MIGRATIONS:
            if ver > cur:
                fn(self._db)
                self._db.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(ver),))
                cur = ver

    # ---- enable flag (the opt-in gate) ----
    def enabled(self):
        with self._lock:
            r = self._db.execute("SELECT value FROM meta WHERE key='enabled'").fetchone()
        return bool(r and r["value"] == "1")

    def _set_enabled(self, on):
        with self._lock:
            self._db.execute(
                "INSERT INTO meta(key,value) VALUES('enabled',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if on else "0",))
            self._db.commit()

    def enable(self):
        self._set_enabled(True)

    def disable(self):
        self._set_enabled(False)

    def set_meta(self, key, value):
        with self._lock:
            self._db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            self._db.commit()

    def _meta_float(self, key):
        with self._lock:
            r = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        try:
            return float(r["value"]) if r else None
        except (ValueError, TypeError):
            return None

    def get_meta(self, key):
        with self._lock:
            r = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    # ---- privacy config (V13 Slice 2B) ----
    def retention_days(self):
        """Auto-purge window for indexed content (default 14d; 0 = keep until deleted)."""
        try:
            v = self.get_meta("cfg.content_retention_days")
            return int(v) if v is not None else 14
        except (ValueError, TypeError):
            return 14

    def redact_on(self):
        """Best-effort secret redaction at index time (default OFF)."""
        return self.get_meta("cfg.content_redact") == "1"

    def scope(self):
        """'all' (conversation + tool I/O, default) or 'conversation' (exclude tool_result/tool_use)."""
        return self.get_meta("cfg.content_scope") or "all"

    def config(self):
        return {"retention_days": self.retention_days(), "redact": self.redact_on(), "scope": self.scope()}

    def set_config(self, retention_days=None, redact=None, scope=None):
        if retention_days is not None:
            try:
                self.set_meta("cfg.content_retention_days", max(0, int(retention_days)))
            except (ValueError, TypeError):
                pass
        if redact is not None:
            self.set_meta("cfg.content_redact", "1" if redact else "0")
        if scope is not None:
            self.set_meta("cfg.content_scope", "conversation" if scope == "conversation" else "all")
        return self.config()

    def apply_retention(self):
        """Auto-purge docs older than the configured window. No-op when disabled or window is 0. Returns
        docs removed. Called from the server maintenance loop — content is opt-in + rebuildable, so this
        bounded auto-delete is the user-chosen default (14d) that shrinks a leak's blast radius."""
        if not self.enabled():
            return 0
        days = self.retention_days()
        if days <= 0:
            return 0
        return self.prune_before(time.time() - days * 86400)

    # ---- indexing ----
    def _insert_docs(self, docs):
        """INSERT new docs, deduped on uuid via the seen table. Returns count newly indexed."""
        n = 0
        with self._lock:
            for doc in docs:
                cur = self._db.execute("INSERT OR IGNORE INTO seen(uuid) VALUES(?)", (doc["uuid"],))
                if cur.rowcount:  # newly seen → index it
                    self._db.execute(
                        "INSERT INTO docs(uuid,session_id,project,host,role,kind,ts,text) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (doc["uuid"], doc["session_id"], doc["project"], doc["host"],
                         doc["role"], doc["kind"], doc["ts"], doc["text"]))
                    n += 1
            self._db.commit()
        return n

    def index_pass(self, files, host=None):
        """Incrementally index new content from each transcript file (offset-tracked; JSONL is append-only).
        NO-OP unless enabled — so zero content reaches disk until the user opts in. Returns {files, docs}."""
        if not self.enabled():
            return {"files": 0, "docs": 0}
        host = host or self.host
        nfiles = 0
        ndocs = 0
        scope = self.scope()                 # 'conversation' drops tool I/O before it's ever written
        redact_on = self.redact_on()         # best-effort secret masking at index time (default off)
        for path in files:
            try:
                st = os.stat(path)
            except OSError:
                continue
            with self._lock:
                row = self._db.execute("SELECT offset FROM files WHERE path=?", (path,)).fetchone()
            prev = int(row["offset"]) if row else 0
            if prev > st.st_size:
                prev = 0  # file truncated/rotated → restart; the seen table dedups anything re-read
            if prev == st.st_size:
                continue  # already fully indexed
            try:
                with open(path, "rb") as f:
                    f.seek(prev)
                    data = f.read()
            except OSError:
                continue
            text = data.decode("utf-8", "replace")
            advance = len(data)
            if data and not text.endswith("\n"):
                nl = text.rfind("\n")
                if nl == -1:
                    continue  # one partial line, no complete record yet — wait for the rest
                advance = len(text[:nl + 1].encode("utf-8"))
                text = text[:nl + 1]
            docs = []
            for ln in text.split("\n"):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                docs.extend(iter_docs_from_line(d, host=host))
            if scope == "conversation":
                docs = [dd for dd in docs if dd["kind"] in ("user", "assistant", "thinking")]
            if redact_on:
                for dd in docs:
                    dd["text"] = redact.redact(dd["text"])
            if docs:
                ndocs += self._insert_docs(docs)
            with self._lock:
                self._db.execute(
                    "INSERT INTO files(path,mtime,size,offset) VALUES(?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime,size=excluded.size,offset=excluded.offset",
                    (path, st.st_mtime, st.st_size, prev + advance))
                self._db.commit()
            nfiles += 1
        if nfiles:
            self.set_meta("last_indexed_ts", time.time())
        return {"files": nfiles, "docs": ndocs}

    # ---- search ----
    @staticmethod
    def _fts_query(q):
        """User text → a safe FTS5 MATCH expression: each whitespace term as a quoted phrase, AND-joined.
        Quoting (and doubling internal quotes) neutralizes FTS5 operators so arbitrary input can't error."""
        terms = [t for t in re.split(r"\s+", (q or "").strip()) if t]
        return " ".join('"' + t.replace('"', '""') + '"' for t in terms)

    def search(self, q, limit=40, kinds=None, since_ts=None):
        """Ranked full-text search. Returns [{uuid, session_id, project, host, role, kind, ts, snippet}].
        snippet carries MARK_A/MARK_B sentinels around hits (client converts to <mark> after escaping).
        Optional filters (folded into the FTS WHERE): kinds=[…] restricts block kinds (user/assistant/
        thinking/tool_use/tool_result); since_ts keeps only blocks at/after that epoch second."""
        if not self.enabled():
            return []
        fts = self._fts_query(q)
        if not fts:
            return []
        where = ["docs MATCH ?"]
        params = [_TEXT_COL, MARK_A, MARK_B, fts]   # first three feed snippet() in the SELECT
        ks = [k for k in (kinds or []) if k in _DOC_KINDS]
        if ks:
            where.append("kind IN (%s)" % ",".join("?" * len(ks)))
            params += ks
        if since_ts:
            try:
                where.append("ts>=?")
                params.append(float(since_ts))
            except (TypeError, ValueError):
                pass
        params.append(int(limit))
        sql = ("SELECT uuid, session_id, project, host, role, kind, ts, "
               "snippet(docs, ?, ?, ?, '…', 12) AS snip "
               "FROM docs WHERE " + " AND ".join(where) + " ORDER BY bm25(docs) LIMIT ?")
        try:
            with self._lock:
                rows = self._db.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        # uuid is the stable anchor for opening the surrounding conversation (context()).
        return [{"uuid": r["uuid"], "session_id": r["session_id"], "project": r["project"], "host": r["host"],
                 "role": r["role"], "kind": r["kind"], "ts": r["ts"], "snippet": r["snip"]}
                for r in rows]

    def context(self, session_id, anchor=None, before_pos=None, after_pos=None, n=20):
        """Read the conversation around a search hit, straight from the indexed blocks (raw content — served
        only on the loopback content port). FTS5 rowid is insertion order, which within one session == transcript
        order, so we window by rowid. Three modes:
          • anchor=<uuid>     → the block + n before & n after (initial open; flags the hit)
          • before_pos=<pos>  → n blocks immediately ABOVE pos (load-more-up)
          • after_pos=<pos>   → n blocks immediately BELOW pos (load-more-down)
        Each block carries an opaque `pos` (the rowid) the client pages with. Returns
        {blocks:[{pos,role,kind,ts,text,hit}], has_more_before, has_more_after}."""
        n = max(1, min(int(n), 200))
        cols = "rowid AS pos, uuid, role, kind, ts, text"

        def mk(r, apos=None):
            return {"pos": r["pos"], "role": r["role"], "kind": r["kind"], "ts": r["ts"],
                    "text": r["text"], "hit": (apos is not None and r["pos"] == apos)}

        with self._lock:
            if before_pos is not None:
                rows = self._db.execute(
                    f"SELECT {cols} FROM docs WHERE session_id=? AND rowid<? ORDER BY rowid DESC LIMIT ?",
                    (session_id, int(before_pos), n)).fetchall()
                blocks = [mk(r) for r in reversed(rows)]
            elif after_pos is not None:
                rows = self._db.execute(
                    f"SELECT {cols} FROM docs WHERE session_id=? AND rowid>? ORDER BY rowid ASC LIMIT ?",
                    (session_id, int(after_pos), n)).fetchall()
                blocks = [mk(r) for r in rows]
            else:
                ar = (self._db.execute("SELECT rowid FROM docs WHERE session_id=? AND uuid=?",
                                       (session_id, anchor)).fetchone() if anchor else None)
                apos = ar["rowid"] if ar else (
                    self._db.execute("SELECT MIN(rowid) AS m FROM docs WHERE session_id=?",
                                     (session_id,)).fetchone() or {"m": None})["m"]
                if apos is None:
                    return {"blocks": [], "has_more_before": False, "has_more_after": False}
                up = self._db.execute(
                    f"SELECT {cols} FROM docs WHERE session_id=? AND rowid<=? ORDER BY rowid DESC LIMIT ?",
                    (session_id, apos, n + 1)).fetchall()
                down = self._db.execute(
                    f"SELECT {cols} FROM docs WHERE session_id=? AND rowid>? ORDER BY rowid ASC LIMIT ?",
                    (session_id, apos, n)).fetchall()
                blocks = [mk(r, apos) for r in reversed(up)] + [mk(r, apos) for r in down]
            more_b = more_a = False
            if blocks:
                top, bot = blocks[0]["pos"], blocks[-1]["pos"]
                more_b = bool(self._db.execute("SELECT 1 FROM docs WHERE session_id=? AND rowid<? LIMIT 1",
                                               (session_id, top)).fetchone())
                more_a = bool(self._db.execute("SELECT 1 FROM docs WHERE session_id=? AND rowid>? LIMIT 1",
                                               (session_id, bot)).fetchone())
        return {"blocks": blocks, "has_more_before": more_b, "has_more_after": more_a}

    # ---- semantic search (local Ollama embeddings) ----
    @staticmethod
    def _np():
        try:
            import numpy as np
            return np
        except ImportError:
            return None

    def semantic_available(self):
        """Semantic is usable only when an embed model is configured, search is on, and numpy is present.
        (Ollama reachability is proven at embed/query time; failures land in meta['embed_error'].)"""
        return bool(EMBED_MODEL) and self.enabled() and self._np() is not None

    def _embed_texts(self, texts):
        """Call the local Ollama embeddings endpoint. Raises on transport/HTTP error (caller records it)."""
        import urllib.request
        body = json.dumps({"model": EMBED_MODEL, "input": list(texts)}).encode("utf-8")
        req = urllib.request.Request(OLLAMA_URL + "/api/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_EMBED_TIMEOUT) as r:
            return (json.loads(r.read()) or {}).get("embeddings") or []

    @staticmethod
    def _norm_bytes(np, v):
        a = np.asarray(v, dtype="float32")
        nrm = float(np.linalg.norm(a))
        if nrm > 0:
            a = a / nrm
        return a.tobytes()

    def embed_pass(self, batch=64):
        """Embed the next batch of un-embedded CONVERSATION blocks via Ollama. No-op unless semantic is
        available. Resilient: if the batch call fails (e.g. one block exceeds the model context), fall back
        to per-item — shrinking the text, and finally quarantining a block in embed_skip — so a single
        pathological block can never wedge the drain. Returns {embedded, skipped} (errors recorded, not raised)."""
        np = self._np()
        if not self.semantic_available() or np is None:
            return {"embedded": 0, "skipped": 0}
        marks = ",".join("?" * len(EMBED_KINDS))
        with self._lock:
            rows = self._db.execute(
                f"SELECT uuid, text FROM docs WHERE kind IN ({marks}) "
                f"AND uuid NOT IN (SELECT uuid FROM embeddings) "
                f"AND uuid NOT IN (SELECT uuid FROM embed_skip) LIMIT ?",
                (*EMBED_KINDS, int(batch))).fetchall()
        if not rows:
            return {"embedded": 0, "skipped": 0}
        items = [(r["uuid"], (r["text"] or "")[:_EMBED_TEXT_CAP]) for r in rows]
        pairs, skip = [], []
        try:
            vecs = self._embed_texts([t for _, t in items])
            if len(vecs) != len(items):
                raise ValueError("embedding count mismatch")
            pairs = [(u, self._norm_bytes(np, v)) for (u, _), v in zip(items, vecs)]
        except Exception as e:  # noqa: BLE001 — batch failed → degrade to per-item shrink-and-skip
            import urllib.error
            self.set_meta("embed_error", str(e)[:200])
            down = False   # transport/5xx (Ollama unreachable) → stop, leave the rest for a later retry
            for u, t in items:
                if down:
                    break
                got, only_400 = None, True
                for cap in _EMBED_FALLBACK_CAPS:
                    try:
                        v = self._embed_texts([t[:cap]])
                        if v:
                            got = self._norm_bytes(np, v[0])
                            break
                    except urllib.error.HTTPError as he:
                        if he.code != 400:           # 5xx etc. → treat as Ollama trouble, not a bad block
                            only_400 = False
                            down = True
                            self.set_meta("embed_error", "HTTP %s" % he.code)
                            break
                        # 400 = input problem → shrink and retry
                    except Exception as e2:          # noqa: BLE001 — connection/timeout → Ollama down
                        only_400 = False
                        down = True
                        self.set_meta("embed_error", str(e2)[:200])
                        break
                if got is not None:
                    pairs.append((u, got))
                elif only_400:
                    skip.append((u,))   # genuinely unembeddable even at the smallest cap → quarantine
        with self._lock:
            if pairs:
                self._db.executemany("INSERT OR IGNORE INTO embeddings(uuid, vec) VALUES(?,?)", pairs)
            if skip:
                self._db.executemany("INSERT OR IGNORE INTO embed_skip(uuid) VALUES(?)", skip)
            self._db.commit()
        if pairs:
            self.set_meta("embed_error", "")
        self._emb_cache = None   # vectors changed → drop the in-memory matrix
        return {"embedded": len(pairs), "skipped": len(skip)}

    def _load_matrix(self):
        """Lazily build (and cache) the normalized embedding matrix for nearest-neighbor scoring."""
        np = self._np()
        if np is None:
            return ([], None)
        if self._emb_cache is not None:
            return self._emb_cache
        with self._lock:
            rows = self._db.execute("SELECT uuid, vec FROM embeddings").fetchall()
        if not rows:
            self._emb_cache = ([], None)
            return self._emb_cache
        uuids = [r["uuid"] for r in rows]
        mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype="float32").reshape(len(rows), -1)
        self._emb_cache = (uuids, mat)
        return self._emb_cache

    def semantic_search(self, q, limit=40, kinds=None, since_ts=None):
        """Meaning-based nearest-neighbor search: embed the query, cosine against the corpus matrix, then
        fetch the top hits' metadata (applying the same kind/date filters). Same result shape as search()
        + a `score`. Returns [] if semantic unavailable / empty corpus / Ollama down."""
        np = self._np()
        if not self.semantic_available() or np is None:
            return []
        q = (q or "").strip()
        if not q:
            return []
        try:
            qv = self._embed_texts([q])
        except Exception as e:  # noqa: BLE001
            self.set_meta("embed_error", str(e)[:200])
            return []
        if not qv:
            return []
        uuids, mat = self._load_matrix()
        if mat is None or not uuids:
            return []
        qa = np.asarray(qv[0], dtype="float32")
        nrm = float(np.linalg.norm(qa))
        if nrm > 0:
            qa = qa / nrm
        if qa.shape[0] != mat.shape[1]:   # dimension mismatch (model changed) → no results, not a crash
            return []
        sims = mat @ qa
        k = int(min(len(uuids), max(limit * 5, 200)))
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        score = {uuids[i]: float(sims[i]) for i in top}
        ks = [x for x in (kinds or []) if x in _DOC_KINDS]
        where = ["uuid IN (%s)" % ",".join("?" * len(score))]
        params = list(score.keys())
        if ks:
            where.append("kind IN (%s)" % ",".join("?" * len(ks)))
            params += ks
        if since_ts:
            where.append("ts>=?")
            params.append(float(since_ts))
        with self._lock:
            rows = self._db.execute(
                "SELECT uuid, session_id, project, host, role, kind, ts, text "
                "FROM docs WHERE " + " AND ".join(where), params).fetchall()
        res = [{"uuid": r["uuid"], "session_id": r["session_id"], "project": r["project"], "host": r["host"],
                "role": r["role"], "kind": r["kind"], "ts": r["ts"],
                "snippet": (r["text"] or "")[:240], "score": round(score.get(r["uuid"], 0.0), 3)}
               for r in rows]
        res.sort(key=lambda x: -x["score"])
        return res[:limit]

    def embed_stats(self):
        """For the UI: is semantic usable, which model, and how far along the background embedding is."""
        marks = ",".join("?" * len(EMBED_KINDS))
        with self._lock:
            emb = self._db.execute("SELECT COUNT(*) AS c FROM embeddings").fetchone()["c"]
            skipped = self._db.execute("SELECT COUNT(*) AS c FROM embed_skip").fetchone()["c"]
            tot = self._db.execute(f"SELECT COUNT(*) AS c FROM docs WHERE kind IN ({marks})",
                                   EMBED_KINDS).fetchone()["c"]
        return {"available": self.semantic_available(), "model": EMBED_MODEL or None,
                "embedded": emb, "skipped": skipped, "embeddable": tot,
                "error": self.get_meta("embed_error") or None}

    def stats(self):
        with self._lock:
            docs = self._db.execute("SELECT COUNT(*) AS c FROM docs").fetchone()["c"]
            sess = self._db.execute("SELECT COUNT(DISTINCT session_id) AS c FROM docs").fetchone()["c"]
            files = self._db.execute("SELECT COUNT(*) AS c FROM files").fetchone()["c"]
        return {"enabled": self.enabled(), "docs": docs, "sessions": sess,
                "files": files, "last_indexed_ts": self._meta_float("last_indexed_ts")}

    def wipe(self):
        """Delete the entire content index (incl. embeddings) and turn search back OFF. 'Delete index'."""
        with self._lock:
            self._db.execute("DELETE FROM docs")
            self._db.execute("DELETE FROM seen")
            self._db.execute("DELETE FROM files")
            self._db.execute("DELETE FROM embeddings")
            self._db.execute("DELETE FROM embed_skip")
            self._db.execute("DELETE FROM meta WHERE key='last_indexed_ts'")
            self._db.execute("DELETE FROM meta WHERE key='embed_error'")
            self._db.execute(
                "INSERT INTO meta(key,value) VALUES('enabled','0') "
                "ON CONFLICT(key) DO UPDATE SET value='0'")
            self._db.commit()
        self._emb_cache = None

    # ---- maintenance (V13 Slice 2) ----
    def checkpoint(self):
        """Truncate the WAL (the passive auto-checkpoint never shrinks the -wal file). Lossless."""
        with self._lock:
            row = self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return tuple(row) if row else None

    def prune_before(self, ts, dry=False):
        """Remove indexed docs older than `ts` (retention). Cascades to embeddings/embed_skip/seen by uuid.
        Safe: offset-based indexing won't re-add them (their bytes are before the file offset). Returns docs
        removed. The `files` offsets are left intact (re-indexing resumes from where it was)."""
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) AS c FROM docs WHERE ts < ?", (ts,)).fetchone()["c"]
            if dry or not n:
                return n
            uuids = [r["uuid"] for r in self._db.execute("SELECT uuid FROM docs WHERE ts < ?", (ts,))]
            self._db.execute("DELETE FROM docs WHERE ts < ?", (ts,))
            for i in range(0, len(uuids), 500):     # chunk the IN-lists
                chunk = uuids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                self._db.execute(f"DELETE FROM embeddings WHERE uuid IN ({marks})", chunk)
                self._db.execute(f"DELETE FROM embed_skip WHERE uuid IN ({marks})", chunk)
                self._db.execute(f"DELETE FROM seen WHERE uuid IN ({marks})", chunk)
            self._db.commit()
        self._emb_cache = None
        return n

    def prune_orphans(self, live_paths, dry=False):
        """Remove docs whose transcript file no longer exists on disk. Claude transcripts are one session
        per file (`<session_id>.jsonl`), so a dead file → drop that session's docs + its files row. Returns
        {files, docs}."""
        live = set(live_paths or [])
        with self._lock:
            paths = [r["path"] for r in self._db.execute("SELECT path FROM files")]
        dead = [p for p in paths if p not in live]
        sids = list({os.path.splitext(os.path.basename(p))[0] for p in dead})
        if not dead:
            return {"files": 0, "docs": 0}
        with self._lock:
            ndocs = 0
            for i in range(0, len(sids), 500):
                chunk = sids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                ndocs += self._db.execute(
                    f"SELECT COUNT(*) AS c FROM docs WHERE session_id IN ({marks})", chunk).fetchone()["c"]
            if dry:
                return {"files": len(dead), "docs": ndocs}
            for i in range(0, len(sids), 500):
                chunk = sids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                uuids = [r["uuid"] for r in self._db.execute(
                    f"SELECT uuid FROM docs WHERE session_id IN ({marks})", chunk)]
                self._db.execute(f"DELETE FROM docs WHERE session_id IN ({marks})", chunk)
                for j in range(0, len(uuids), 500):
                    uc = uuids[j:j + 500]
                    um = ",".join("?" * len(uc))
                    self._db.execute(f"DELETE FROM embeddings WHERE uuid IN ({um})", uc)
                    self._db.execute(f"DELETE FROM embed_skip WHERE uuid IN ({um})", uc)
                    self._db.execute(f"DELETE FROM seen WHERE uuid IN ({um})", uc)
            for i in range(0, len(dead), 500):
                chunk = dead[i:i + 500]
                marks = ",".join("?" * len(chunk))
                self._db.execute(f"DELETE FROM files WHERE path IN ({marks})", chunk)
            self._db.commit()
        self._emb_cache = None
        return {"files": len(dead), "docs": ndocs}

    def vacuum(self):
        """Reclaim free pages after a prune. Briefly exclusive; busy_timeout covers concurrent readers."""
        with self._lock:
            self._db.commit()
            self._db.execute("VACUUM")
        return os.path.getsize(self.path) if os.path.exists(self.path) else 0

    def close(self):
        with self._lock:
            self._db.close()

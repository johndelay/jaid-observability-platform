#!/usr/bin/env python3
"""
cc-observability — "You should know" (YSK) capture
==================================================
Claude Code's built-in YSK plugin (`cc-plugin-you-should-know`) runs a quiet side agent during long turns
and puts a one-line note above the prompt when it spots something you'd likely miss. This module turns
what the plugin leaves behind into behavioral events (the `events` table), so its value can be judged
against its cost:

  - ysk_prompt_taken   the prompt the plugin types when you press "Chat in main session" (transcripts)
  - ysk_shown          a note was shown           ┐ diffs of the plugin's state file,
  - ysk_resolved       a note was closed with an  ┘ ~/.claude/plugins/store/cc-plugin-you-should-know_builtin-*.json
                       answer (knew it / understood / chat / helpful — the file doesn't say which)
  - ysk_rating         a line the `ysk-rate` skill appended to ysk-ratings.jsonl (your verdict + why)
  - ysk_fork           one of the plugin's side-agent calls and its token usage, from Claude Code's DEBUG LOG —
                       only written when claude is started with --debug (→ ~/.claude/debug/<session>.txt); priced here
  - ysk_check          one check and its outcome (shown / none / …), from the state file — only written when
                       CLAUDE_CODE_YOU_SHOULD_KNOW_DEBUG=1; tells checks apart from "Learn more" calls

The note events carry `note_key` (a hash of the note text, identical across all four sources) so they join.

NB: the note events carry the NOTE TEXT — a deliberate exception to the usage DB holding no
content (it is exported and served on the PIN-gated LAN port like every other event). ysk_fork/ysk_check
carry none: token counts, model, timings, outcome.
NB: the plugin's calls never reach transcripts (it forks with skipTranscript), so their COST is visible only
in the debug log. The plugin's one model call (model.fork) serves both its checks and its "Learn more"
explanations, so a ysk_fork is not necessarily a check.
The plugin is closed-source and changes between Claude Code versions, so every parser here fails soft:
an unrecognized shape yields no events, never a guess.
"""
import glob
import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone

import costing

TAKEN_MARK = "Here is a note offered by a side agent:"
STATE_GLOB = "cc-plugin-you-should-know_builtin-*.json"
PLUGIN = "cc-plugin-you-should-know"
VERDICTS = ("GOOD", "MEH", "BAD")
CHECKS = ("true", "new", "matters", "timely", "acted")
STORE_DIR = os.path.expanduser(os.environ.get("CC_YSK_STORE_DIR", "~/.claude/plugins/store"))
DEBUG_DIR = os.path.expanduser(os.environ.get("CC_YSK_DEBUG_DIR", "~/.claude/debug"))

# Debug-log lines (2.1.296). A fork logs "finished" with its usage, then the mod-API line naming the plugin —
# "finished" is generic to every mod's model.fork, so only the pair attributes a call to this plugin.
_FINISHED = "Forked agent [plugin_model_fork] finished:"
_USAGE = re.compile(r"totalUsage: input=(\d+) output=(\d+) cacheRead=(\d+) cacheCreate=(\d+)")
_FORK = re.compile(r"\$\.model\.fork \(([^)]+)\): (\d+)ms, (\d+) repl")
_DISPATCH = re.compile(r"\[API:timing\] dispatching to \S+ model=(\S+)")
_SET_MODEL = re.compile(r"\[engine\] send set_model model=(\S+)")
_LINE_TS = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:.]+Z) ")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def note_key(note):
    """Stable join key for one note across shown / resolved / taken / rated (whitespace-normalized)."""
    return hashlib.sha1(" ".join(str(note).split()).encode("utf-8")).hexdigest()[:16]


def _row(event_id, ts, session_id, host, type_, ref, payload):
    return {"event_id": event_id, "ts": ts, "session_id": session_id, "account": None, "host": host,
            "type": type_, "ref": ref, "payload_json": json.dumps(payload, separators=(",", ":"))}


def _ts(v):
    """A rating's ts → epoch seconds. Accepts epoch s/ms or ISO-8601 (with Z); anything else → None."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v / 1000.0 if v > 1e11 else float(v)
    if isinstance(v, str) and v:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    return None


def parse_taken_prompt(text):
    """A typed user prompt → {tag, note, note_key} if the plugin wrote it, else None. Shape (2.1.296):
    '<TAKEN_MARK>\\n\\n> <tag> · <note>\\n>\\n> <explanation…>' — only the first quoted line is the note.
    Callers must pass only real prompts: a tool result can contain the marker phrase too."""
    if not isinstance(text, str) or not text.startswith(TAKEN_MARK):
        return None
    for ln in text[len(TAKEN_MARK):].splitlines():
        if ln.startswith("> ") and ln[2:].strip():
            tag, sep, note = ln[2:].strip().partition(" · ")
            tag, note = (tag.strip(), note.strip()) if sep else (None, tag.strip())
            return {"tag": tag, "note": note, "note_key": note_key(note)}
    return None


def read_state(store_dir=None):
    """The plugin's state dict, or None (no file / unreadable / caught mid-rewrite — the plugin rewrites it in
    place). Newest file wins if several hashes exist."""
    files = glob.glob(os.path.join(store_dir or STORE_DIR, STATE_GLOB))
    if not files:
        return None
    try:
        with open(max(files, key=os.path.getmtime), encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return None
    return st if isinstance(st, dict) else None


def state_events(state, host=None, now=None, backfill=False):
    """State dict → ysk_shown + ysk_resolved rows. Idempotent: event_id is type + note_key, so re-polling an
    unchanged file inserts nothing. `seen` = notes shown (newest last, capped at 50), `known` = the subset
    closed with an answer. Neither keeps a timestamp; the only one is `shownAt` (ms) on a note still on
    screen (sessions[].view), which also gives its session + tag. Otherwise ts = this poll's time.
    backfill=True marks the first read of an existing file: those notes were shown at unknown earlier times,
    so ts_source='backfill' and ts must not be read as when they happened."""
    if not isinstance(state, dict):
        return []
    seen = [n for n in (state.get("seen") or []) if isinstance(n, str) and n.strip()]
    known = [n for n in (state.get("known") or []) if isinstance(n, str) and n.strip()]
    on_screen = {}   # note line -> (session_id, tag, shownAt ms)
    for s in state.get("sessions") or []:
        v = s.get("view") if isinstance(s, dict) else None
        if isinstance(v, dict) and isinstance(v.get("line"), str):
            on_screen[v["line"]] = (s.get("id"), s.get("tag"), v.get("shownAt"))
    late = "backfill" if backfill else "poll"
    out = []
    for n in seen:
        k = note_key(n)
        sid, tag, shown_at = on_screen.get(n, (None, None, None))
        if isinstance(shown_at, (int, float)) and not isinstance(shown_at, bool):
            ts, src = shown_at / 1000.0, "shownAt"
        else:
            ts, src = now, late
        out.append(_row(f"ysk:shown:{k}", ts, sid, host, "ysk_shown", k,
                        {"tag": tag, "note": n, "note_key": k, "ts_source": src}))
    for n in known:
        k = note_key(n)
        out.append(_row(f"ysk:resolved:{k}", now, None, host, "ysk_resolved", k,
                        {"note": n, "note_key": k, "ts_source": late}))
    # With CLAUDE_CODE_YOU_SHOULD_KNOW_DEBUG=1 the plugin also keeps, per session, a running count `checked` and
    # its last 3 checks {number, step, outcome, latencyMs, at}. `number` is that running count, so a check that
    # scrolled out between polls still shows as a gap — the session's check total is exact either way.
    for s in state.get("sessions") or []:
        if not isinstance(s, dict) or not isinstance(s.get("id"), str):
            continue
        for c in s.get("checks") or []:
            if not isinstance(c, dict) or not isinstance(c.get("outcome"), str) \
                    or not all(_num(c.get(f)) for f in ("number", "step", "latencyMs", "at")):
                continue
            out.append(_row(f"ysk:check:{s['id']}:{int(c['number'])}", c["at"] / 1000.0, s["id"], host,
                            "ysk_check", c["outcome"],
                            {"number": int(c["number"]), "step": int(c["step"]), "outcome": c["outcome"],
                             "latency_ms": c["latencyMs"]}))
    return out


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def fork_events(lines, host=None, session_id=None, log=None, state=None):
    """Debug-log lines → ysk_fork rows: one per side-agent call this plugin made, with its token usage, the
    model it ran on, and its cost at that model's rates. `state` carries parsing across calls on the same file
    (the model in force, "finished" lines still waiting for their plugin line), so a file can be read in
    chunks. Other mods' forks are paired off and dropped. Model = the one dispatched for the fork's request
    (logged just before it), else the session's (`set_model`) — the fork runs on the main thread's model."""
    st = {} if state is None else state
    pending = st.setdefault("pending", [])
    out = []
    for ln in lines:
        if "[API:timing] dispatching" in ln:
            m = _DISPATCH.search(ln)
            st["dispatch_model"] = m.group(1) if m else st.get("dispatch_model")
        elif "[API REQUEST]" in ln and "source=hook_prompt" in ln:
            st["fork_model"] = st.get("dispatch_model")
        elif "send set_model" in ln:
            m = _SET_MODEL.search(ln)
            st["session_model"] = m.group(1) if m else st.get("session_model")
        elif _FINISHED in ln:
            u, t = _USAGE.search(ln), _LINE_TS.match(ln)
            if u:
                pending.append({"line": ln, "ts": _ts(t.group(1)) if t else None, "usage": [int(x) for x in u.groups()],
                                "model": st.get("fork_model") or st.get("session_model")})
                del pending[:-16]   # bound it: a "finished" whose plugin line never came is dropped, not guessed
        elif "$.model.fork (" in ln and pending:
            m = _FORK.search(ln)
            f = pending.pop(0)
            if not m or m.group(1) != PLUGIN:
                continue
            inp, outp, cr, cc = f["usage"]
            cost = costing.reprice(f["model"], inp, outp, cr, cc) if f["model"] else None
            eid = "ysk:fork:" + hashlib.sha1(f"{log}|{f['line']}|{ln}".encode("utf-8")).hexdigest()[:16]
            out.append(_row(eid, f["ts"], session_id, host, "ysk_fork", f["model"],
                            {"model": f["model"], "input": inp, "output": outp, "cache_read": cr, "cache_create": cc,
                             "ms": int(m.group(2)), "replies": int(m.group(3)),
                             "cost_usd": round(cost, 6) if cost is not None else None, "log": log}))
    return out


def debug_fork_events(debug_dir=None, cursors=None, host=None):
    """Each debug log in `debug_dir` (default name: <session id>.txt) → ysk_fork rows, reading only what was
    appended since the last pass. `cursors` ({path: parse state + byte offset}) lives in the caller's memory;
    a restart re-reads files from the start, which is harmless (stable event ids). Only complete lines are
    read — a half-written last line waits for the next pass. Symlinks (`latest`) are skipped: no double count."""
    d = debug_dir or DEBUG_DIR
    cursors = {} if cursors is None else cursors
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    out = []
    for name in names:
        p = os.path.join(d, name)
        if not name.endswith((".txt", ".log")) or os.path.islink(p) or not os.path.isfile(p):
            continue
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        cur = cursors.get(p)
        if cur is None or size < cur["offset"]:   # new file, or truncated/replaced → start over
            cur = cursors[p] = {"offset": 0}
        if size == cur["offset"]:
            continue
        try:
            with open(p, "rb") as fh:
                fh.seek(cur["offset"])
                chunk = fh.read(size - cur["offset"])
        except OSError:
            continue
        end = chunk.rfind(b"\n") + 1
        if not end:
            continue
        cur["offset"] += end
        stem = os.path.splitext(name)[0]
        out += fork_events(chunk[:end].decode("utf-8", "replace").splitlines(), host=host,
                           session_id=stem if _UUID.match(stem) else None, log=name, state=cur)
    return out


def rating_events(lines, host=None):
    """ysk-ratings.jsonl lines → ysk_rating rows. One line = one rating; re-rating a note appends a new line
    (the newest wins when scoring). event_id hashes the raw line, so re-reading the file is idempotent.
    Lines that are malformed or off-rubric (no note, verdict not GOOD/MEH/BAD) are skipped, never guessed."""
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(r, dict) or r.get("verdict") not in VERDICTS or not isinstance(r.get("note"), str) \
                or not r["note"].strip():
            continue
        k = note_key(r["note"])
        checks = r.get("checks") if isinstance(r.get("checks"), dict) else {}
        # user_checks: which checks the USER answered (vs Claude's proposal left standing). None = not recorded,
        # e.g. a line from the paste-in prompt — so a precision figure can be restricted to user-answered ratings.
        user = r.get("user_checks")
        user = [c for c in CHECKS if c in user] if isinstance(user, list) else None
        payload = {"tag": r.get("tag"), "note": r["note"], "note_key": k, "verdict": r["verdict"],
                   "why": r.get("why"), "checks": {c: checks.get(c) for c in CHECKS}, "user_checks": user}
        eid = "ysk:rating:" + hashlib.sha1(ln.encode("utf-8")).hexdigest()[:16]
        out.append(_row(eid, _ts(r.get("ts")), r.get("session_id"), host, "ysk_rating", k, payload))
    return out

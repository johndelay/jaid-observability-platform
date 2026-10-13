#!/usr/bin/env python3
"""ysk-rate helper — find the "You should know" note to rate, and append a rating line safely.

  rate.py latest [--session ID]   → JSON {note, tag, session_id, source} for the note on screen (preferring
                                    that session's), else the newest note the plugin showed
  rate.py append < rating.json    → check one rating against the rubric, fill ts/host, append it to the
                                    ratings file, print the exact line written

Ratings file: $CC_YSK_RATINGS, else ~/.cache/cc-observability/ysk-ratings.jsonl — where the JAID dashboard
reads it (ysk_rating events). Mode 600: it holds note text. Stdlib only; standalone (no dashboard needed).
Exit: 0 ok · 1 rating rejected (off-rubric) · 2 nothing to rate / could not read or write.
"""
import glob
import json
import os
import socket
import sys
from datetime import datetime, timezone

VERDICTS = ("GOOD", "MEH", "BAD")
CHECKS = ("true", "new", "matters", "timely", "acted")
STORE_DIR = os.path.expanduser(os.environ.get("CC_YSK_STORE_DIR", "~/.claude/plugins/store"))
RATINGS = os.path.expanduser(os.environ.get("CC_YSK_RATINGS", "~/.cache/cc-observability/ysk-ratings.jsonl"))


def latest(session=None):
    files = glob.glob(os.path.join(STORE_DIR, "cc-plugin-you-should-know_builtin-*.json"))
    if not files:
        return None
    with open(max(files, key=os.path.getmtime), encoding="utf-8") as fh:
        st = json.load(fh)
    views = [(s.get("id"), s.get("tag"), s["view"]) for s in st.get("sessions") or []
             if isinstance(s, dict) and isinstance(s.get("view"), dict) and s["view"].get("line")]
    views.sort(key=lambda v: (v[0] == session, v[2].get("shownAt") or 0))
    if views:
        sid, tag, v = views[-1]
        return {"note": v["line"], "tag": tag, "session_id": sid, "source": "on screen"}
    seen = [n for n in st.get("seen") or [] if isinstance(n, str) and n.strip()]
    return {"note": seen[-1], "tag": None, "session_id": None, "source": "newest shown"} if seen else None


def append(r):
    if not isinstance(r, dict) or r.get("verdict") not in VERDICTS:
        return None, f"verdict must be one of {', '.join(VERDICTS)}"
    if not isinstance(r.get("note"), str) or not r["note"].strip():
        return None, "note is required (the note's text, verbatim)"
    checks = r.get("checks")
    if not isinstance(checks, dict) or any(not isinstance(checks.get(c), bool) for c in CHECKS):
        return None, f"checks must give true/false for each of: {', '.join(CHECKS)}"
    sid = r.get("session_id")
    rec = {"ts": r.get("ts") or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
           "host": r.get("host") or socket.gethostname(),
           "session_id": None if not sid or "CLAUDE_SESSION_ID" in str(sid) else sid,   # unsubstituted → null
           "tag": r.get("tag"), "note": r["note"].strip(), "checks": {c: checks[c] for c in CHECKS},
           "verdict": r["verdict"], "why": (r.get("why") or "").strip()}
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
    os.makedirs(os.path.dirname(RATINGS), exist_ok=True)
    fd = os.open(RATINGS, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return line, None


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "latest":
        session = argv[argv.index("--session") + 1] if "--session" in argv[:-1] else None
        try:
            n = latest(session)
        except (OSError, ValueError) as e:
            print(f"could not read the plugin state file: {e.__class__.__name__}", file=sys.stderr)
            return 2
        if not n:
            print("no note found (is the You should know plugin enabled on this machine?)", file=sys.stderr)
            return 2
        print(json.dumps(n, ensure_ascii=False))
        return 0
    if cmd == "append":
        try:
            r = json.load(sys.stdin)
        except ValueError:
            print("rejected: stdin is not one JSON object", file=sys.stderr)
            return 1
        try:
            line, err = append(r)
        except OSError as e:
            print(f"could not write {RATINGS}: {e.__class__.__name__}", file=sys.stderr)
            return 2
        if err:
            print(f"rejected: {err}", file=sys.stderr)
            return 1
        print(f"wrote to {RATINGS}:\n{line}")
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

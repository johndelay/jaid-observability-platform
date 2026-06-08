#!/usr/bin/env python3
"""Claude Coach — metrics gatherer (V12).

Emits ONE compact JSON object of rate-normalized, content-free coaching metrics for your Claude Code
usage, which the `claude-coach` Skill then reads and turns into personalized advice — coached by your OWN
Claude, no server or third party required.

Two sources, auto-selected (prefer the richer one, never fail):
  1. DASHBOARD  — if a local cc-observability dashboard is reachable (CC_URL or http://localhost:8099),
     GET its already-computed /craft (+ /drift) endpoints. Authoritative; matches the UI exactly.
  2. STANDALONE — otherwise scan ~/.claude/projects/*.jsonl directly and compute the SAME aggregates
     with the SAME scoring modules (store.extract_events / craft / medals). Zero infra: this is the
     "works the moment you clone, no Docker" path. The dashboard is the upgrade, not a requirement.

HONESTY (same discipline as the cost estimate): every number here is a heuristic over observable proxies
(cache hits, compaction triggers, context-zone time, skill/subagent use) — NOT a judgment of work quality.
CONTENT-FREE: never reads or emits conversation text — token counts, event types, and timestamps only.
LOCAL-ONLY: reads your machine; sends nothing anywhere (the dashboard fetch, if used, is your own LAN box).
"""
import json
import os
import sys
import time
import glob
import urllib.parse
import urllib.request
import urllib.error
import http.cookiejar

# --- import the project's single source of truth for scoring + parsing (repo root is two dirs up) ---
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
try:
    import store          # extract_events, zone_band, parse_ts, ZONE_BANDS, ZONE_TOK_*, model_family
    import craft          # score()
    import medals         # evaluate()
    import costing        # reprice() — V11 what-if model-downgrade savings (standalone path)
except Exception as e:  # noqa: BLE001
    print(json.dumps({"error": f"cannot import scoring modules from {_REPO}: {e}"}))
    sys.exit(0)

CC_URL = os.environ.get("CC_URL", "http://localhost:8099").rstrip("/")
ACCESS_PIN = os.environ.get("CC_ACCESS_PIN", "")
PROJECTS_DIR = os.path.expanduser(os.environ.get("CC_PROJECTS_DIR", "~/.claude/projects"))


# ============================ DASHBOARD PATH ============================
def _opener():
    """A urllib opener with a cookie jar — captures the `cc_auth` cookie across the /login 302 redirect."""
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def _login(op):
    """If the dashboard UI is PIN-gated, POST the (form-encoded) PIN; the opener's jar keeps the cookie.
    Returns True on success. No PIN configured → can't authenticate."""
    if not ACCESS_PIN:
        return False
    body = urllib.parse.urlencode({"pin": ACCESS_PIN}).encode()
    try:
        op.open(CC_URL + "/login", data=body, timeout=4).read()
        return True
    except Exception:  # noqa: BLE001
        return False


def from_dashboard():
    """Return the dashboard's /craft (+ /drift) payload, or None if unreachable / not enabled / can't auth."""
    op = _opener()
    try:
        try:
            raw = op.open(CC_URL + "/craft", timeout=4).read()
        except urllib.error.HTTPError as he:
            if he.code in (401, 403) and _login(op):   # PIN-gated → log in, retry once
                raw = op.open(CC_URL + "/craft", timeout=4).read()
            else:
                return None
        data = json.loads(raw)
        if not data.get("enabled") or data.get("score") is None:
            return None
        try:
            data["drift"] = json.loads(op.open(CC_URL + "/drift", timeout=4).read())
        except Exception:  # noqa: BLE001
            data["drift"] = None
        try:   # V11 efficiency coach: what-if savings + subscription ROI + cap headroom (authoritative)
            data["efficiency"] = json.loads(op.open(CC_URL + "/efficiency", timeout=4).read())
        except Exception:  # noqa: BLE001
            data["efficiency"] = None
        data["source"] = "dashboard"
        data["dashboard_url"] = CC_URL
        return data
    except Exception:  # noqa: BLE001
        return None


# ============================ STANDALONE PATH ============================
def _iter_entries():
    """Yield every JSON transcript entry across ~/.claude/projects/**/*.jsonl (newest files first so a
    partial read still favors recent activity). Skips unreadable lines silently."""
    files = glob.glob(os.path.join(PROJECTS_DIR, "**", "*.jsonl"), recursive=True)
    files.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        yield json.loads(ln)
                    except ValueError:
                        continue
        except OSError:
            continue


def _scan():
    """One pass over the transcripts → (usage_rows, events). Mirrors watcher.usage_events (per-message token
    counts) + store.extract_events (behavioral events), deduped exactly as the store would."""
    usage, events = [], []
    seen_u, seen_e = set(), set()
    for d in _iter_entries():
        # behavioral events (compaction / skill_use / subagent_spawn / tool_error) — reuse the store's parser
        for ev in store.extract_events(d):
            if ev["event_id"] in seen_e:
                continue
            seen_e.add(ev["event_id"])
            events.append(ev)
        # per-message token usage (assistant messages only) — mirror watcher.usage_events fields
        if d.get("type") != "assistant":
            continue
        m = d.get("message") or {}
        u = m.get("usage") or {}
        mid, rid = m.get("id"), d.get("requestId")
        if not u or not mid or not rid:
            continue
        key = (mid, rid)
        if key in seen_u:
            continue
        seen_u.add(key)
        it = int(u.get("input_tokens", 0) or 0)
        cc = int(u.get("cache_creation_input_tokens", 0) or 0)
        cr = int(u.get("cache_read_input_tokens", 0) or 0)
        ot = int(u.get("output_tokens", 0) or 0)
        usage.append({
            "ts": store.parse_ts(d.get("timestamp")),
            "it": it, "cc": cc, "cr": cr, "ot": ot,
            "ctx": it + cc + cr,
            "side": bool(d.get("isSidechain")),
            "model": m.get("model"),
        })
    return usage, events


def _agg(usage, events):
    """Build the rate-normalized aggregate bundle — same keys + definitions as the server's
    _craft_aggregates (token_economics + zone_time + event_totals)."""
    cr = sum(r["cr"] for r in usage)
    it = sum(r["it"] for r in usage)
    cc = sum(r["cc"] for r in usage)
    denom = cr + it + cc
    cache_ratio = round(cr / denom, 4) if denom else None
    # zone_time: non-sidechain rows with real context, bucketed by absolute-token band
    zc = {b: 0 for b in store.ZONE_BANDS}
    for r in usage:
        if not r["side"] and r["ctx"] and r["ctx"] > 0:
            zc[store.zone_band(r["ctx"])] += 1
    ztot = sum(zc.values())
    healthy_pct = round((zc["sharp"] + zc["good"]) / ztot * 100, 1) if ztot else None
    et = {}
    for ev in events:
        et[ev["type"]] = et.get(ev["type"], 0) + 1
        if ev["type"] == "compaction":
            trig = (json.loads(ev["payload_json"]).get("trigger") or "")
            if trig in ("manual", "auto"):
                et["compaction_" + trig] = et.get("compaction_" + trig, 0) + 1
    agg = {
        "cache_ratio": cache_ratio,
        "messages": len(usage),
        "compaction_manual": et.get("compaction_manual", 0),
        "compaction_auto": et.get("compaction_auto", 0),
        "healthy_pct": healthy_pct,
        "tool_errors": et.get("tool_error", 0),
        "skill_use": et.get("skill_use", 0),
        "subagent_spawn": et.get("subagent_spawn", 0),
    }
    zone_time = dict(zc, total=ztot, healthy_pct=healthy_pct)
    mix = {}
    for r in usage:
        if r["model"]:
            mix[r["model"]] = mix.get(r["model"], 0) + 1
    return agg, zone_time, mix


def _window(usage, events, days):
    cut = time.time() - days * 86400
    return ([r for r in usage if r["ts"] and r["ts"] >= cut],
            [e for e in events if e["ts"] and e["ts"] >= cut])


def _daily_series(usage, events, days=30, min_msgs=50):
    """Per local calendar day score (mirrors store.daily_craft_aggregates) → trajectory + personal best."""
    cut = time.time() - days * 86400
    by_day = {}
    for r in usage:
        if not r["ts"] or r["ts"] < cut:
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(r["ts"]))
        by_day.setdefault(day, ([], []))[0].append(r)
    for e in events:
        if not e["ts"] or e["ts"] < cut:
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(e["ts"]))
        if day in by_day:
            by_day[day][1].append(e)
    series = []
    for day in sorted(by_day):
        u, ev = by_day[day]
        if len(u) < min_msgs:
            continue
        agg, _, _ = _agg(u, ev)
        s = craft.score(agg)["score"]
        if s is not None:
            series.append({"day": day, "score": s})
    return series


def _savings(usage, days=30, short_output=600, trivial_output=120):
    """Standalone V11 what-if model-downgrade savings — mirrors store.model_savings: reprice each non-sidechain
    row's stored tokens under a cheaper model and sum the positive delta (both via costing → pure rate arbitrage,
    no quality claim). ROI + cap headroom are dashboard-only (need a plan-price pref + rate_history)."""
    cut = time.time() - days * 86400
    pol = {"all_opus_sonnet": {"label": "All Opus → Sonnet", "saved": 0.0, "turns": 0},
           "short_opus_sonnet": {"label": "Short Opus turns → Sonnet", "saved": 0.0, "turns": 0},
           "trivial_haiku": {"label": "Trivial turns → Haiku", "saved": 0.0, "turns": 0}}
    considered = 0
    for r in usage:
        if r["side"] or (r["ts"] and r["ts"] < cut):
            continue
        considered += 1
        fam = store.model_family(r["model"])
        toks = dict(input_tokens=r["it"], output_tokens=r.get("ot", 0), cache_read=r["cr"], cache_creation=r["cc"])
        actual = costing.reprice(r["model"], **toks)
        if fam == "opus":
            s = actual - costing.reprice("claude-sonnet-4-6", **toks)
            if s > 0:
                pol["all_opus_sonnet"]["saved"] += s; pol["all_opus_sonnet"]["turns"] += 1
                if r.get("ot", 0) < short_output:
                    pol["short_opus_sonnet"]["saved"] += s; pol["short_opus_sonnet"]["turns"] += 1
        if fam in ("opus", "sonnet") and r.get("ot", 0) < trivial_output:
            s = actual - costing.reprice("claude-haiku-4-5", **toks)
            if s > 0:
                pol["trivial_haiku"]["saved"] += s; pol["trivial_haiku"]["turns"] += 1
    return {"policies": [{"id": k, "label": v["label"], "saved_usd": round(v["saved"], 2), "turns": v["turns"]}
                         for k, v in pol.items()], "considered": considered, "days": days, "estimate": True}


def from_standalone():
    usage, events = _scan()
    if not usage:
        return {"source": "standalone", "enabled": True, "score": None,
                "note": f"no usage found under {PROJECTS_DIR}"}
    agg, zone_time, mix = _agg(usage, events)
    out = craft.score(agg)
    out.update(medals.evaluate(agg))
    a7, _, _2 = (lambda uw, ew: (_agg(uw, ew)[0], None, None))(*_window(usage, events, 7))
    a30, _3, _4 = (lambda uw, ew: (_agg(uw, ew)[0], None, None))(*_window(usage, events, 30))
    out["compare"] = {"w7": craft.score(a7), "w30": craft.score(a30)}
    series = _daily_series(usage, events, days=30)
    out["series"] = series
    if series:
        best = max(series, key=lambda p: p["score"])
        out["best"] = best
        out["new_high"] = len(series) > 1 and series[-1] == best
    out.update({"enabled": True, "account": None, "zone_time": zone_time,
                "model_mix": mix, "source": "standalone", "drift": None})
    out["efficiency"] = {"enabled": True, "estimate": True, "savings": _savings(usage),
                         "roi": [], "caps": [],
                         "note": "standalone: what-if downgrade savings only — ROI needs a plan price and "
                                 "cap headroom needs rate history, both dashboard-only"}
    return out


def main():
    force = (sys.argv[1] if len(sys.argv) > 1 else "").lstrip("-")
    data = None
    if force != "standalone":
        data = from_dashboard()
    if data is None:
        data = from_standalone()
    data["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    print(json.dumps(data, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

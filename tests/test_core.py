"""Core unit tests (stdlib unittest, no deps): watcher parsing/derivation + server policy.
Run: python3 -m unittest discover -s tests   (or via ./verify.sh)"""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import watcher       # noqa: E402
import server        # noqa: E402
import mcp_probe     # noqa: E402
import tmux_inject   # noqa: E402
import costing       # noqa: E402
import store         # noqa: E402
import craft         # noqa: E402
import medals        # noqa: E402
import content_index  # noqa: E402
import version        # noqa: E402
import sqlite3        # noqa: E402
import maintenance    # noqa: E402
import redact         # noqa: E402
import portable       # noqa: E402
import crypto         # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skill", "jaid-coach"))
import coach_metrics  # noqa: E402
import shutil        # noqa: E402
import subprocess    # noqa: E402

USAGE = {"input_tokens": 1, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 85000, "output_tokens": 50}


def _iso_ago(seconds=0):
    """An ISO-8601 Z timestamp `seconds` in the past.

    Any event fixture whose test then queries a ROLLING WINDOW (`days=30`, etc.) must be built relative to
    now. A hardcoded date works until it drifts past the window, then the test starts failing on a date
    unrelated to any code change — which is exactly what happened to the two tests below: they pinned
    2026-06-06 against a `days=30` query and went red ~43 days later, with the code still correct.
    Timestamps used only for parsing/ordering assertions don't need this and are left as-is.
    """
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(lines):
    fd, p = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(json.dumps(x) for x in lines) + "\n")
    return p


def _asst(text="done"):
    return {"type": "assistant", "timestamp": "2026-06-04T20:00:00Z", "isSidechain": False,
            "cwd": "/x/Proj", "message": {"model": "claude-opus-4-8", "usage": USAGE,
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}}


def _user(text="hi"):
    return {"type": "user", "timestamp": "2026-06-04T20:01:00Z", "isSidechain": False,
            "message": {"content": text}}


class WatcherTests(unittest.TestCase):
    def test_derive_waiting_when_last_is_assistant(self):
        snap = watcher.parse_session(_write([_asst()]))
        self.assertEqual(snap["state"], "waiting")
        self.assertTrue(snap["needs_me"])
        self.assertIsNotNone(snap["awaiting_input_since"])

    def test_derive_working_when_last_is_user(self):
        snap = watcher.parse_session(_write([_asst(), _user()]))
        self.assertEqual(snap["state"], "working")
        self.assertFalse(snap["needs_me"])

    def test_snapshot_has_required_fields(self):
        snap = watcher.parse_session(_write([_asst()]))
        for k in ("session_id", "context_tokens", "transcript_path", "activity", "state", "needs_me"):
            self.assertIn(k, snap)
        self.assertEqual(snap["context_tokens"], 85011)  # input+cache_create+cache_read
        self.assertEqual(snap["mcp_observed"], {})       # no MCP calls → empty (MCP tax Slice 1)

    def test_parse_mcp_name(self):
        # MCP tax Slice 1: 'mcp__<server>__<tool>' → (server, tool); everything else → (None, None)
        self.assertEqual(watcher.parse_mcp_name("mcp__github__create_issue"), ("github", "create_issue"))
        self.assertEqual(watcher.parse_mcp_name("mcp__63dc9909-uuid__read_file"), ("63dc9909-uuid", "read_file"))
        self.assertEqual(watcher.parse_mcp_name("mcp__server"), ("server", ""))   # server, no tool segment
        self.assertEqual(watcher.parse_mcp_name("Read"), (None, None))            # normal tool
        self.assertEqual(watcher.parse_mcp_name("mcp__"), (None, None))           # malformed (empty server)
        self.assertEqual(watcher.parse_mcp_name(None), (None, None))

    def test_mcp_observed_usage_and_errors(self):
        # MCP tax Slice 1: attribute observed tool_use to servers; match error tool_results back by id
        def _asst_mcp(calls):   # calls = [(id, name)]
            content = [{"type": "text", "text": "ok"}]
            for i, n in calls:
                content.append({"type": "tool_use", "id": i, "name": n, "input": {}})
            return {"type": "assistant", "timestamp": "2026-06-04T20:00:00Z", "isSidechain": False,
                    "cwd": "/x/Proj", "message": {"model": "claude-opus-4-8", "usage": USAGE,
                    "content": content, "stop_reason": "tool_use"}}

        def _tr(tid, is_error=False):
            return {"type": "user", "timestamp": "2026-06-04T20:00:30Z", "isSidechain": False,
                    "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": is_error}]}}

        snap = watcher.parse_session(_write([
            _asst_mcp([("t1", "mcp__github__create_issue"), ("t2", "mcp__github__list_prs"),
                       ("t3", "mcp__filesystem__read_file"), ("t4", "Read")]),   # Read is NOT an MCP call
            _tr("t2", is_error=True), _tr("t3"),
        ]))
        mo = snap["mcp_observed"]
        self.assertEqual(set(mo), {"github", "filesystem"})              # 'Read' excluded
        self.assertEqual(mo["github"]["calls"], 2)
        self.assertEqual(mo["github"]["tools"], ["create_issue", "list_prs"])   # sorted
        self.assertEqual(mo["github"]["errors"], 1)                     # only t2 errored
        self.assertEqual(mo["filesystem"], {"tools": ["read_file"], "calls": 1, "errors": 0})
        # and it survives the server.compute() passthrough to the served snapshot
        self.assertEqual(server.compute(snap, time.time())["mcp_observed"], mo)

    def test_mcp_probe_parses_name_status_only_and_drops_secrets(self):
        # MCP tax Slice 2 — THE security-critical test. `claude mcp list` prints launch commands that can hold
        # company URLs and ${TOKEN} env refs; the probe must keep ONLY name + status and drop everything else.
        sample = (
            "Checking MCP server health…\n\n"
            "claude.ai Spotify: https://mcp-gateway-external-pilot.spotify.net/mcp - ✓ Connected\n"
            "claude.ai Slack: https://mcp.slack.com/mcp - ! Needs authentication\n"
            "plugin:aikido:aikido-mcp: npx -y @aikidosec/mcp - ✓ Connected\n"
            "jira: mcp-atlassian --jira-url https://your-company.atlassian.net --jira-token ${JIRA_API_TOKEN} - ✓ Connected\n"
            "gitlab: /bin/bash -c GITLAB_TOKEN=\"$GITLAB_PERSONAL_ACCESS_TOKEN\" exec /home/user/.local/lib/gitlab-mcp/main.js - ✓ Connected\n"
            "github: https://api.githubcopilot.com/mcp/ (HTTP) - ✗ Failed to connect\n"
            "playwright: npx @playwright/mcp@latest - ✓ Connected\n\n"
            "MCP Config Diagnostics\n"
            "Location: /home/user/.claude.json\n"
        )
        servers = mcp_probe.parse_mcp_list(sample)
        by = {s["name"]: s["status"] for s in servers}
        self.assertEqual(by["jira"], "connected")
        self.assertEqual(by["github"], "failed")
        self.assertEqual(by["claude.ai Slack"], "needs_auth")
        self.assertEqual(by["plugin:aikido:aikido-mcp"], "connected")   # a name with colons stays intact
        self.assertIn("playwright", by)
        self.assertNotIn("Location", by)                                # diagnostics/footer not parsed as servers
        self.assertNotIn("Checking MCP server health…", by)
        # every entry is EXACTLY {name, status} — no command/URL/env keys smuggled in
        self.assertTrue(all(set(s) == {"name", "status"} for s in servers))
        # SECURITY REGRESSION: no command/URL/host/token fragment may reach the payload
        blob = json.dumps(servers)
        for leak in ("your-company", "atlassian", "TOKEN", "${", "https://", "npx", "/bin/bash", "exec", ".claude.json"):
            self.assertNotIn(leak, blob, "MCP probe leaked %r into its payload" % leak)

    def test_activity_from_lines_renders_kinds(self):
        lines = [json.dumps(_asst("hello")),
                 json.dumps({"type": "user", "isSidechain": False,
                             "message": {"content": [{"type": "tool_result", "content": "out", "tool_use_id": "t"}]}})]
        items = watcher.activity_from_lines(lines)
        kinds = {it["kind"] for it in items}
        self.assertIn("text", kinds)
        self.assertIn("result", kinds)

    def test_ask_user_question_renders_as_ask(self):
        # AskUserQuestion must surface its question + option labels (the generic tool summary drops them).
        ask = {"type": "assistant", "timestamp": "2026-06-04T20:00:00Z", "isSidechain": False,
               "message": {"model": "claude-opus-4-8", "content": [
                   {"type": "tool_use", "name": "AskUserQuestion", "id": "q1",
                    "input": {"questions": [{"question": "Go live now?", "header": "Go live",
                                             "options": [{"label": "Activate now"}, {"label": "Leave dormant"}]}]}}]}}
        items = watcher.activity_from_lines([json.dumps(ask)])
        asks = [it for it in items if it["kind"] == "ask"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0]["question"], "Go live now?")
        self.assertEqual(asks[0]["options"], ["Activate now", "Leave dormant"])

    def test_post_sends_ingest_token_only_when_set(self):
        # post() must add X-CC-Token when CC_INGEST_TOKEN is set (server requires it), and
        # omit it otherwise (open LAN). Capture the Request without doing real network I/O.
        import urllib.request
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.headers)
            class _R:
                def read(self_inner): return b""
            return _R()

        orig_urlopen, orig_token = urllib.request.urlopen, watcher.INGEST_TOKEN
        try:
            urllib.request.urlopen = fake_urlopen
            # urllib title-cases header keys -> "X-cc-token"
            watcher.INGEST_TOKEN = "secret-xyz"
            watcher.post({"host": "h", "sessions": []})
            self.assertEqual(captured["headers"].get("X-cc-token"), "secret-xyz")

            captured.clear()
            watcher.INGEST_TOKEN = ""
            watcher.post({"host": "h", "sessions": []})
            self.assertNotIn("X-cc-token", captured["headers"])
        finally:
            urllib.request.urlopen, watcher.INGEST_TOKEN = orig_urlopen, orig_token

    def test_usage_events_extracts_tokens_without_content(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "sessX.jsonl")
        lines = [
            {"type": "assistant", "requestId": "r1", "sessionId": "sessX", "cwd": "/p",
             "timestamp": "2026-06-05T00:00:00Z",
             "message": {"id": "m1", "model": "claude-opus-4-8",
                         "content": [{"type": "text", "text": "SECRET_REPLY_TEXT"}],
                         "usage": {"input_tokens": 5, "cache_creation_input_tokens": 100,
                                   "cache_read_input_tokens": 1000, "output_tokens": 50}}},
            {"type": "assistant", "isSidechain": True, "requestId": "r2", "sessionId": "sessX",
             "message": {"id": "m2", "usage": {"output_tokens": 9}}},          # subagent → INCLUDED (tagged)
            {"type": "user", "message": {"content": "MY_PROMPT_TEXT"}},        # not assistant → skipped
        ]
        with open(p, "w") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")
        try:
            evs = watcher.usage_events(p)
            self.assertEqual(len(evs), 2)                          # main message + subagent message
            by = {e["message_id"]: e for e in evs}
            self.assertFalse(by["m1"]["is_sidechain"])
            self.assertTrue(by["m2"]["is_sidechain"])              # subagent usage tagged + shipped
            self.assertEqual(by["m1"]["usage"]["output_tokens"], 50)
            blob = json.dumps(evs)
            self.assertNotIn("SECRET_REPLY_TEXT", blob)            # NO assistant content leaves the host
            self.assertNotIn("MY_PROMPT_TEXT", blob)               # NO user content either
            self.assertNotIn("content", blob)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_account_flows_from_status_or_host_fallback(self):
        # multi-account labeling: the per-session statusline value (status.json) wins; a session with no
        # status account falls back to the host-level ACCOUNT. Isolate STATE_DIR + PROJECTS_DIRS so only
        # our temp state files drive collect_snapshots().
        state, proj = tempfile.mkdtemp(), tempfile.mkdtemp()
        sid_hooked, sid_bare = "sess-acct-hooked", "sess-acct-bare"
        with open(os.path.join(state, sid_hooked + ".status.json"), "w") as f:
            json.dump({"used_percentage": 5, "account": "from-status@x.com", "extra_usage": True}, f)
        with open(os.path.join(state, sid_hooked + ".state.json"), "w") as f:
            json.dump({"state": "working", "cwd": "/tmp/projA"}, f)
        with open(os.path.join(state, sid_bare + ".state.json"), "w") as f:   # no status.json -> no per-session account
            json.dump({"state": "working", "cwd": "/tmp/projB"}, f)
        saved = (watcher.STATE_DIR, watcher.PROJECTS_DIRS, watcher.ACCOUNT)
        try:
            watcher.STATE_DIR, watcher.PROJECTS_DIRS, watcher.ACCOUNT = state, [proj], "host-fallback@x.com"
            # read_state_files surfaces the hook's account verbatim
            recs = watcher.read_state_files(time.time())
            self.assertEqual(recs[sid_hooked]["account"], "from-status@x.com")
            self.assertTrue(recs[sid_hooked]["extra_usage"])           # extra-usage flag rides along
            # collect_snapshots: status account wins; the bare session inherits the host fallback
            snaps = {s["session_id"]: s for s in watcher.collect_snapshots()}
            self.assertEqual(snaps[sid_hooked]["account"], "from-status@x.com")
            self.assertTrue(snaps[sid_hooked]["extra_usage"])
            self.assertEqual(snaps[sid_bare]["account"], "host-fallback@x.com")
        finally:
            watcher.STATE_DIR, watcher.PROJECTS_DIRS, watcher.ACCOUNT = saved
            shutil.rmtree(state, ignore_errors=True)
            shutil.rmtree(proj, ignore_errors=True)

    def test_statusline_only_host_keeps_transcript_state(self):
        # Regression (pblaptop Triage blind-spot, 2026-06-12): a host that runs the statusline hook
        # (.status.json) but NOT the state hooks (.state.json) must keep the transcript-derived
        # 'waiting'. The status file carries no `state` key, so it must not clobber state to None
        # (which rendered as "unknown"/needs_me=False, hiding sessions actually awaiting input).
        state, proj = tempfile.mkdtemp(), tempfile.mkdtemp()
        sid = "sess-statusonly"
        with open(os.path.join(proj, sid + ".jsonl"), "w") as f:
            f.write(json.dumps(_asst()) + "\n")        # last msg = assistant -> derives 'waiting'
        with open(os.path.join(state, sid + ".status.json"), "w") as f:
            json.dump({"used_percentage": 38, "account": "x@y.com"}, f)   # statusline only, NO state
        saved = (watcher.STATE_DIR, watcher.PROJECTS_DIRS, watcher.ACCOUNT)
        try:
            watcher.STATE_DIR, watcher.PROJECTS_DIRS, watcher.ACCOUNT = state, [proj], "host@y.com"
            snaps = {s["session_id"]: s for s in watcher.collect_snapshots()}
            self.assertIn(sid, snaps)
            self.assertEqual(snaps[sid]["state"], "waiting")   # NOT clobbered to None by the status file
            self.assertTrue(snaps[sid]["needs_me"])
            self.assertEqual(snaps[sid]["account"], "x@y.com")  # statusline account still flows through
        finally:
            watcher.STATE_DIR, watcher.PROJECTS_DIRS, watcher.ACCOUNT = saved
            shutil.rmtree(state, ignore_errors=True)
            shutil.rmtree(proj, ignore_errors=True)


class ServerTests(unittest.TestCase):
    def test_window_for(self):
        self.assertEqual(server.window_for(50000, "claude-opus-4-8[1m]"), 1_000_000)
        self.assertEqual(server.window_for(50000, "claude-sonnet-4-6"), 200_000)
        self.assertEqual(server.window_for(300000, "whatever"), 1_000_000)  # auto-escalate past 200k

    def test_compute_authoritative_window(self):
        now = time.time()
        out = server.compute({"context_tokens": 100000, "auth_window": 1_000_000, "reported_at": now}, now)
        self.assertTrue(out["authoritative"])
        self.assertEqual(out["window"], 1_000_000)
        self.assertEqual(out["pct"], 10.0)

    def test_pct_compact_prefers_authoritative(self):
        # The gauge shows distance-to-auto-compact. When Claude Code's statusline supplies its own
        # used_percentage, pct_compact must equal it exactly (so the dashboard hits 100% when Claude
        # Code compacts) — NOT the raw fill, NOT our computed approximation.
        now = time.time()
        auth = server.compute({"context_tokens": 180000, "auth_window": 200000,
                               "auth_used_pct": 100.0, "reported_at": now}, now)
        self.assertEqual(auth["pct_compact"], 100.0)
        self.assertEqual(auth["pct"], 90.0)           # raw fill retained, distinct from the gauge value
        # Watcher-only host (no statusline feed) → fall back to the computed pct_to_compact.
        comp = server.compute({"context_tokens": 100000, "auth_window": 200000, "reported_at": now}, now)
        self.assertIsNone(comp.get("auth_used_pct"))
        self.assertEqual(comp["pct_compact"], comp["pct_to_compact"])

    def test_compute_passes_account_through(self):
        now = time.time()
        out = server.compute({"account": "me@example.com", "reported_at": now}, now)
        self.assertEqual(out["account"], "me@example.com")
        # account_approx defaults to True (approximate); exact only when the snapshot says so explicitly
        self.assertTrue(out["account_approx"])
        self.assertFalse(server.compute({"account": "me@x", "account_approx": False, "reported_at": now}, now)["account_approx"])
        self.assertTrue(server.compute({"account": "me@x", "account_approx": True, "reported_at": now}, now)["account_approx"])

    def test_needs_me_recency_bound(self):
        # Isolate the recency bound from the strict-blocking gate by giving both a blocking signal
        # (a permission Notification) — so the ONLY variable under test is how long ago the wait started.
        now = time.time()
        recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
        r = server.compute({"needs_me": True, "state": "waiting", "notif_kind": "permission", "awaiting_input_since": recent, "reported_at": now}, now)
        o = server.compute({"needs_me": True, "state": "waiting", "notif_kind": "permission", "awaiting_input_since": old, "reported_at": now}, now)
        self.assertTrue(r["needs_me"], "recent wait should stay needs-me")
        self.assertEqual(r["eff_state"], "waiting")
        self.assertFalse(o["needs_me"], "old wait should be demoted")
        self.assertEqual(o["eff_state"], "idle")

    def test_needs_me_strict_blocking_gate(self):
        # "Needs you" must mean Claude is BLOCKED on you, not merely that a turn finished or went idle. The
        # Stop hook fires "waiting" on every completion AND the Notification hook fires on a mere idle nudge,
        # so both must be demoted. Only a PERMISSION-classified Notification or an explicit AskUserQuestion
        # keeps a session in the hero.
        now = time.time()
        recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        # bare turn-completion: Claude just finished talking -> idle, not needs-me
        stop = server.compute({"needs_me": True, "state": "waiting", "last_hook": "Stop",
                               "awaiting_input_since": recent, "reported_at": now,
                               "activity": [{"kind": "text", "text": "all done"}]}, now)
        self.assertFalse(stop["needs_me"], "a finished turn (Stop, text) is your-turn, not needs-me")
        self.assertEqual(stop["eff_state"], "idle")
        # idle "waiting for your input" Notification -> still your-turn, NOT needs-me (the real-world bug)
        idle = server.compute({"needs_me": True, "state": "waiting", "last_hook": "Notification",
                               "notif_kind": "idle", "awaiting_input_since": recent, "reported_at": now,
                               "activity": [{"kind": "text", "text": "here is the summary"}]}, now)
        self.assertFalse(idle["needs_me"], "an idle-nudge Notification is NOT needs-me")
        self.assertEqual(idle["eff_state"], "idle")
        # permission Notification -> blocked on you
        notif = server.compute({"needs_me": True, "state": "waiting", "last_hook": "Notification",
                                "notif_kind": "permission", "awaiting_input_since": recent, "reported_at": now,
                                "activity": [{"kind": "text", "text": "running ls"}]}, now)
        self.assertTrue(notif["needs_me"], "a permission Notification is needs-me")
        self.assertEqual(notif["eff_state"], "waiting")
        # explicit AskUserQuestion -> blocked on you (also the only signal available on watcher-only hosts)
        ask = server.compute({"needs_me": True, "state": "waiting", "last_hook": "Stop",
                              "awaiting_input_since": recent, "reported_at": now,
                              "activity": [{"kind": "ask", "question": "Proceed?", "options": ["yes", "no"]}]}, now)
        self.assertTrue(ask["needs_me"], "an AskUserQuestion is needs-me")
        self.assertEqual(ask["eff_state"], "waiting")

    def test_needs_me_strict_gate_can_be_disabled(self):
        # CC_STRICT_NEEDS_ME=0 restores the old behavior: any finished turn surfaces as needs-me.
        now = time.time()
        recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        orig = server.STRICT_NEEDS_ME
        try:
            server.STRICT_NEEDS_ME = False
            out = server.compute({"needs_me": True, "state": "waiting", "last_hook": "Stop",
                                  "awaiting_input_since": recent, "reported_at": now,
                                  "activity": [{"kind": "text", "text": "all done"}]}, now)
            self.assertTrue(out["needs_me"], "with the strict gate off, a finished turn stays needs-me")
        finally:
            server.STRICT_NEEDS_ME = orig

    def test_remote_nonauthoritative_state_survives(self):
        # Watcher-only host (no Phase A statusline feed): no auth_window, cost/rate_limits null.
        # Triage must still work — real state/context preserved, window derived, eff_state computed.
        # Mirrors live my-laptop snapshot (opus-4-6, 189447 ctx → 94.7% of derived 200k window).
        now = time.time()
        recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        # On a watcher-only host there's no Notification hook, so the only blocking signal that can surface
        # needs-me is an explicit AskUserQuestion in the transcript — give it one to prove triage is alive.
        out = server.compute({
            "host": "my-laptop", "context_tokens": 189447, "model": "claude-opus-4-6",
            "state": "waiting", "needs_me": True, "awaiting_input_since": recent,
            "activity": [{"kind": "ask", "question": "Proceed?", "options": ["yes", "no"]}],
            "cost_usd": None, "rate_limits": None, "reported_at": now,
        }, now)
        self.assertFalse(out["authoritative"])            # no statusline feed
        self.assertEqual(out["window"], 200_000)          # derived from model, not auth_window
        self.assertEqual(out["context_tokens"], 189447)   # real context preserved
        self.assertEqual(out["pct"], 94.7)                # computed from derived window
        self.assertEqual(out["state"], "waiting")
        self.assertTrue(out["needs_me"])                  # triage ALIVE on watcher-only host (via the ask)
        self.assertEqual(out["eff_state"], "waiting")
        self.assertIsNone(out["cost_usd"])                # known gap: null on watcher-only
        self.assertIsNone(out["rate_limits"])

    def test_last_msg_derivation(self):
        # ask wins when most recent; falls back to the last assistant text; None when neither.
        self.assertIsNone(server.last_msg(None))
        self.assertIsNone(server.last_msg([{"kind": "tool", "name": "Bash"}]))
        txt = server.last_msg([{"kind": "text", "text": "  the answer is 42  "}, {"kind": "tool", "name": "Bash"}])
        self.assertEqual(txt, {"kind": "text", "text": "the answer is 42", "options": []})
        a = server.last_msg([{"kind": "text", "text": "earlier"},
                             {"kind": "ask", "question": "Pick one", "options": ["A", "B"]}])
        self.assertEqual(a, {"kind": "ask", "text": "Pick one", "options": ["A", "B"]})

    def test_compute_exposes_last_msg(self):
        now = time.time()
        out = server.compute({"reported_at": now, "activity": [{"kind": "text", "text": "hi there"}]}, now)
        self.assertEqual(out["last_msg"]["kind"], "text")
        self.assertEqual(out["last_msg"]["text"], "hi there")

    def test_answerable_and_reply_enabled_passthrough(self):
        now = time.time()
        on = server.compute({"answerable": True, "reported_at": now}, now)
        off = server.compute({"reported_at": now}, now)
        self.assertTrue(on["answerable"])
        self.assertFalse(off["answerable"])
        # reply_enabled mirrors whether a PIN is configured server-side
        self.assertEqual(on["reply_enabled"], bool(server.ACCESS_PIN))

    def test_wait_age_parsing(self):
        now = time.time()
        iso = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
        age = server._wait_age_secs(iso, now)
        self.assertTrue(560 < age < 640)
        self.assertIsNone(server._wait_age_secs("not-a-date", now))
        self.assertIsNone(server._wait_age_secs(None, now))


class HostHealthTests(unittest.TestCase):
    """P4: per-host reporting-health classification + auth-rejection tracking (server-side policy)."""

    def setUp(self):
        self._clear()

    def tearDown(self):
        self._clear()   # don't leak _state into other test classes (e.g. the beacon integration test)

    @staticmethod
    def _clear():
        with server._lock:
            server._hosts.clear()
            server._rejected.clear()
            server._state.clear()

    def test_classify_green_amber_red_by_age(self):
        now = time.time()
        server.record_host_ok("h-green", now=now)
        server.record_host_ok("h-amber", now=now - (server.HOST_FRESH_SECS + 5))
        server.record_host_ok("h-red",   now=now - (server.HOST_STALE_SECS + 5))
        byhost = {x["host"]: x for x in server.host_health(now)["hosts"]}
        self.assertEqual(byhost["h-green"]["status"], "green")
        self.assertEqual(byhost["h-amber"]["status"], "amber")
        self.assertEqual(byhost["h-red"]["status"], "red")

    def test_red_sorts_first(self):
        now = time.time()
        server.record_host_ok("a-green", now=now)
        server.record_host_ok("z-red", now=now - (server.HOST_STALE_SECS + 5))
        self.assertEqual(server.host_health(now)["hosts"][0]["host"], "z-red")  # most urgent first

    def test_session_counts_attach_to_host(self):
        now = time.time()
        with server._lock:
            server._state["s1"] = {"host": "hx"}
            server._state["s2"] = {"host": "hx"}
        server.record_host_ok("hx", now=now)
        self.assertEqual(server.host_health(now)["hosts"][0]["sessions"], 2)

    def test_idle_host_with_zero_sessions_still_reports(self):
        # The whole point of the beacon: a healthy host with no sessions is GREEN, not invisible.
        now = time.time()
        server.record_host_ok("idle", source="beacon", now=now)
        out = server.host_health(now)
        self.assertEqual(out["hosts"][0]["status"], "green")
        self.assertEqual(out["hosts"][0]["sessions"], 0)

    def test_rejected_tracked_by_ip_and_windowed(self):
        now = time.time()
        server.record_rejected("10.0.0.9", now=now)
        server.record_rejected("10.0.0.9", now=now)
        rej = server.host_health(now)["rejected"]
        self.assertEqual(rej[0]["ip"], "10.0.0.9")
        self.assertEqual(rej[0]["count"], 2)
        # old rejections age out of the window (no stale red alarm forever)
        self.assertEqual(server.host_health(now + server.REJECT_WINDOW_SECS + 1)["rejected"], [])

    def test_record_host_ok_ignores_blank_host(self):
        server.record_host_ok("")
        server.record_host_ok(None)
        self.assertEqual(server.host_health()["hosts"], [])


class IngestAuthIntegrationTests(unittest.TestCase):
    """Cross-boundary guard: a REAL watcher.post() against a token-gated server.Handler.

    This is the path that 401'd on my-laptop and that NO test crossed before: the demo host
    runs CC_LOCAL_SCAN and never hits /ingest, and the unit tests checked each side alone.
    Here we stand the server on an ephemeral port, POST through it, and assert the snapshot
    lands only when the watcher sends a matching token."""

    def setUp(self):
        from http.server import ThreadingHTTPServer
        import threading

        class _QuietHandler(server.Handler):
            def log_message(self, *a):  # silence per-request stderr noise
                pass

        self._orig_server_token = server.INGEST_TOKEN
        self._orig_watcher_token = watcher.INGEST_TOKEN
        self._orig_url = watcher.COLLECTOR_URL
        server.INGEST_TOKEN = "tok-integration"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
        port = self.httpd.server_address[1]
        watcher.COLLECTOR_URL = f"http://127.0.0.1:{port}/ingest"
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.INGEST_TOKEN = self._orig_server_token
        watcher.INGEST_TOKEN = self._orig_watcher_token
        watcher.COLLECTOR_URL = self._orig_url
        with server._lock:
            server._state.clear()
            server._hosts.clear()
            server._rejected.clear()

    def test_matching_token_lands_snapshot(self):
        watcher.INGEST_TOKEN = "tok-integration"
        watcher.post({"session_id": "sess-int-ok", "host": "h", "sessions": []})
        with server._lock:
            self.assertIn("sess-int-ok", server._state)

    def test_missing_token_is_rejected(self):
        watcher.INGEST_TOKEN = ""  # tokenless watcher (the my-laptop 401 case)
        watcher.post({"session_id": "sess-int-401", "host": "h", "sessions": []})
        with server._lock:
            self.assertNotIn("sess-int-401", server._state)

    def test_beacon_records_host_health_without_a_session(self):
        # A zero-session watcher still heartbeats so the dashboard knows it's alive (P4).
        watcher.INGEST_TOKEN = "tok-integration"
        watcher.post({"host": "idle-host", "beacon": True})
        out = server.host_health()
        hosts = {h["host"]: h for h in out["hosts"]}
        self.assertIn("idle-host", hosts)
        self.assertEqual(hosts["idle-host"]["status"], "green")
        with server._lock:                          # a beacon must NOT create a phantom session
            self.assertEqual(len(server._state), 0)

    def test_wrong_token_post_is_recorded_as_rejected_not_healthy(self):
        # The P1 case: a misconfigured watcher (wrong token) must surface as a rejection, never as healthy.
        watcher.INGEST_TOKEN = "wrong-token"
        watcher.post({"host": "bad-host", "beacon": True})
        out = server.host_health()
        self.assertTrue(out["rejected"], "a wrong-token POST should be counted as rejected")
        self.assertNotIn("bad-host", {h["host"] for h in out["hosts"]})  # never marked healthy


class ReplyQueueTests(unittest.TestCase):
    def setUp(self):
        server._outbox.clear()

    def test_enqueue_claim_complete_cycle(self):
        rec = server.enqueue_reply("my-desktop", "sid-1", "continue")
        self.assertEqual(rec["status"], "queued")
        claimed = server.claim_reply("my-desktop", timeout=1)
        self.assertEqual(claimed["reply_id"], rec["reply_id"])
        self.assertEqual(claimed["status"], "dispatched")     # marked so a 2nd responder won't double-inject
        self.assertEqual(claimed["text"], "continue")
        self.assertTrue(server.complete_reply(rec["reply_id"], {"ok": True, "target": "%3"}))
        self.assertEqual(server.wait_reply_result(rec, timeout=1), {"ok": True, "target": "%3"})

    def test_claim_ignores_other_hosts(self):
        server.enqueue_reply("my-laptop", "sid-x", "hi")
        self.assertIsNone(server.claim_reply("my-desktop", timeout=0.2))  # different host -> nothing

    def test_wait_times_out_and_expires(self):
        rec = server.enqueue_reply("h", "s", "t")
        self.assertIsNone(server.wait_reply_result(rec, timeout=0.2))  # no responder
        self.assertNotIn(rec["reply_id"], server._outbox)              # expired & dropped
        self.assertEqual(rec["status"], "expired")

    def test_complete_unknown_reply_is_false(self):
        self.assertFalse(server.complete_reply("nope", {"ok": True}))

    def test_full_async_roundtrip(self):
        """Enqueue, then a background 'responder' claims + completes; the blocking wait returns the result."""
        rec = server.enqueue_reply("hostA", "sidA", "yes")

        def responder():
            c = server.claim_reply("hostA", timeout=2)
            server.complete_reply(c["reply_id"], {"ok": True, "detail": "delivered", "target": "%1"})

        import threading as _t
        t = _t.Thread(target=responder)
        t.start()
        res = server.wait_reply_result(rec, timeout=2)
        t.join()
        self.assertEqual(res["detail"], "delivered")


class CostingTests(unittest.TestCase):
    def test_normalize_strips_marker_and_date(self):
        self.assertEqual(costing.normalize("claude-opus-4-8[1m]"), "claude-opus-4-8")
        self.assertEqual(costing.normalize("claude-opus-4-8-20260115"), "claude-opus-4-8")
        self.assertEqual(costing.normalize("Claude.Opus@4-8"), "claude-opus-4-8")

    def test_rate_resolution_exact_prefix_synthetic(self):
        self.assertIsNotNone(costing.rate_for("claude-opus-4-8"))
        self.assertIsNotNone(costing.rate_for("claude-opus-4-8-20260115"))   # dated → family
        self.assertIsNone(costing.rate_for("<synthetic>"))
        self.assertIsNone(costing.rate_for(""))
        self.assertIsNone(costing.rate_for(None))

    def test_line_cost_uses_four_distinct_rates(self):
        u = {"input_tokens": 2, "cache_creation_input_tokens": 4287,
             "cache_read_input_tokens": 271700, "output_tokens": 415}
        # 2*5e-6 + 4287*6.25e-6 + 271700*0.5e-6 + 415*25e-6
        expect = 2*5e-6 + 4287*6.25e-6 + 271700*0.5e-6 + 415*25e-6
        self.assertAlmostEqual(costing.line_cost(u, "claude-opus-4-8"), expect, places=9)

    def test_cache_not_priced_at_input_rate(self):
        # 1000 cache-read tokens must cost 10% of 1000 input tokens, not the same.
        cr = costing.line_cost({"cache_read_input_tokens": 1000}, "claude-opus-4-8")
        inp = costing.line_cost({"input_tokens": 1000}, "claude-opus-4-8")
        self.assertAlmostEqual(cr, inp * 0.1, places=9)

    def test_unknown_model_is_zero(self):
        self.assertEqual(costing.line_cost({"input_tokens": 999}, "gpt-4"), 0.0)
        self.assertEqual(costing.line_cost({"input_tokens": 999}, None), 0.0)

    def test_tiered_helper(self):
        self.assertEqual(costing.tiered(100_000, 3e-6, 6e-6), 100_000*3e-6)              # below tier → base
        self.assertAlmostEqual(costing.tiered(300_000, 3e-6, 6e-6),
                               200_000*3e-6 + 100_000*6e-6, places=9)                    # split at 200k

    def test_reprice_under_alternate_model(self):
        # V11: reprice the SAME tokens under different rates → Sonnet cheaper than Opus, by the rate delta
        kw = dict(input_tokens=1000, output_tokens=1000, cache_read=0, cache_creation=0)
        self.assertAlmostEqual(costing.reprice("claude-opus-4-8", **kw), 1000*5e-6 + 1000*25e-6, places=9)
        self.assertAlmostEqual(costing.reprice("claude-sonnet-4-6", **kw), 1000*3e-6 + 1000*15e-6, places=9)
        self.assertEqual(costing.reprice("<synthetic>", **kw), 0.0)                      # unknown → $0

    def test_current_model_lineup_known(self):
        # the app must price every CURRENT model (incl. recently-added Fable/Mythos) — not $0, not legacy
        kw = dict(input_tokens=1000, output_tokens=1000)
        self.assertAlmostEqual(costing.reprice("claude-fable-5", **kw),  1000*10e-6 + 1000*50e-6, places=9)  # $10/$50
        self.assertAlmostEqual(costing.reprice("claude-mythos-5", **kw), 1000*10e-6 + 1000*50e-6, places=9)
        self.assertAlmostEqual(costing.reprice("claude-sonnet-4-5", **kw), 1000*3e-6 + 1000*15e-6, places=9)  # explicit, not lucky-prefix
        self.assertAlmostEqual(costing.reprice("claude-opus-4-1", **kw), 1000*15e-6 + 1000*75e-6, places=9)   # deprecated $15/$75
        # families resolve, incl. the new ones
        self.assertEqual(costing.family_of("claude-fable-5"), "fable")
        self.assertEqual(costing.family_of("claude-mythos-5"), "mythos")
        self.assertEqual(store.model_family("claude-fable-5"), "fable")

    def test_unknown_version_falls_back_to_family_not_legacy_or_zero(self):
        # a FUTURE/unseen version must price at its family's CURRENT rate — never silently $0, never the
        # stale legacy rate (the old greedy-prefix bug would have priced opus-4-9 at legacy $15/$75)
        f = costing.reprice("claude-opus-4-9", input_tokens=1000)        # unseen future Opus
        self.assertAlmostEqual(f, 1000*5e-6, places=9)                   # → current Opus $5, NOT legacy $15
        self.assertAlmostEqual(costing.reprice("claude-haiku-9-9", input_tokens=1000), 1000*1e-6, places=9)
        # Bedrock/Vertex-prefixed id classifies + prices via family too
        self.assertAlmostEqual(costing.reprice("anthropic.claude-sonnet-4-6", input_tokens=1000), 1000*3e-6, places=9)
        self.assertEqual(costing.reprice("gpt-4", input_tokens=1000), 0.0)   # truly unknown family → $0


def _usage_line(mid, rid, sid, ts_iso, ctx_read, model="claude-opus-4-8", side=False):
    return {"type": "assistant", "isSidechain": side, "requestId": rid, "sessionId": sid,
            "timestamp": ts_iso, "message": {"id": mid, "model": model,
            "usage": {"input_tokens": 5, "cache_creation_input_tokens": 100,
                      "cache_read_input_tokens": ctx_read, "output_tokens": 50}}}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.s = store.Store(os.path.join(self.dir, "u.db"))

    def tearDown(self):
        self.s.close()

    def test_session_prefs_hide_unhide_and_partial_update(self):
        self.assertEqual(self.s.hidden_sessions(), set())
        self.assertTrue(self.s.set_session_pref("sid-1", hidden=True))
        self.assertEqual(self.s.hidden_sessions(), {"sid-1"})
        # partial update: setting nickname only must NOT clear hidden
        self.s.set_session_pref("sid-1", nickname="my run")
        self.assertEqual(self.s.hidden_sessions(), {"sid-1"})
        self.assertEqual(self.s.session_prefs_all()["sid-1"],
                         {"hidden": True, "nickname": "my run", "color": None})
        # un-hide; nickname preserved
        self.s.set_session_pref("sid-1", hidden=False)
        self.assertEqual(self.s.hidden_sessions(), set())
        self.assertEqual(self.s.session_prefs_all()["sid-1"]["nickname"], "my run")
        self.assertFalse(self.s.set_session_pref(None, hidden=True))   # no session_id → no-op

    def test_top_sessions_ranks_by_cost_and_counts_all_token_kinds(self):
        now = time.time()
        def row(mid, sid, cost, inp=0, out=0, cr=0, cc=0, side=0, host="h1", proj="p1"):
            return {"message_id": mid, "request_id": mid, "session_id": sid, "ts": now, "cost_usd": cost,
                    "input_tokens": inp, "output_tokens": out, "cache_read": cr, "cache_creation": cc,
                    "is_sidechain": side, "host": host, "project": proj}
        self.s.record_many([
            row("a1", "cheap", 1.0, inp=10, out=5),
            row("b1", "rich",  5.0, inp=10, out=5, cr=1000, cc=500),
            row("b2", "rich",  4.0, inp=10, out=5, side=1),          # sidechain: billed, so it COUNTS
            row("c1", "mid",   3.0, inp=1,  out=1),
        ])
        top = self.s.top_sessions(10)
        self.assertEqual([t["session_id"] for t in top], ["rich", "mid", "cheap"])   # cost-descending
        rich = top[0]
        self.assertAlmostEqual(rich["cost_usd"], 9.0, places=6)      # sidechain spend included
        self.assertEqual(rich["messages"], 2)
        # tokens must include cache_read + cache_creation, not just input+output (the session_total outlier)
        self.assertEqual(rich["tokens"], (10 + 5 + 1000 + 500) + (10 + 5))
        self.assertEqual(rich["host"], "h1")
        self.assertEqual(rich["project"], "p1")
        # limit is honored and coerced
        self.assertEqual(len(self.s.top_sessions(2)), 2)
        self.assertEqual(len(self.s.top_sessions(0)), 3)             # 0 → falls back to the default, not empty

    def test_session_color_set_preserve_and_clear(self):
        self.assertEqual(self.s.session_colors(), {})
        self.s.set_session_pref("sid-c", color="cyan")
        self.assertEqual(self.s.session_colors(), {"sid-c": "cyan"})
        # a partial update on another field must NOT clear the color
        self.s.set_session_pref("sid-c", hidden=True)
        self.assertEqual(self.s.session_colors(), {"sid-c": "cyan"})
        self.assertEqual(self.s.hidden_sessions(), {"sid-c"})
        # ...and setting a color must not clear hidden
        self.s.set_session_pref("sid-c", color="pink")
        self.assertEqual(self.s.session_colors(), {"sid-c": "pink"})
        self.assertEqual(self.s.hidden_sessions(), {"sid-c"})
        # "" clears back to the derived hue; None would have meant "leave alone"
        self.s.set_session_pref("sid-c", color="")
        self.assertEqual(self.s.session_colors(), {})
        self.assertEqual(self.s.session_prefs_all()["sid-c"]["color"], None)
        self.assertEqual(self.s.hidden_sessions(), {"sid-c"})           # still preserved

    def test_session_context_series_ordered_excludes_sidechain(self):
        now = time.time()
        rows = [{"message_id": "m%d" % i, "request_id": "r%d" % i, "session_id": "sX",
                 "ts": now + i, "context_tokens": 1000 * i, "is_sidechain": 0} for i in range(1, 4)]
        rows.append({"message_id": "ms", "request_id": "rs", "session_id": "sX",
                     "ts": now + 9, "context_tokens": 999999, "is_sidechain": 1})   # subagent → excluded
        self.s.record_many(rows)
        ser = self.s.session_context_series("sX")
        self.assertEqual([p["ctx"] for p in ser], [1000, 2000, 3000])   # ordered + sidechain dropped
        self.assertEqual(self.s.session_context_series(""), [])

    def test_extract_events_idempotent_and_counts(self):
        lines = [
            json.dumps({"uuid": "u1", "sessionId": "S", "timestamp": "2026-06-06T07:21:14Z", "type": "system",
                        "compactMetadata": {"trigger": "manual", "preTokens": 1000}}),
            json.dumps({"uuid": "u2", "sessionId": "S", "timestamp": "2026-06-06T07:22:00Z", "type": "assistant",
                        "message": {"content": [{"type": "tool_use", "name": "Skill", "id": "t1", "input": {"skill": "cc-verify"}},
                                                {"type": "tool_use", "name": "Agent", "id": "t2", "input": {"subagent_type": "x"}},
                                                {"type": "tool_use", "name": "Read", "id": "t3", "input": {}}]}}),
            json.dumps({"uuid": "u3", "sessionId": "S", "timestamp": "2026-06-06T07:23:00Z", "type": "user",
                        "message": {"content": [{"type": "tool_result", "is_error": True, "tool_use_id": "t9"}]}}),
        ]
        self.assertEqual(self.s.ingest_event_lines(lines, host="h"), 4)   # Read tool excluded
        self.assertEqual(self.s.ingest_event_lines(lines, host="h"), 0)   # re-tail → idempotent
        c = self.s.event_counts("S")
        self.assertEqual((c.get("compaction"), c.get("compaction_manual")), (1, 1))
        self.assertEqual((c.get("skill_use"), c.get("subagent_spawn"), c.get("tool_error")), (1, 1, 1))
        self.assertEqual(self.s.event_counts(""), {})

    def test_event_totals_and_meta_flag(self):
        lines = [json.dumps({"uuid": "u%d" % i, "sessionId": "S", "timestamp": "2026-06-06T07:0%d:00Z" % i,
                             "compactMetadata": {"trigger": "manual" if i < 4 else "auto"}}) for i in range(1, 6)]
        self.s.ingest_event_lines(lines)
        t = self.s.event_totals()
        self.assertEqual((t.get("compaction"), t.get("compaction_manual"), t.get("compaction_auto")), (5, 3, 2))
        self.assertEqual(self.s.events_count(), 5)
        self.assertIsNone(self.s.get_meta("flag"))
        self.s.set_meta("flag", "done")
        self.assertEqual(self.s.get_meta("flag"), "done")

    def test_zone_time_buckets_and_healthy_pct(self):
        # one sample per dumb-zone band (matching ZONE_TOK_*); a sidechain row must be excluded
        ctxs = [10_000, 60_000, 150_000, 250_000]            # sharp, good, drift, danger
        rows = [{"message_id": "z%d" % i, "request_id": "r%d" % i, "session_id": "sZ", "account": "acct1",
                 "ts": time.time() + i, "context_tokens": c, "is_sidechain": 0} for i, c in enumerate(ctxs)]
        rows.append({"message_id": "zs", "request_id": "rs", "session_id": "sZ", "account": "acct1",
                     "ts": time.time(), "context_tokens": 300_000, "is_sidechain": 1})  # excluded
        self.s.record_many(rows)
        z = self.s.zone_time()
        self.assertEqual((z["sharp"], z["good"], z["drift"], z["danger"], z["total"]), (1, 1, 1, 1, 4))
        self.assertEqual(z["healthy_pct"], 50.0)             # (sharp+good)/total = 2/4
        self.assertEqual(self.s.zone_time(account="nobody")["healthy_pct"], None)
        self.assertEqual(self.s.zone_time(account="acct1")["total"], 4)

    def test_trophy_rollups_totals_breakdown_and_streak(self):
        # V10 Slice 1: all_time_totals + cost_breakdown + active_days_streak (+ the server's cache-savings math)
        import costing
        now = time.time()
        rows = [
            {"message_id": "a", "request_id": "1", "session_id": "s1", "ts": now, "model": "claude-opus-4-8",
             "input_tokens": 100, "output_tokens": 50, "cache_read": 1000, "cache_creation": 20,
             "context_tokens": 5000, "cost_usd": 0.10, "is_sidechain": 0},
            {"message_id": "b", "request_id": "2", "session_id": "s1", "ts": now - 86400, "model": "claude-sonnet-4-6",
             "input_tokens": 10, "output_tokens": 5, "cache_read": 500, "cache_creation": 0,
             "context_tokens": 3000, "cost_usd": 0.01, "is_sidechain": 0},
            {"message_id": "c", "request_id": "3", "session_id": "s2", "ts": now - 2 * 86400, "model": "claude-opus-4-8",
             "input_tokens": 7, "output_tokens": 3, "cache_read": 0, "cache_creation": 0,
             "context_tokens": 2000, "cost_usd": 0.02, "is_sidechain": 0},
            {"message_id": "d", "request_id": "4", "session_id": "s2", "ts": now - 5 * 86400, "model": "claude-opus-4-8",
             "input_tokens": 1, "output_tokens": 1, "cache_read": 0, "cache_creation": 0,
             "context_tokens": 1000, "cost_usd": 0.03, "is_sidechain": 0},
        ]
        self.s.record_many(rows)
        at = self.s.all_time_totals()
        self.assertEqual((at["messages"], at["sessions"]), (4, 2))
        self.assertAlmostEqual(at["cost_usd"], 0.16, places=6)
        self.assertEqual(at["tokens"], (100 + 50 + 1000 + 20) + (10 + 5 + 500) + (7 + 3) + (1 + 1))
        bd = self.s.cost_breakdown()
        self.assertEqual((bd["totals"]["input"], bd["totals"]["cache_read"]), (118, 1500))
        # cache-savings = Σ cache_read × (input_rate − cache_read_rate) per model — same formula server _report uses
        savings = sum(b["cache_read"] * (costing.rate_for(b["model"])["input"] - costing.rate_for(b["model"])["cache_read"])
                      for b in bd["by_model"] if costing.rate_for(b["model"]))
        self.assertAlmostEqual(savings, 1000 * (5e-6 - 0.5e-6) + 500 * (3e-6 - 0.3e-6), places=9)
        st = self.s.active_days_streak()
        # rows on today, -1, -2 (consecutive) then a gap to -5 → current 3 (ends today), longest 3, 4 active days
        self.assertEqual((st["active_days"], st["current"], st["longest"]), (4, 3, 3))
        # daily_totals now carries a `tokens` column (Slice 2 heatmap substrate): 4 day-rows, today's = 1170
        daily = self.s.daily_totals(365)
        self.assertEqual(len(daily), 4)
        self.assertTrue(all("tokens" in d for d in daily))
        self.assertEqual(daily[0]["tokens"], 100 + 50 + 1000 + 20)   # ORDER BY d DESC → today first

    def test_active_days_streak_empty(self):
        self.assertEqual(self.s.active_days_streak(), {"current": 0, "longest": 0, "active_days": 0})

    def test_model_savings_policies(self):
        # V11 Slice 1: what-if downgrade savings per policy over non-sidechain rows
        now = time.time()
        def row(mid, model, out, side=0):   # large input so per-policy savings clear the 2-decimal cent rounding
            return {"message_id": mid, "request_id": mid, "session_id": "s", "ts": now, "model": model,
                    "input_tokens": 1_000_000, "output_tokens": out, "cache_read": 0, "cache_creation": 0, "is_sidechain": side}
        self.s.record_many([
            row("a", "claude-opus-4-8", 50),       # opus + short(<600) + trivial(<120)
            row("b", "claude-opus-4-8", 2000),     # opus only (long)
            row("c", "claude-sonnet-4-6", 30),     # sonnet + trivial(<120)
            row("d", "claude-haiku-4-5", 10),      # haiku — in no downgrade policy
            row("e", "claude-opus-4-8", 40, side=1),  # sidechain → excluded entirely
        ])
        ms = self.s.model_savings(days=30)
        self.assertTrue(ms["estimate"])                          # honesty flag present
        self.assertEqual(ms["considered"], 4)                   # sidechain excluded
        pol = {p["id"]: p for p in ms["policies"]}
        self.assertEqual(pol["all_opus_sonnet"]["turns"], 2)    # both opus rows
        self.assertEqual(pol["short_opus_sonnet"]["turns"], 1)  # only the short opus row
        self.assertEqual(pol["trivial_haiku"]["turns"], 2)      # short opus + small sonnet
        self.assertTrue(all(pol[k]["saved_usd"] > 0 for k in pol))   # every downgrade saves (cheaper rates)

    def test_model_savings_breakdown_by_project_and_session(self):
        # the short-Opus→Sonnet opportunity grouped by project + session (the "where to right-size" view)
        now = time.time()
        def row(mid, proj, sess, out, model="claude-opus-4-8", side=0):
            return {"message_id": mid, "request_id": mid, "session_id": sess, "project": proj, "ts": now,
                    "model": model, "input_tokens": 1_000_000, "output_tokens": out,
                    "cache_read": 0, "cache_creation": 0, "is_sidechain": side}
        self.s.record_many([
            row("a", "projA", "s1", 50),                 # short opus → counts (projA/s1)
            row("b", "projA", "s1", 80),                 # short opus → counts (projA/s1 again)
            row("c", "projB", "s2", 40),                 # short opus → counts (projB/s2)
            row("d", "projA", "s3", 2000),               # long opus → excluded (not low-output)
            row("e", "projB", "s2", 30, model="claude-sonnet-4-6"),  # not opus → excluded
            row("f", "projA", "s1", 60, side=1),         # sidechain → excluded
        ])
        bd = self.s.model_savings_breakdown(days=30)
        self.assertTrue(bd["estimate"] and bd["policy"] == "short_opus_sonnet")
        by_p = {x["project"]: x for x in bd["by_project"]}
        self.assertEqual(set(by_p), {"projA", "projB"})
        self.assertEqual(by_p["projA"]["turns"], 2)             # rows a,b only (c is projB, d/e/f excluded)
        self.assertEqual(by_p["projB"]["turns"], 1)             # row c only
        self.assertEqual(bd["by_project"][0]["project"], "projA")  # sorted by saved_usd desc (projA bigger)
        by_s = {x["session_id"]: x for x in bd["by_session"]}
        self.assertEqual(by_s["s1"]["turns"], 2)
        self.assertEqual(by_s["s1"]["project"], "projA")
        self.assertTrue(all(x["saved_usd"] > 0 for x in bd["by_project"]))

    def test_savings_counts_fable_premium_tier(self):
        # Fable ($10/$50) is pricier than Opus, so short low-output Fable turns are ALSO a Sonnet-downgrade
        # opportunity — the savings logic must not gate on Opus alone.
        now = time.time()
        def row(mid, proj, out, model):
            return {"message_id": mid, "request_id": mid, "session_id": "s", "project": proj, "ts": now,
                    "model": model, "input_tokens": 1_000_000, "output_tokens": out,
                    "cache_read": 0, "cache_creation": 0, "is_sidechain": 0}
        self.s.record_many([
            row("f1", "projF", 40, "claude-fable-5"),     # short Fable → must count toward Sonnet downgrade
            row("o1", "projO", 40, "claude-opus-4-8"),    # short Opus → counts (as before)
            row("h1", "projH", 40, "claude-haiku-4-5"),   # Haiku → never a downgrade candidate
        ])
        ms = self.s.model_savings(days=30)
        pol = {p["id"]: p for p in ms["policies"]}
        self.assertEqual(pol["short_opus_sonnet"]["turns"], 2)   # Fable + Opus, NOT Haiku
        bd = self.s.model_savings_breakdown(days=30)
        projs = {x["project"] for x in bd["by_project"]}
        self.assertIn("projF", projs)                            # Fable opportunity surfaces by project
        self.assertNotIn("projH", projs)                         # Haiku does not

    def test_plan_prices_roundtrip_and_clear(self):
        # V11 Slice 2: per-account monthly plan price stored as one JSON meta blob; ≤0/None/blank clears
        self.assertEqual(self.s.get_plan_prices(), {})
        self.assertTrue(self.s.set_plan_price("me@x.com", 20))
        self.assertTrue(self.s.set_plan_price("work@y.com", "100"))   # numeric string accepted
        self.assertEqual(self.s.get_plan_prices(), {"me@x.com": 20.0, "work@y.com": 100.0})
        self.s.set_plan_price("me@x.com", 0)                          # 0 → clear
        self.assertEqual(self.s.get_plan_prices(), {"work@y.com": 100.0})
        self.s.set_plan_price("work@y.com", None)                     # None → clear
        self.assertEqual(self.s.get_plan_prices(), {})
        self.assertFalse(self.s.set_plan_price("", 10))               # no account → no-op
        self.assertFalse(self.s.set_plan_price("a@b.com", "notanumber"))

    def test_cost_by_account_month_window(self):
        now = time.time()
        self.s.record_many([
            {"message_id": "m1", "request_id": "1", "session_id": "s", "account": "a@x.com", "ts": now,
             "cost_usd": 5.0, "input_tokens": 1, "is_sidechain": 0},
            {"message_id": "m2", "request_id": "2", "session_id": "s", "account": "a@x.com", "ts": now - 40 * 86400,
             "cost_usd": 9.0, "input_tokens": 1, "is_sidechain": 0},   # outside 30d window
        ])
        month = {r["account"]: r["cost_usd"] for r in self.s.cost_by_account("month")}
        self.assertAlmostEqual(month["a@x.com"], 5.0, places=6)        # 40-day-old row excluded
        total = {r["account"]: r["cost_usd"] for r in self.s.cost_by_account("total")}
        self.assertAlmostEqual(total["a@x.com"], 14.0, places=6)       # total includes both

    def test_cap_usage_from_rate_history(self):
        # V11 Slice 3: avg/peak + cap_hits (# distinct reset-windows that maxed out >= hit_pct)
        now = time.time()
        self.s.record_rate_point("a@x.com", "five_hour", 50, 1000, ts=now - 300, min_gap=0)
        self.s.record_rate_point("a@x.com", "five_hour", 100, 1000, ts=now - 200, min_gap=0)  # window 1000 maxes
        self.s.record_rate_point("a@x.com", "five_hour", 30, 2000, ts=now - 100, min_gap=0)   # window 2000 doesn't
        cu = self.s.cap_usage("a@x.com", 30)
        fh = cu["five_hour"]
        self.assertEqual((fh["peak"], fh["cap_hits"], fh["samples"]), (100.0, 1, 3))
        self.assertAlmostEqual(fh["avg"], 60.0, places=1)              # (50+100+30)/3
        self.assertNotIn("seven_day", cu)                             # no 7d readings → omitted
        self.assertEqual(self.s.cap_usage("nobody", 30), {})         # no history → empty

    def test_mcp_report_roundtrip_and_upsert(self):
        # MCP tax Slice 2: latest report per host (upsert); junk/no-name dropped; no host → 0
        self.assertEqual(self.s.mcp_reports(), {})
        n = self.s.record_mcp_report("hostA", [{"name": "github", "status": "connected"},
                                               {"name": "jira", "status": "failed"}], ts=1000)
        self.assertEqual(n, 2)
        rep = self.s.mcp_reports()["hostA"]
        self.assertEqual(rep["ts"], 1000)
        self.assertEqual({s["name"] for s in rep["servers"]}, {"github", "jira"})
        # a newer report for the same host REPLACES (one row per host)
        self.s.record_mcp_report("hostA", [{"name": "playwright", "status": "connected"}], ts=2000)
        rep = self.s.mcp_reports()["hostA"]
        self.assertEqual((rep["ts"], [s["name"] for s in rep["servers"]]), (2000, ["playwright"]))
        # entries without a name are dropped; an empty host is a no-op
        self.assertEqual(self.s.record_mcp_report("hostB", [{"status": "x"}, "bad", {"name": "ok", "status": "connected"}]), 1)
        self.assertEqual(self.s.record_mcp_report("", [{"name": "x"}]), 0)

    def test_mcp_call_events_extracted_and_rolled_up(self):
        # MCP event backfill: mcp__server__tool tool_use → mcp_call events → over-time usage / dead-weight
        def line(uuid, sid, name, tid, when):
            return json.dumps({"uuid": uuid, "sessionId": sid, "timestamp": when,
                               "message": {"content": [{"type": "tool_use", "name": name, "id": tid, "input": {}}]}})
        # within the days=30 window this test queries — see _iso_ago()
        ta, tb, tc, td = _iso_ago(3600), _iso_ago(3540), _iso_ago(3480), _iso_ago(3420)
        # extract_events recognizes the mcp_call type with {server, tool}
        evs = store.extract_events(json.loads(line("u1", "s1", "mcp__github__create_issue", "t1", ta)))
        self.assertEqual([e["type"] for e in evs], ["mcp_call"])
        self.assertEqual(json.loads(evs[0]["payload_json"]), {"server": "github", "tool": "create_issue"})
        # ingest a few (github x2, filesystem x1) on this host; a plain Read is NOT an mcp_call
        self.s.ingest_event_lines([
            line("a", "s1", "mcp__github__create_issue", "t1", ta),
            line("b", "s1", "mcp__github__list_prs", "t2", tb),
            line("c", "s2", "mcp__filesystem__read_file", "t3", tc),
            line("d", "s2", "Read", "t4", td),
        ], host="hostA")
        used = self.s.mcp_servers_used(days=30)["hostA"]
        self.assertEqual(set(used), {"github", "filesystem"})        # Read excluded
        self.assertEqual(used["github"]["calls"], 2)
        self.assertEqual(used["filesystem"]["calls"], 1)
        self.assertIsNotNone(used["github"]["last_ts"])
        # idempotent: re-ingesting the same line inserts nothing new
        self.assertEqual(self.s.ingest_event_lines([
            line("a", "s1", "mcp__github__create_issue", "t1", ta)], host="hostA"), 0)
        # the rolling window is a real filter, not an accident of the fixture dates: an event from 45 days
        # ago is excluded at days=30 but present with no window. (Asserting this explicitly is what keeps a
        # future fixture from drifting out of the window and failing for a reason unrelated to the code.)
        self.s.ingest_event_lines([
            line("old", "s3", "mcp__legacy__ping", "t9", _iso_ago(45 * 86400))], host="hostA")
        self.assertNotIn("legacy", self.s.mcp_servers_used(days=30)["hostA"])
        self.assertIn("legacy", self.s.mcp_servers_used(days=0)["hostA"])

    def test_prefix_overhead_first_turn_and_stats(self):
        # MCP tax Slice 3: context floor = first-turn cache_creation+cache_read (real billed), per session
        now = time.time()
        def row(mid, sid, ts, cc, cr, side=0):
            return {"message_id": mid, "request_id": mid, "session_id": sid, "ts": ts,
                    "cache_creation": cc, "cache_read": cr, "input_tokens": 1, "is_sidechain": side}
        self.s.record_many([
            row("a1", "s1", now - 100, 20000, 10000),      # s1 earliest main turn → prefix 30000
            row("a2", "s1", now - 50, 500, 40000),         # later turn (bigger cache_read) must NOT win
            row("a3", "s1", now - 120, 9999, 1, side=1),   # earlier BUT sidechain → ignored
            row("b1", "s2", now - 200, 5000, 5000),        # s2 earliest → prefix 10000
            row("b2", "s2", now - 150, 100, 40000),
        ])
        self.assertEqual(self.s.session_prefix_overhead("s1"), 30000)   # first main-thread turn, not the later/bigger one
        self.assertEqual(self.s.session_prefix_overhead("s2"), 10000)
        self.assertIsNone(self.s.session_prefix_overhead("nope"))
        st = self.s.prefix_overhead_stats(days=30)
        self.assertEqual(st["sessions"], 2)                            # two distinct sessions
        self.assertEqual(st["max"], 30000)
        self.assertIn(st["median"], (10000, 30000))                   # one of the two first-turn prefixes

    def test_reducible_spend_danger_band_and_auto_compactions(self):
        # V11 Slice 4 (speculative): cost_usd bucketed by dumb-zone band; lead figure = the Danger band only
        now = time.time()
        def row(mid, ctx, cost, side=0):
            return {"message_id": mid, "request_id": mid, "session_id": "s", "ts": now, "context_tokens": ctx,
                    "cost_usd": cost, "input_tokens": 1, "is_sidechain": side}
        self.s.record_many([
            row("a", 10_000, 1.0),    # sharp
            row("b", 80_000, 2.0),    # good
            row("c", 150_000, 4.0),   # drift
            row("d", 250_000, 8.0),   # danger
            row("e", 300_000, 5.0, side=1),  # danger BUT sidechain → excluded
        ])
        # one manual + one auto compaction → only the auto count surfaces in the reducible card
        # timestamps must sit inside the days=30 window queried below — see _iso_ago()
        self.s.ingest_event_lines([json.dumps({"uuid": "z1", "sessionId": "s", "timestamp": _iso_ago(3600),
                                               "compactMetadata": {"trigger": "auto"}}),
                                   json.dumps({"uuid": "z2", "sessionId": "s", "timestamp": _iso_ago(3540),
                                               "compactMetadata": {"trigger": "manual"}})])
        rd = self.s.reducible_spend(days=30)
        self.assertTrue(rd["estimate"])                              # honesty flag present
        self.assertEqual(rd["considered"], 4)                       # sidechain excluded
        self.assertAlmostEqual(rd["total_usd"], 15.0, places=6)     # 1+2+4+8 (no sidechain)
        self.assertAlmostEqual(rd["at_risk_usd"], 8.0, places=6)    # Danger band only
        self.assertAlmostEqual(rd["by_band"]["drift"], 4.0, places=6)
        self.assertAlmostEqual(rd["at_risk_pct"], round(8.0 / 15.0 * 100, 1), places=1)
        self.assertEqual(rd["auto_compactions"], 1)                 # auto only, not the manual one

    def test_hourly_histogram_buckets(self):
        # V10 Slice 4: same instant → one (dow,hour) bucket; a row one day earlier → a second bucket (diff dow)
        now = time.time()
        rows = [{"message_id": "h%d" % i, "request_id": "r%d" % i, "session_id": "s", "ts": now,
                 "input_tokens": 1, "cost_usd": 0.01, "is_sidechain": 0} for i in range(3)]
        rows.append({"message_id": "hx", "request_id": "rx", "session_id": "s", "ts": now - 86400,
                     "input_tokens": 1, "cost_usd": 0.02, "is_sidechain": 0})
        self.s.record_many(rows)
        hh = self.s.hourly_histogram(30)
        self.assertEqual(len(hh), 2)
        self.assertEqual(sum(c["messages"] for c in hh), 4)
        self.assertTrue(all(0 <= c["dow"] <= 6 and 0 <= c["hour"] <= 23 for c in hh))
        big = max(hh, key=lambda c: c["messages"])
        self.assertEqual(big["messages"], 3)
        self.assertAlmostEqual(big["cost_usd"], 0.03, places=6)
        self.assertEqual(self.s.hourly_histogram(30), self.s.hourly_histogram())   # default days=30

    def test_daily_totals_by_model_families(self):
        # V10 Slice 3: per-day message counts grouped by model family (opus/sonnet/haiku/other)
        self.assertEqual((store.model_family("claude-opus-4-8-20260115"), store.model_family("claude-sonnet-4-6"),
                          store.model_family("claude-haiku-4-5"), store.model_family("<synthetic>"),
                          store.model_family(None)), ("opus", "sonnet", "haiku", "other", "other"))
        now = time.time()
        rows = [
            {"message_id": "o1", "request_id": "1", "session_id": "s", "ts": now, "model": "claude-opus-4-8", "is_sidechain": 0},
            {"message_id": "o2", "request_id": "2", "session_id": "s", "ts": now, "model": "claude-opus-4-8", "is_sidechain": 0},
            {"message_id": "sn", "request_id": "3", "session_id": "s", "ts": now, "model": "claude-sonnet-4-6", "is_sidechain": 0},
            {"message_id": "hk", "request_id": "4", "session_id": "s", "ts": now - 86400, "model": "claude-haiku-4-5", "is_sidechain": 0},
        ]
        self.s.record_many(rows)
        mm = self.s.daily_totals_by_model(30)
        self.assertEqual(len(mm), 2)                       # two days
        self.assertEqual(mm[0]["day"] < mm[1]["day"], True)   # ascending by day
        today = mm[-1]
        self.assertEqual((today.get("opus"), today.get("sonnet")), (2, 1))
        self.assertNotIn("haiku", today)                  # absent families omit the key
        self.assertEqual(mm[0].get("haiku"), 1)

    def test_token_economics_cache_ratio_and_mix(self):
        rows = [
            {"message_id": "e1", "request_id": "r1", "session_id": "sE", "account": "a1", "ts": time.time(),
             "model": "claude-opus-4-8", "input_tokens": 100, "cache_creation": 0, "cache_read": 900, "output_tokens": 50},
            {"message_id": "e2", "request_id": "r2", "session_id": "sE", "account": "a1", "ts": time.time(),
             "model": "claude-sonnet-4-6", "input_tokens": 0, "cache_creation": 0, "cache_read": 0, "output_tokens": 10},
        ]
        self.s.record_many(rows)
        te = self.s.token_economics()
        self.assertEqual(te["cache_ratio"], 0.9)             # 900 / (900 + 100 + 0)
        self.assertEqual(te["messages"], 2)
        self.assertEqual(te["model_mix"], {"claude-opus-4-8": 1, "claude-sonnet-4-6": 1})
        self.assertIn("a1", self.s.craft_accounts())

    def test_event_totals_account_filter(self):
        self.s.record_events([
            {"event_id": "ev1", "ts": time.time(), "session_id": "sA", "account": "a1", "type": "skill_use", "ref": "x"},
            {"event_id": "ev2", "ts": time.time(), "session_id": "sB", "account": "a2", "type": "skill_use", "ref": "y"},
        ])
        self.assertEqual(self.s.event_totals().get("skill_use"), 2)
        self.assertEqual(self.s.event_totals(account="a1").get("skill_use"), 1)
        self.assertEqual(self.s.event_totals(account="nobody"), {})

    def test_drift_curve_finds_knee_when_errors_rise_with_context(self):
        now = time.time()
        rows = []
        for i in range(60):   # low-context messages
            rows.append({"message_id": "lo%d" % i, "request_id": "r%d" % i, "session_id": "D",
                         "ts": now + i, "model": "claude-opus-4-8", "context_tokens": 10_000, "is_sidechain": 0})
        for i in range(60):   # high-context (danger band) messages, later in the timeline
            rows.append({"message_id": "hi%d" % i, "request_id": "rh%d" % i, "session_id": "D",
                         "ts": now + 1000 + i, "model": "claude-opus-4-8", "context_tokens": 250_000, "is_sidechain": 0})
        self.s.record_many(rows)
        evs = [{"event_id": "lo_e", "ts": now + 5, "session_id": "D", "type": "tool_error", "ref": "x"}]
        for i in range(18):   # many errors while deep in context
            evs.append({"event_id": "hi_e%d" % i, "ts": now + 1000 + i, "session_id": "D", "type": "tool_error", "ref": "d%d" % i})
        self.s.record_events(evs)
        d = self.s.drift_curve(min_model_msgs=40, min_model_errors=5)
        o = d["overall"]
        self.assertEqual(o["knee_band"], "danger")
        self.assertEqual(o["knee_tokens"], store.ZONE_TOK_DANGER)
        self.assertIn("claude-opus-4-8", d["by_model"])
        # the error at low context maps to the sharp band (nearest preceding message)
        sharp = next(b for b in o["bands"] if b["band"] == "sharp")
        self.assertEqual((sharp["errors"], sharp["messages"]), (1, 60))

    def test_drift_curve_no_knee_when_errors_flat(self):
        now = time.time()
        rows, evs = [], []
        for i in range(40):
            rows.append({"message_id": "s%d" % i, "request_id": "r%d" % i, "session_id": "F",
                         "ts": now + i, "model": "claude-opus-4-8", "context_tokens": 10_000, "is_sidechain": 0})
            rows.append({"message_id": "d%d" % i, "request_id": "rd%d" % i, "session_id": "F",
                         "ts": now + 1000 + i, "model": "claude-opus-4-8", "context_tokens": 250_000, "is_sidechain": 0})
        self.s.record_many(rows)
        for i in range(4):    # equal error rate in both bands → no upward knee
            evs.append({"event_id": "se%d" % i, "ts": now + i, "session_id": "F", "type": "tool_error", "ref": "s%d" % i})
            evs.append({"event_id": "de%d" % i, "ts": now + 1000 + i, "session_id": "F", "type": "tool_error", "ref": "d%d" % i})
        self.s.record_events(evs)
        self.assertIsNone(self.s.drift_curve()["overall"]["knee_band"])

    def test_daily_craft_aggregates_buckets_by_day_and_gates(self):
        # two days: day A has plenty of messages, day B has too few → only A survives the min_msgs gate
        a_ts = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc).timestamp()
        b_ts = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc).timestamp()
        rows = [{"message_id": "a%d" % i, "request_id": "r%d" % i, "session_id": "S", "account": "a1",
                 "ts": a_ts + i, "model": "claude-opus-4-8", "cache_read": 900, "input_tokens": 100,
                 "cache_creation": 0, "context_tokens": 10_000, "is_sidechain": 0} for i in range(60)]
        rows += [{"message_id": "b%d" % i, "request_id": "rb%d" % i, "session_id": "S", "account": "a1",
                  "ts": b_ts + i, "model": "claude-opus-4-8", "cache_read": 1, "input_tokens": 1,
                  "cache_creation": 0, "context_tokens": 10_000, "is_sidechain": 0} for i in range(3)]
        self.s.record_many(rows)
        ser = self.s.daily_craft_aggregates(days=3650, min_msgs=50)
        days = sorted(ser.keys())
        self.assertEqual(len(days), 1)                          # only the busy day qualifies
        agg = ser[days[0]]
        self.assertEqual(agg["messages"], 60)
        self.assertEqual(agg["cache_ratio"], 0.9)              # 900 / (900+100)
        self.assertEqual(agg["healthy_pct"], 100.0)            # all samples in sharp band

    def test_extract_requires_keys_and_tags_sidechain(self):
        ok = store.extract_usage(_usage_line("m1", "r1", "s", "2026-06-04T20:00:00Z", 1000))
        self.assertEqual(ok["message_id"], "m1")
        self.assertEqual(ok["context_tokens"], 5 + 100 + 1000)
        self.assertGreater(ok["cost_usd"], 0)
        self.assertEqual(ok["is_sidechain"], 0)
        # subagent usage IS counted now (it's billed) — tagged is_sidechain=1, not dropped
        sub = store.extract_usage(_usage_line("m", "r", "s", "2026-06-04T20:00:00Z", 1, side=True))
        self.assertIsNotNone(sub)
        self.assertEqual(sub["is_sidechain"], 1)
        self.assertGreater(sub["cost_usd"], 0)
        self.assertIsNone(store.extract_usage({"type": "user"}))
        nomid = _usage_line("m", "r", "s", "t", 1); nomid["message"].pop("id")
        self.assertIsNone(store.extract_usage(nomid))

    def test_subagent_cost_counted_but_excluded_from_compact_eta(self):
        now = time.time()
        # main thread: context climbs 100k→160k; a subagent message also lands (high context, billed)
        self.s.record_many([
            store.extract_usage(_usage_line("a0", "r0", "sx",
                datetime.fromtimestamp(now - 600, timezone.utc).isoformat(), 100_000 - 105)),
            store.extract_usage(_usage_line("a1", "r1", "sx",
                datetime.fromtimestamp(now, timezone.utc).isoformat(), 160_000 - 105)),
            store.extract_usage(_usage_line("sub", "rs", "sx",
                datetime.fromtimestamp(now - 300, timezone.utc).isoformat(), 900_000 - 105, side=True)),
        ])
        # cost INCLUDES the subagent row (billing-accurate)
        self.assertEqual(self.s.session_total("sx")["messages"], 3)
        rows = self.s._all("SELECT COUNT(*) n FROM usage WHERE session_id='sx' AND is_sidechain=1")
        self.assertEqual(rows[0]["n"], 1)
        # compact ETA uses only the main thread's context slope — the 900k subagent point must NOT skew it
        eta = self.s.compact_eta("sx", 160_000, 950_000)
        self.assertIsNotNone(eta)
        self.assertGreater(eta["eta_secs"], 0)   # climbing 100k→160k → finite; not corrupted by the subagent

    def test_insert_or_ignore_is_idempotent(self):
        rows = [store.extract_usage(_usage_line("m1", "r1", "s", "2026-06-04T20:00:00Z", 1000))]
        self.assertEqual(self.s.record_many(rows), 1)
        self.assertEqual(self.s.record_many(rows), 0)            # same (message_id,request_id) → ignored
        self.assertEqual(self.s.session_total("s")["messages"], 1)

    def test_session_and_project_totals(self):
        self.s.record_many([
            store.extract_usage(_usage_line("m1", "r1", "s", "2026-06-04T20:00:00Z", 1000), project="P"),
            store.extract_usage(_usage_line("m2", "r2", "s", "2026-06-04T20:01:00Z", 2000), project="P"),
        ])
        self.assertEqual(self.s.session_total("s")["messages"], 2)
        self.assertAlmostEqual(self.s.session_total("s")["cost_usd"], self.s.total_cost(), places=9)
        self.assertEqual(self.s.project_totals()[0]["project"], "P")

    def test_account_attribution_and_cost_by_account(self):
        iso = datetime.fromtimestamp(time.time(), timezone.utc).isoformat()   # today (local)
        row = store.extract_usage(_usage_line("ma", "ra", "s1", iso, 1000), account="me@x.com")
        self.assertEqual(row["account"], "me@x.com")
        self.s.record_many([
            row,
            store.extract_usage(_usage_line("mb", "rb", "s2", iso, 2000), account="me@x.com"),
            store.extract_usage(_usage_line("mc", "rc", "s3", iso, 500), account="work@y.com"),
        ])
        by = {d["account"]: d for d in self.s.cost_by_account("today")}
        self.assertEqual(by["me@x.com"]["messages"], 2)
        self.assertEqual(by["work@y.com"]["messages"], 1)
        self.assertGreaterEqual(by["me@x.com"]["cost_usd"], by["work@y.com"]["cost_usd"])  # 2 rows ≥ 1
        self.assertEqual(self.s.cost_by_account("today")[0]["account"], "me@x.com")        # sorted by spend

    def test_session_account_map_and_reattribution(self):
        iso = datetime.fromtimestamp(time.time(), timezone.utc).isoformat()
        # usage ingested before we know the account → stays NULL ("(unattributed)")
        self.s.record_many([
            store.extract_usage(_usage_line("m1", "r1", "sA", iso, 1000)),
            store.extract_usage(_usage_line("m2", "r2", "sB", iso, 1000)),
            store.extract_usage(_usage_line("m3", "r3", "sC", iso, 1000)),   # never learned → stays unattributed
        ])
        self.assertIsNone(self.s.account_for_session("sA"))
        # learn accounts per-session, then re-derive every row from the map
        self.s.set_session_account("sA", "alice@x.com", host="h", source="hook")
        self.s.set_session_account("sB", "bob@y.com", host="h", source="host")
        self.s.reattribute_accounts(only_null=False)
        by = {d["account"]: d for d in self.s.cost_by_account("today")}
        self.assertEqual(by["alice@x.com"]["messages"], 1)
        self.assertEqual(by["bob@y.com"]["messages"], 1)
        self.assertIn("(unattributed)", by)                              # sC has no account
        # an EXACT 'hook' value can't be clobbered by a later APPROXIMATE 'host' guess
        self.s.set_session_account("sA", "WRONG@z.com", host="h", source="host")
        self.assertEqual(self.s.account_for_session("sA"), "alice@x.com")
        # a fresh 'hook' value does update
        self.s.set_session_account("sA", "alice2@x.com", host="h", source="hook")
        self.assertEqual(self.s.account_for_session("sA"), "alice2@x.com")
        # cheap incremental pass fills ONLY a newly-learned session's NULL rows (doesn't rewrite others)
        self.s.set_session_account("sC", "carol@x.com", host="h", source="hook")
        self.assertEqual(self.s.reattribute_accounts(only_null=True), 1)   # just sC's one NULL row
        self.assertEqual(self.s.account_for_session("sC"), "carol@x.com")
        # a full pass also rewrites changed attributions (sA's row alice → alice2 from the updated map)
        self.s.reattribute_accounts(only_null=False)
        self.assertEqual({d["account"] for d in self.s.cost_by_account("today")},
                         {"alice2@x.com", "bob@y.com", "carol@x.com"})

    def test_record_remote_usage_attributes_per_session(self):
        self.s.set_session_account("rs1", "work@y.com", host="my-laptop", source="host")
        iso = datetime.fromtimestamp(time.time(), timezone.utc).isoformat()
        events = [{"message_id": "rm1", "request_id": "rr1", "session_id": "rs1", "model": "claude-opus-4-8",
                   "ts": iso, "usage": {"input_tokens": 5, "cache_creation_input_tokens": 100,
                                        "cache_read_input_tokens": 1000, "output_tokens": 50}}]
        self.assertEqual(self.s.record_remote_usage(events, host="my-laptop"), 1)
        self.assertEqual(self.s.record_remote_usage(events, host="my-laptop"), 0)   # dedup by (msg,req)
        by = {d["account"]: d for d in self.s.cost_by_account("today")}
        self.assertIn("work@y.com", by)                          # remote spend attributed to its account
        self.assertGreater(by["work@y.com"]["cost_usd"], 0)      # cost computed server-side
        self.assertEqual(self.s.session_total("rs1")["messages"], 1)

    def test_extra_usage_eta_climbing_flat_and_reset(self):
        now = time.time()
        reset_far = now + 4 * 3600
        # climbing 40%→70% over 30 min → 30% left at 1%/min → ~30 min (1800s) to 100%
        self.s.record_rate_point("acc", "five_hour", 40, reset_far, ts=now - 1800)
        self.s.record_rate_point("acc", "five_hour", 70, reset_far, ts=now)
        eta = self.s.extra_usage_eta("acc", "five_hour", now)
        self.assertIsNotNone(eta)
        self.assertAlmostEqual(eta["eta_secs"], 1800, delta=90)
        self.assertGreater(eta["slope_pct_per_hr"], 0)
        # flat → not climbing → no ETA
        self.s.record_rate_point("flat", "seven_day", 50, now + 10 * 86400, ts=now - 1800)
        self.s.record_rate_point("flat", "seven_day", 50, now + 10 * 86400, ts=now)
        self.assertIsNone(self.s.extra_usage_eta("flat", "seven_day", now))
        # climbing, but the window RESETS before 100% would be hit → no Extra Usage this cycle
        self.s.record_rate_point("soon", "five_hour", 40, now + 600, ts=now - 1800)
        self.s.record_rate_point("soon", "five_hour", 70, now + 600, ts=now)
        self.assertIsNone(self.s.extra_usage_eta("soon", "five_hour", now))   # eta ~1800s > 600s to reset

    def test_extra_usage_eta_restarts_after_reset_and_throttle(self):
        now = time.time()
        # a reset (used_pct drop) mid-lookback: slope must use only the post-reset segment (5%→35% over 20m)
        self.s.record_rate_point("r", "five_hour", 80, now + 100, ts=now - 2400)   # old window (about to reset)
        self.s.record_rate_point("r", "five_hour", 5, now + 5 * 3600, ts=now - 1200)  # reset → new window
        self.s.record_rate_point("r", "five_hour", 35, now + 5 * 3600, ts=now)
        eta = self.s.extra_usage_eta("r", "five_hour", now)
        self.assertIsNotNone(eta)
        self.assertAlmostEqual(eta["eta_secs"], 2600, delta=200)   # (100-35)/(30%/1200s)=2600s, not skewed by the 80→5 drop
        # throttle: unchanged + within min_gap → skipped; a real change records
        self.assertTrue(self.s.record_rate_point("t", "five_hour", 50, now + 3600, ts=now))
        self.assertFalse(self.s.record_rate_point("t", "five_hour", 50, now + 3600, ts=now + 60))
        self.assertTrue(self.s.record_rate_point("t", "five_hour", 55, now + 3600, ts=now + 60))

    def test_overage_by_account_sums_cost_during_overcap(self):
        now = time.time()
        # account "ou" is OVER cap from now-1000 (100%) until now-200 (drops to 60%) on the 5h window
        self.s.record_rate_point("ou", "five_hour", 100, now + 600, ts=now - 1000)
        self.s.record_rate_point("ou", "five_hour", 60, now + 600, ts=now - 200)
        rin = store.extract_usage(_usage_line("mi", "ri", "ou",
              datetime.fromtimestamp(now - 600, timezone.utc).isoformat(), 1000), account="ou")   # inside over-cap
        rout = store.extract_usage(_usage_line("mo", "ro", "ou",
              datetime.fromtimestamp(now - 1500, timezone.utc).isoformat(), 1000), account="ou")  # before over-cap
        self.s.record_many([rin, rout])
        ov = self.s.overage_by_account({"ou"}, period="total", now=now)
        self.assertIn("ou", ov)
        self.assertAlmostEqual(ov["ou"], rin["cost_usd"], places=6)   # only the in-window row counts
        # gated: an account not in the enabled set is never computed
        self.assertEqual(self.s.overage_by_account(set(), period="total", now=now), {})
        # an account that never crossed the cap has no overage
        self.assertNotIn("never", self.s.overage_by_account({"never"}, period="total", now=now))

    def test_compact_eta_growing_vs_flat(self):
        now = time.time()
        # context climbs 100k→160k over 10 min → growing → finite ETA
        for i, ctx in enumerate((100_000, 130_000, 160_000)):
            iso = datetime.fromtimestamp(now - 600 + i * 300, timezone.utc).isoformat()
            self.s.record_many([store.extract_usage(
                _usage_line(f"g{i}", f"r{i}", "grow", iso, ctx - 105))])  # ctx_read so context_tokens≈ctx
        eta = self.s.compact_eta("grow", 160_000, 950_000)
        self.assertIsNotNone(eta)
        self.assertGreater(eta["eta_secs"], 0)
        self.assertGreater(eta["tokens_per_min"], 0)
        # a session with one point (or dropping) → no ETA
        self.assertIsNone(self.s.compact_eta("nope", 100, 950_000))

    def test_block_burn_recent_activity(self):
        now = time.time()
        for i in range(3):
            iso = datetime.fromtimestamp(now - 120 + i * 30, timezone.utc).isoformat()
            self.s.record_many([store.extract_usage(_usage_line(f"b{i}", f"r{i}", "s", iso, 500))])
        burn = self.s.block_burn(now)
        self.assertIsNotNone(burn)
        self.assertGreater(burn["tokens_per_min"], 0)
        self.assertGreaterEqual(burn["cost_per_hour"], 0)


class TmuxInjectTests(unittest.TestCase):
    def test_valid_target_accepts_panes_and_sessions(self):
        for t in ("%0", "%37", "cc-build", "cc-build:1", "cc-build:1.0", "my_sess-2"):
            self.assertTrue(tmux_inject.valid_target(t), t)

    def test_valid_target_rejects_injection(self):
        for t in ("; rm -rf /", "a b", "$(x)", "a|b", "a&&b", "`x`", "", None, "a;b"):
            self.assertFalse(tmux_inject.valid_target(t), repr(t))

    def test_resolve_target_reads_map_file(self):
        d = tempfile.mkdtemp()
        sid = "abc-123"
        with open(os.path.join(d, sid + ".target.json"), "w") as f:
            json.dump({"session_id": sid, "tmux_pane": "%4", "tmux_session": "cc-build"}, f)
        self.assertEqual(tmux_inject.resolve_target(sid, state_dir=d), "%4")
        self.assertIsNone(tmux_inject.resolve_target("missing", state_dir=d))

    def test_resolve_target_rejects_malformed_pane(self):
        d = tempfile.mkdtemp()
        sid = "evil"
        with open(os.path.join(d, sid + ".target.json"), "w") as f:
            json.dump({"tmux_pane": "; rm -rf /"}, f)
        self.assertIsNone(tmux_inject.resolve_target(sid, state_dir=d))

    def test_inject_bad_target_raises(self):
        with self.assertRaises(tmux_inject.InjectError) as cm:
            tmux_inject.inject_text("; rm -rf /", "hi")
        self.assertEqual(cm.exception.reason, "bad_target")

    def test_deliver_no_target_raises(self):
        with self.assertRaises(tmux_inject.InjectError) as cm:
            tmux_inject.deliver("nope", "hi", state_dir="/tmp/cc-does-not-exist")
        self.assertEqual(cm.exception.reason, "no_target")

    @unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
    def test_inject_into_live_pane_round_trips(self):
        """Real end-to-end: spin up a disposable shell pane, inject a command + Enter,
        confirm the keystrokes typed AND submitted (the separate-Enter trick works)."""
        sess = "cc-inject-utest"
        subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
        subprocess.run(["tmux", "new-session", "-d", "-s", sess], check=True)
        try:
            pane = subprocess.run(["tmux", "display-message", "-p", "-t", sess, "#{pane_id}"],
                                  capture_output=True, text=True, check=True).stdout.strip()
            self.assertTrue(tmux_inject.pane_alive(pane))
            res = tmux_inject.inject_text(pane, "echo CC_RT_$((6*7))")
            self.assertTrue(res["ok"])
            time.sleep(0.7)
            cap = subprocess.run(["tmux", "capture-pane", "-t", sess, "-p"],
                                 capture_output=True, text=True, check=True).stdout
            self.assertIn("CC_RT_42", cap)  # only present if Enter actually submitted
        finally:
            subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
        self.assertFalse(tmux_inject.pane_alive(pane))  # killed → stale, guarded


class PrefEndpointTests(unittest.TestCase):
    """Cross-boundary test (per the P1/P3 prevention rule): exercise POST /pref over a REAL HTTP server +
    store, then confirm GET /state reflects the hidden flag — the watcher/client ↔ server boundary unit
    tests of compute() alone can't catch."""
    def setUp(self):
        from http.server import ThreadingHTTPServer
        import threading
        self.dir = tempfile.mkdtemp()
        self._store = store.Store(os.path.join(self.dir, "u.db"))
        self._orig_store, server._store = server._store, self._store
        self._orig_pin = server.ACCESS_PIN
        server.ACCESS_PIN = ""        # default these tests to open access (verify.sh exports a PIN into the env)
        with server._lock:
            server._state.clear()
            server._state["sid-A"] = {"session_id": "sid-A", "context_tokens": 1000,
                                      "auth_window": 200000, "reported_at": time.time()}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.httpd.RequestHandlerClass.log_message = lambda *a, **k: None   # quiet
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        server._store = self._orig_store
        server.ACCESS_PIN = self._orig_pin
        with server._lock:
            server._state.clear()
        self._store.close(); shutil.rmtree(self.dir, ignore_errors=True)

    def _req(self, method, path, body=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Content-Type": "application/json"} if body is not None else {}
        c.request(method, path, json.dumps(body) if body is not None else None, hdrs)
        r = c.getresponse(); data = r.read(); c.close()
        return r.status, (json.loads(data) if data else None)

    def test_pref_hide_then_state_reflects_then_unhide(self):
        st, resp = self._req("POST", "/pref", {"session_id": "sid-A", "hidden": True})
        self.assertEqual(st, 200)
        self.assertTrue(resp["ok"]); self.assertTrue(resp["hidden"])
        st2, state = self._req("GET", "/state")
        self.assertEqual(st2, 200)
        item = next(x for x in state if x["session_id"] == "sid-A")
        self.assertTrue(item["hidden"])                       # /state surfaces the hide across the boundary
        st3, resp3 = self._req("POST", "/pref", {"session_id": "sid-A", "hidden": False})
        self.assertFalse(resp3["hidden"])

    def test_pref_color_roundtrips_to_state_and_clears(self):
        st, resp = self._req("POST", "/pref", {"session_id": "sid-A", "color": "cyan"})
        self.assertEqual(st, 200)
        self.assertTrue(resp["ok"]); self.assertEqual(resp["color"], "cyan")
        st2, state = self._req("GET", "/state")
        item = next(x for x in state if x["session_id"] == "sid-A")
        self.assertEqual(item["color"], "cyan")               # /state carries it so the badge can render it
        st3, resp3 = self._req("POST", "/pref", {"session_id": "sid-A", "color": ""})
        self.assertEqual(st3, 200)
        self.assertIsNone(resp3["color"])                     # "" resets to the hash-derived hue

    def test_pref_color_rejects_unknown_name(self):
        st, resp = self._req("POST", "/pref", {"session_id": "sid-A", "color": "chartreuse"})
        self.assertEqual(st, 400)
        self.assertFalse(resp["ok"]); self.assertEqual(resp["reason"], "bad_color")
        # and the rejection must not have written anything
        st2, state = self._req("GET", "/state")
        item = next(x for x in state if x["session_id"] == "sid-A")
        self.assertIsNone(item["color"])

    def test_pref_requires_session_id(self):
        st, resp = self._req("POST", "/pref", {"hidden": True})
        self.assertEqual(st, 400)
        self.assertFalse(resp["ok"])

    def test_pref_unauthorized_when_pin_set(self):
        server.ACCESS_PIN = "1234"                            # gate on; no cookie sent → must be refused
        st, resp = self._req("POST", "/pref", {"session_id": "sid-A", "hidden": True})
        self.assertEqual(st, 401)

    def test_history_returns_session_context_series(self):
        now = time.time()
        self._store.record_many([{"message_id": "h%d" % i, "request_id": "r%d" % i, "session_id": "sid-A",
                                  "ts": now + i, "context_tokens": 1000 * i, "is_sidechain": 0} for i in range(1, 4)])
        st, resp = self._req("GET", "/history?id=sid-A")
        self.assertEqual(st, 200)
        self.assertEqual([round(p["ctx"]) for p in resp["points"]], [1000, 2000, 3000])
        st2, _ = self._req("GET", "/history")                 # missing id → 400
        self.assertEqual(st2, 400)

    def test_session_events_endpoint(self):
        self._store.ingest_event_lines([json.dumps({"uuid": "e1", "sessionId": "sid-A",
            "timestamp": "2026-06-06T07:21:14Z", "compactMetadata": {"trigger": "auto"}})], host="h")
        st, resp = self._req("GET", "/session-events?id=sid-A")
        self.assertEqual(st, 200)
        self.assertEqual(resp["counts"].get("compaction"), 1)
        self.assertEqual(resp["counts"].get("compaction_auto"), 1)
        st2, _ = self._req("GET", "/session-events")          # missing id → 400
        self.assertEqual(st2, 400)

    def test_craft_endpoint_scores_over_real_store(self):
        now = time.time()
        self._store.record_many([
            {"message_id": "c1", "request_id": "r1", "session_id": "sid-A", "account": "a1", "ts": now,
             "model": "claude-opus-4-8", "input_tokens": 100, "cache_creation": 0, "cache_read": 900,
             "output_tokens": 50, "context_tokens": 60_000, "is_sidechain": 0},
            {"message_id": "c2", "request_id": "r2", "session_id": "sid-A", "account": "a1", "ts": now + 1,
             "model": "claude-opus-4-8", "input_tokens": 100, "cache_creation": 0, "cache_read": 900,
             "output_tokens": 50, "context_tokens": 10_000, "is_sidechain": 0}])
        self._store.record_events([
            {"event_id": "k1", "ts": now, "session_id": "sid-A", "account": "a1", "type": "skill_use", "ref": "x"},
            {"event_id": "g1", "ts": now, "session_id": "sid-A", "account": "a1", "type": "compaction",
             "ref": "p", "payload_json": json.dumps({"trigger": "manual"})}])
        st, resp = self._req("GET", "/craft")
        self.assertEqual(st, 200)
        self.assertTrue(resp["enabled"])
        self.assertIsNotNone(resp["score"])
        self.assertAlmostEqual(resp["dims"]["efficiency"]["cache_ratio"], 0.9)
        self.assertEqual(resp["zone_time"]["healthy_pct"], 100.0)   # both samples below drift onset
        self.assertIn("a1", resp["accounts"])
        self.assertIn("medals", resp)                               # Phase 8 medals ride on /craft
        self.assertEqual(len(resp["medals"]), len(__import__("medals").REGISTRY))
        self.assertIn("level", resp)
        self.assertIn("compare", resp)                              # Track 2 windowed self-comparison
        self.assertIn("w7", resp["compare"])
        self.assertIn("w30", resp["compare"])
        self.assertIn("series", resp)                               # Track 2b score-over-time series
        self.assertIsInstance(resp["series"], list)
        st2, r2 = self._req("GET", "/craft?account=nobody")         # filter to an empty account
        self.assertEqual(st2, 200)
        self.assertIsNone(r2["score"])                              # no data → unscored, not an error

    def test_drift_endpoint_returns_curve(self):
        now = time.time()
        rows = [{"message_id": "d%d" % i, "request_id": "r%d" % i, "session_id": "sid-A", "account": "a1",
                 "ts": now + i, "model": "claude-opus-4-8",
                 "context_tokens": 10_000 if i < 30 else 250_000, "is_sidechain": 0} for i in range(60)]
        self._store.record_many(rows)
        self._store.record_events([{"event_id": "x%d" % i, "ts": now + 30 + i, "session_id": "sid-A",
                                    "account": "a1", "type": "tool_error", "ref": "e%d" % i} for i in range(12)])
        st, resp = self._req("GET", "/drift")
        self.assertEqual(st, 200)
        self.assertTrue(resp["enabled"])
        self.assertIn("overall", resp)
        self.assertEqual(len(resp["overall"]["bands"]), 4)          # sharp/good/drift/danger always present


class SecurityHardeningTests(unittest.TestCase):
    """Security review batch 1: /login brute-force throttle (#2) + /import path confinement (#3),
    exercised over a REAL HTTP server with a PIN set (the cross-boundary cases unit tests can't catch)."""
    def setUp(self):
        import hashlib, hmac as _hmac, threading
        from http.server import ThreadingHTTPServer
        self.dir = tempfile.mkdtemp()
        self.exportdir = os.path.join(self.dir, "exports"); os.makedirs(self.exportdir)
        self._store = store.Store(os.path.join(self.dir, "u.db"))
        self._orig = (server._store, server.ACCESS_PIN, server.AUTH_COOKIE, server.EXPORT_DIR,
                      server.LOGIN_MAX_FAILS, server.INGEST_TOKEN, server.OUTBOX_WAIT)
        server._store = self._store
        server.ACCESS_PIN = "supersecretpassphrase"
        server.AUTH_COOKIE = _hmac.new(server.ACCESS_PIN.encode(), b"cc-observability-v1|",
                                       hashlib.sha256).hexdigest()
        server.EXPORT_DIR = self.exportdir
        server.LOGIN_MAX_FAILS = 3
        server.INGEST_TOKEN = ""        # control-plane tests set this per-case
        server.OUTBOX_WAIT = 0.2        # keep the /outbox long-poll from hanging the test
        with server._login_lock:
            server._login_fails.clear()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.httpd.RequestHandlerClass.log_message = lambda *a, **k: None
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True); self._t.start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        (server._store, server.ACCESS_PIN, server.AUTH_COOKIE, server.EXPORT_DIR,
         server.LOGIN_MAX_FAILS, server.INGEST_TOKEN, server.OUTBOX_WAIT) = self._orig
        with server._login_lock:
            server._login_fails.clear()
        self._store.close(); shutil.rmtree(self.dir, ignore_errors=True)

    def _login(self, pin):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/login", "pin=" + pin, {"Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse(); r.read(); c.close()
        return r.status

    def _import(self, body):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/import", json.dumps(body),
                  {"Content-Type": "application/json", "Cookie": "cc_auth=" + server.AUTH_COOKIE})
        r = c.getresponse(); data = r.read(); c.close()
        return r.status, (json.loads(data) if data else None)

    def test_login_throttle_locks_out_after_max_fails(self):
        for _ in range(server.LOGIN_MAX_FAILS):
            self.assertEqual(self._login("wrong"), 401)            # wrong PIN → login page (401)
        self.assertEqual(self._login("wrong"), 429)                # tripped → locked out
        self.assertEqual(self._login(server.ACCESS_PIN), 429)      # even the CORRECT PIN is locked out now

    def test_login_success_clears_throttle(self):
        self.assertEqual(self._login("wrong"), 401)
        self.assertEqual(self._login(server.ACCESS_PIN), 302)      # correct PIN → redirect to /
        with server._login_lock:
            self.assertNotIn("127.0.0.1", server._login_fails)     # success resets the per-IP counter

    def test_import_rejects_path_outside_export_dir(self):
        st, resp = self._import({"path": "/etc/passwd"})
        self.assertEqual(st, 400)
        self.assertEqual(resp["reason"], "bad_path")               # arbitrary abs path refused, never opened
        st2, resp2 = self._import({"path": os.path.join(self.exportdir, "..", "..", "etc", "passwd")})
        self.assertEqual(st2, 400)
        self.assertEqual(resp2["reason"], "bad_path")              # ../ traversal out of EXPORT_DIR blocked

    def test_import_load_failed_is_generic_no_oracle(self):
        p = os.path.join(self.exportdir, "junk.bin")
        with open(p, "wb") as f:
            f.write(b"not a valid bundle")
        st, resp = self._import({"path": p})                       # in-dir but bogus → generic failure
        self.assertEqual(st, 400)
        self.assertEqual(resp["reason"], "load_failed")
        self.assertNotIn("detail", resp)                           # NO str(e) echo → no content/type oracle

    def _get(self, path, token=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, None, ({"X-CC-Token": token} if token is not None else {}))
        r = c.getresponse(); data = r.read(); c.close()
        return r.status, (json.loads(data) if data and r.getheader("Content-Type", "").startswith("application/json") else None)

    def test_control_plane_fails_closed_when_token_unset(self):
        server.INGEST_TOKEN = ""                                   # #4: control-plane must NOT fall open
        st, _ = self._get("/outbox?host=h")
        self.assertEqual(st, 401)                                  # refused (vs metrics /ingest which is open)

    def test_control_plane_requires_matching_token(self):
        server.INGEST_TOKEN = "tok-xyz"
        self.assertEqual(self._get("/outbox?host=h")[0], 401)            # no token header
        self.assertEqual(self._get("/outbox?host=h", token="wrong")[0], 401)
        self.assertEqual(self._get("/outbox?host=h", token="tok-xyz")[0], 200)   # authed → empty queue


class FsPermsTests(unittest.TestCase):
    """Cross-platform file/dir permission hardening (fsperms). POSIX assertions are skipped on Windows,
    where os.chmod can't express 'other users' and the per-user-profile ACL governs access instead."""
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits not meaningful on Windows")
    def test_secure_dir_is_owner_only(self):
        import fsperms
        d = os.path.join(self.dir, "sub")
        fsperms.secure_dir(d)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits not meaningful on Windows")
    def test_secure_file_and_is_exposed(self):
        import fsperms
        p = os.path.join(self.dir, "f")
        with open(p, "w") as f:
            f.write("x")
        os.chmod(p, 0o644)
        self.assertTrue(fsperms.is_exposed(p))                 # 0644 → group/other readable
        fsperms.secure_file(p)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertFalse(fsperms.is_exposed(p))

    @unittest.skipIf(os.name == "nt", "POSIX mode bits not meaningful on Windows")
    def test_store_db_created_owner_only(self):
        p = os.path.join(self.dir, "u.db")
        s = store.Store(p)
        s.close()
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)   # account emails + coach narrative live here

    def test_secure_helpers_safe_on_missing_and_memory(self):
        import fsperms
        fsperms.secure_file(os.path.join(self.dir, "nope"))    # missing → no throw
        fsperms.secure_file(":memory:")                        # sqlite memory sentinel → no throw
        self.assertFalse(fsperms.is_exposed(os.path.join(self.dir, "nope")))


class CraftScoreTests(unittest.TestCase):
    """Pure scoring formula (craft.py) — no DB. The policy lives in one place, so test it directly."""
    def test_efficiency_is_cache_ratio_scaled(self):
        self.assertEqual(craft.efficiency_score(0.9), 90.0)
        self.assertEqual(craft.efficiency_score(0), 0.0)
        self.assertEqual(craft.efficiency_score(1.5), 100.0)   # clamped
        self.assertIsNone(craft.efficiency_score(None))

    def test_hygiene_blends_present_signals_only(self):
        # all three present: discipline 80, healthy 90, cleanliness (1-2/100)=98 → avg 89.33→89.3
        self.assertEqual(craft.hygiene_score(8, 2, 90.0, 2, 100), round((80 + 90 + 98) / 3, 1))
        # only compaction discipline present → that value
        self.assertEqual(craft.hygiene_score(1, 1, None, 0, 0), 50.0)
        # nothing present → None
        self.assertIsNone(craft.hygiene_score(0, 0, None, 0, 0))
        # error rate caps at 100% (cleanliness floors at 0)
        self.assertEqual(craft.hygiene_score(0, 0, None, 999, 100), 0.0)

    def test_craft_dim_saturates_and_needs_messages(self):
        self.assertIsNone(craft.craft_dim_score(5, 5, 0))      # no messages → None
        self.assertEqual(craft.craft_dim_score(0, 0, 100), 0.0)
        lo = craft.craft_dim_score(1, 0, 100)
        hi = craft.craft_dim_score(10, 10, 100)
        self.assertTrue(0 < lo < hi <= 100)                    # monotone, bounded

    def test_composite_renormalizes_over_present_dims(self):
        self.assertEqual(craft.composite(90, None, None), 90.0)
        self.assertEqual(craft.composite(90, 60, None), 75.0)
        self.assertIsNone(craft.composite(None, None, None))

    def test_grade_boundaries(self):
        self.assertEqual([craft.grade(x) for x in (95, 85, 75, 65, 40)], ["A", "B", "C", "D", "F"])
        self.assertEqual(craft.grade(None), "—")

    def test_score_full_breakdown_shape(self):
        out = craft.score({"cache_ratio": 0.92, "messages": 200, "compaction_manual": 9, "compaction_auto": 1,
                           "healthy_pct": 88.0, "tool_errors": 4, "skill_use": 6, "subagent_spawn": 3})
        self.assertIsNotNone(out["score"])
        self.assertIn(out["grade"], list("ABCDF"))
        self.assertEqual(out["dims"]["efficiency"]["cache_ratio"], 0.92)
        self.assertEqual(out["dims"]["hygiene"]["compaction_auto"], 1)
        self.assertEqual(out["dims"]["craft"]["skill_use"], 6)


class MedalTests(unittest.TestCase):
    """Pure award registry/evaluator (medals.py) — tiers + level + locking, no DB."""
    def _agg(self, **kw):
        base = {"cache_ratio": 0.5, "messages": 0, "compaction_manual": 0, "compaction_auto": 0,
                "healthy_pct": None, "tool_errors": 0, "skill_use": 0, "subagent_spawn": 0}
        base.update(kw)
        return base

    def test_count_medal_tiers_and_progress(self):
        r = medals.evaluate(self._agg(subagent_spawn=37))
        d = next(m for m in r["medals"] if m["id"] == "delegator")
        self.assertEqual(d["tier"], "Silver")            # 37 ≥ 25, < 100
        self.assertEqual(d["next_threshold"], 100)
        self.assertTrue(0 < d["progress"] < 1)
        self.assertFalse(d["locked"])

    def test_rate_medal_gated_by_min_samples(self):
        # great ratio but too few messages → locked, no tier
        locked = next(m for m in medals.evaluate(self._agg(cache_ratio=0.99, messages=10))["medals"]
                      if m["id"] == "cache_whisperer")
        self.assertTrue(locked["locked"])
        self.assertIsNone(locked["tier"])
        # enough messages → earns a tier
        ok = next(m for m in medals.evaluate(self._agg(cache_ratio=0.99, messages=500))["medals"]
                  if m["id"] == "cache_whisperer")
        self.assertFalse(ok["locked"])
        self.assertEqual(ok["tier"], "Platinum")          # 99 ≥ 98

    def test_platinum_caps_progress_and_points(self):
        r = medals.evaluate(self._agg(compaction_manual=5, compaction_auto=0))  # 100% discipline
        cs = next(m for m in r["medals"] if m["id"] == "context_surgeon")
        self.assertEqual(cs["tier"], "Platinum")
        self.assertEqual(cs["progress"], 1.0)
        self.assertIsNone(cs["next_threshold"])

    def test_level_points_and_counts(self):
        r = medals.evaluate(self._agg(cache_ratio=0.99, messages=500, compaction_manual=5,
                                      subagent_spawn=37, skill_use=16))
        lvl = r["level"]
        self.assertEqual(lvl["total"], len(medals.REGISTRY))
        self.assertEqual(lvl["max_points"], len(medals.REGISTRY) * 4)
        self.assertTrue(0 < lvl["earned"] <= lvl["total"])
        self.assertTrue(lvl["points"] > 0)

    def test_empty_agg_earns_nothing_but_still_lists_all(self):
        r = medals.evaluate({})
        self.assertEqual(len(r["medals"]), len(medals.REGISTRY))
        self.assertEqual(r["level"]["earned"], 0)
        self.assertEqual(r["level"]["points"], 0)


class CoachMetricsTests(unittest.TestCase):
    """The JAID Coach standalone scanner (skill/jaid-coach/coach_metrics.py) must compute the SAME
    aggregate bundle the dashboard's server._craft_aggregates does — same token_economics / zone_time /
    event_totals definitions — so the coached score matches the dashboard exactly."""

    def _entries(self):
        # 3 assistant usage rows (one a sidechain → excluded from zone but counted in cache_ratio+messages),
        # + a skill_use, a subagent_spawn (Agent), a tool_error, and a manual compaction.
        def asst(mid, rid, it, cc, cr, side=False):
            return {"type": "assistant", "requestId": rid, "isSidechain": side,
                    "timestamp": "2026-06-06T12:00:00.000Z",
                    "message": {"id": mid, "model": "claude-opus-4-8",
                                "usage": {"input_tokens": it, "cache_creation_input_tokens": cc,
                                          "cache_read_input_tokens": cr, "output_tokens": 5}}}
        return [
            asst("m1", "r1", 1, 10, 85000),                 # ctx 85011 → "good"
            asst("m1", "r1", 1, 10, 85000),                 # exact dup → deduped by (id, requestId)
            asst("m2", "r2", 1, 0, 0, side=True),           # sidechain → counts in cache_ratio, not zone
            asst("m3", "r3", 1, 0, 130000),                 # ctx 130001 → "drift"
            {"type": "assistant", "requestId": "r4", "timestamp": "2026-06-06T12:00:00.000Z",
             "uuid": "u4", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Skill",
                                                    "input": {"skill": "x"}}]}},
            {"type": "assistant", "requestId": "r5", "timestamp": "2026-06-06T12:00:00.000Z",
             "uuid": "u5", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Agent",
                                                   "input": {"subagent_type": "Explore"}}]}},
            {"type": "user", "uuid": "u6", "timestamp": "2026-06-06T12:00:00.000Z",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True}]}},
            {"compactMetadata": {"trigger": "manual"}, "uuid": "u7",
             "timestamp": "2026-06-06T12:00:00.000Z"},
        ]

    def test_scan_and_agg_match_store_definitions(self):
        entries = self._entries()
        usage, events = [], []
        seen_u, seen_e = set(), set()
        # mirror coach_metrics._scan over an in-memory entry list (no temp files needed)
        for d in entries:
            for ev in store.extract_events(d):
                if ev["event_id"] not in seen_e:
                    seen_e.add(ev["event_id"]); events.append(ev)
            if d.get("type") != "assistant":
                continue
            m = d.get("message") or {}
            u = m.get("usage") or {}
            mid, rid = m.get("id"), d.get("requestId")
            if not u or not mid or not rid:
                continue
            if (mid, rid) in seen_u:
                continue
            seen_u.add((mid, rid))
            it = int(u.get("input_tokens", 0)); cc = int(u.get("cache_creation_input_tokens", 0))
            cr = int(u.get("cache_read_input_tokens", 0))
            usage.append({"ts": store.parse_ts(d.get("timestamp")), "it": it, "cc": cc, "cr": cr,
                          "ctx": it + cc + cr, "side": bool(d.get("isSidechain")), "model": m.get("model")})
        agg, zone, mix = coach_metrics._agg(usage, events)
        self.assertEqual(agg["messages"], 3)                       # dup collapsed; 3 real usage rows
        # cache_ratio = sum(cr)/(sum(cr)+sum(it)+sum(cc)) = 215000/215013
        self.assertAlmostEqual(agg["cache_ratio"], round(215000 / 215013, 4), places=4)
        self.assertEqual(zone["sharp"], 0)
        self.assertEqual(zone["good"], 1)
        self.assertEqual(zone["drift"], 1)
        self.assertEqual(zone["total"], 2)                         # sidechain excluded from zone
        self.assertEqual(agg["healthy_pct"], 50.0)                 # (sharp+good)/total = 1/2
        self.assertEqual(agg["tool_errors"], 1)
        self.assertEqual(agg["skill_use"], 1)
        self.assertEqual(agg["subagent_spawn"], 1)
        self.assertEqual(agg["compaction_manual"], 1)
        self.assertEqual(agg["compaction_auto"], 0)
        # the agg flows cleanly through the SAME scoring modules the dashboard uses
        self.assertIsNotNone(craft.score(agg)["score"])
        self.assertEqual(len(medals.evaluate(agg)["medals"]), len(medals.REGISTRY))

    def test_scan_reads_transcript_dir(self):
        d = tempfile.mkdtemp()
        try:
            sub = os.path.join(d, "proj"); os.makedirs(sub)
            with open(os.path.join(sub, "s.jsonl"), "w") as f:
                f.write("\n".join(json.dumps(x) for x in self._entries()) + "\n")
            orig = coach_metrics.PROJECTS_DIR
            coach_metrics.PROJECTS_DIR = d
            try:
                usage, events = coach_metrics._scan()
            finally:
                coach_metrics.PROJECTS_DIR = orig
            self.assertEqual(len(usage), 3)
            self.assertTrue(all("ot" in r for r in usage))   # V11: scan now captures output_tokens for savings
            types = {e["type"] for e in events}
            self.assertEqual(types, {"skill_use", "subagent_spawn", "tool_error", "compaction"})
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_standalone_savings_match_store_logic(self):
        # V11: the Coach's standalone _savings mirrors store.model_savings (same costing + thresholds → parity)
        now = time.time()
        usage = [
            {"ts": now, "it": 1_000_000, "ot": 50, "cc": 0, "cr": 0, "ctx": 1, "side": False, "model": "claude-opus-4-8"},
            {"ts": now, "it": 1_000_000, "ot": 2000, "cc": 0, "cr": 0, "ctx": 1, "side": False, "model": "claude-opus-4-8"},
            {"ts": now, "it": 1_000_000, "ot": 30, "cc": 0, "cr": 0, "ctx": 1, "side": False, "model": "claude-sonnet-4-6"},
            {"ts": now, "it": 1_000_000, "ot": 10, "cc": 0, "cr": 0, "ctx": 1, "side": True, "model": "claude-opus-4-8"},
        ]
        sv = coach_metrics._savings(usage, days=30)
        self.assertTrue(sv["estimate"])
        self.assertEqual(sv["considered"], 3)                    # sidechain excluded
        pol = {p["id"]: p for p in sv["policies"]}
        self.assertEqual((pol["all_opus_sonnet"]["turns"], pol["short_opus_sonnet"]["turns"],
                          pol["trivial_haiku"]["turns"]), (2, 1, 2))
        self.assertTrue(all(pol[k]["saved_usd"] > 0 for k in pol))


class SessionAutopsyTests(unittest.TestCase):
    """V8 Slice A — store.session_autopsy / recent_sessions: content-free per-session report card."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.s = store.Store(os.path.join(self.dir, "u.db"))

    def tearDown(self):
        self.s.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _seed(self):
        now = 1_700_000_000.0
        rows = [
            # main-thread turns climbing the zones: sharp(10k) → good(60k) → drift(150k) → danger(250k)
            dict(message_id="m1", request_id="r1", session_id="S", account="me@x.com", project="proj-A",
                 host="h1", ts=now, model="claude-opus-4-8", input_tokens=100, cache_creation=0,
                 cache_read=9000, output_tokens=50, context_tokens=10_000, cost_usd=0.10, is_sidechain=0),
            dict(message_id="m2", request_id="r2", session_id="S", account="me@x.com", project="proj-A",
                 host="h1", ts=now + 60, model="claude-sonnet-4-6", input_tokens=100, cache_creation=0,
                 cache_read=9000, output_tokens=80, context_tokens=60_000, cost_usd=0.20, is_sidechain=0),
            dict(message_id="m3", request_id="r3", session_id="S", account="me@x.com", project="proj-A",
                 host="h1", ts=now + 120, model="claude-opus-4-8", input_tokens=100, cache_creation=0,
                 cache_read=9000, output_tokens=80, context_tokens=150_000, cost_usd=0.30, is_sidechain=0),
            dict(message_id="m4", request_id="r4", session_id="S", account="me@x.com", project="proj-A",
                 host="h1", ts=now + 180, model="claude-opus-4-8", input_tokens=100, cache_creation=0,
                 cache_read=9000, output_tokens=80, context_tokens=250_000, cost_usd=0.40, is_sidechain=0),
            # a sidechain (subagent) row — billed but excluded from the zone timeline
            dict(message_id="s1", request_id="rs", session_id="S", account="me@x.com", project="proj-A",
                 host="h1", ts=now + 90, model="claude-opus-4-8", input_tokens=10, cache_creation=0,
                 cache_read=0, output_tokens=10, context_tokens=999_999, cost_usd=0.05, is_sidechain=1),
            # a second, older session so recent_sessions ordering is testable
            dict(message_id="o1", request_id="ro", session_id="OLD", account="me@x.com", project="proj-B",
                 host="h1", ts=now - 5000, model="claude-haiku-4-5", input_tokens=50, cache_creation=0,
                 cache_read=0, output_tokens=20, context_tokens=5_000, cost_usd=0.01, is_sidechain=0),
        ]
        self.s.record_many(rows)
        self.s.record_events([
            {"event_id": "e1", "ts": now + 10, "session_id": "S", "type": "compaction",
             "ref": "u", "payload_json": json.dumps({"trigger": "manual"})},
            {"event_id": "e2", "ts": now + 20, "session_id": "S", "type": "compaction",
             "ref": "u", "payload_json": json.dumps({"trigger": "auto"})},
            {"event_id": "e3", "ts": now + 30, "session_id": "S", "type": "skill_use", "ref": "t",
             "payload_json": json.dumps({"skill": "cc-verify"})},
            {"event_id": "e4", "ts": now + 40, "session_id": "S", "type": "subagent_spawn", "ref": "t",
             "payload_json": "{}"},
            {"event_id": "e5", "ts": now + 50, "session_id": "S", "type": "tool_error", "ref": "t",
             "payload_json": "{}"},
        ])
        return now

    def test_session_autopsy_assembles_content_free_report(self):
        now = self._seed()
        a = self.s.session_autopsy("S")
        self.assertEqual(a["messages"], 5)                 # 4 main + 1 sidechain (all billed)
        self.assertEqual(a["events"], {"compaction_manual": 1, "compaction_auto": 1,
                                       "skill_use": 1, "subagent_spawn": 1, "tool_error": 1})
        # zone timeline is MAIN-THREAD only (sidechain's 999999 ctx excluded) → 4 points, one per band
        self.assertEqual(len(a["zone_series"]), 4)
        self.assertEqual(a["zone_time"]["total"], 4)
        self.assertEqual((a["zone_time"]["sharp"], a["zone_time"]["good"],
                          a["zone_time"]["drift"], a["zone_time"]["danger"]), (1, 1, 1, 1))
        self.assertEqual(a["zone_time"]["healthy_pct"], 50.0)   # (sharp+good)/total
        self.assertEqual(a["model_mix"], {"claude-opus-4-8": 4, "claude-sonnet-4-6": 1})  # 3 main opus + 1 sidechain
        self.assertEqual(a["duration_secs"], 180.0)            # max(main+side ts) - min ts
        self.assertAlmostEqual(a["cost_usd"], 1.05, places=4)  # 0.10+0.20+0.30+0.40+0.05
        self.assertFalse(a["estimate"])                        # real-billed, not an estimate
        # content-free: the serialized payload must carry no conversation text — only numbers/ids/model names
        blob = json.dumps(a)
        for forbidden in ("tool_result", "content", "text", "skill", "trigger"):
            self.assertNotIn('"' + forbidden + '"', blob)

    def test_session_autopsy_none_for_empty(self):
        self.assertIsNone(self.s.session_autopsy("nope"))
        self.assertIsNone(self.s.session_autopsy(""))

    def test_recent_sessions_newest_first(self):
        self._seed()
        rs = self.s.recent_sessions(12)
        self.assertEqual([r["session_id"] for r in rs], ["S", "OLD"])   # S is more recent
        self.assertEqual(rs[0]["project"], "proj-A")
        self.assertEqual(rs[0]["messages"], 5)


class CoachWorkerTests(unittest.TestCase):
    """V8 Slice B — coach.py prompt builder (content-free) + coach_worker engine selection (graceful)."""

    def test_build_prompt_is_content_free_and_trims_bulk(self):
        import coach
        bundle = {"score": 63.3, "grade": "D", "cache_ratio": 0.9, "healthy_pct": 40,
                  "compaction_manual": 6, "compaction_auto": 1,
                  "autopsy": {"messages": 42, "events": {"tool_error": 3},
                              "zone_series": [{"ts": 1, "ctx": 10}, {"ts": 2, "ctx": 20}]}}
        p = coach.build_prompt(bundle)
        self.assertIn("system", p)
        self.assertIn("user", p)
        sys_l = p["system"].lower()
        for token in ("content-free", "never invent", "never criticise", "projects, files"):
            self.assertIn(token, sys_l)             # honesty + no-identifying-detail discipline baked in
        self.assertNotIn("zone_series", p["user"])  # bulky per-turn arrays trimmed (dogfood context discipline)
        self.assertIn("63.3", p["user"])            # the scoring numbers survive
        self.assertIn(coach.SYSTEM, coach.flatten(p))

    def test_pick_engine_resolves_by_availability(self):
        import coach_worker as cw
        orig = (cw.ENGINE, cw._claude_available, cw._ollama_available)
        try:
            cw.ENGINE = "off"
            self.assertEqual(cw.pick_engine(), "off")
            cw.ENGINE = "claude"; cw._claude_available = lambda: False
            self.assertEqual(cw.pick_engine(), "off")            # forced claude but CLI absent → off
            cw._claude_available = lambda: True
            self.assertEqual(cw.pick_engine(), "claude")
            cw.ENGINE = "auto"; cw._claude_available = lambda: False; cw._ollama_available = lambda: True
            self.assertEqual(cw.pick_engine(), "ollama")         # auto falls back to a local Ollama
            cw._ollama_available = lambda: False
            self.assertEqual(cw.pick_engine(), "off")            # nothing available → off (never crashes)
        finally:
            cw.ENGINE, cw._claude_available, cw._ollama_available = orig

    def test_generate_off_is_graceful_noop(self):
        import coach_worker as cw
        orig = cw.ENGINE
        try:
            cw.ENGINE = "off"
            self.assertEqual(cw.generate({"score": 50}), (None, "off"))
        finally:
            cw.ENGINE = orig


class ContentIndexTests(unittest.TestCase):
    """V9 content search index — the one content-bearing module. Opt-in, isolated, offset-tracked, deletable."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.idx = content_index.ContentIndex(os.path.join(self.dir, "c.db"), host="h1")
        self.p = os.path.join(self.dir, "s1.jsonl")
        self._write_lines([
            {"type": "user", "sessionId": "s1", "uuid": "u1", "timestamp": "2026-06-06T10:00:00Z",
             "cwd": "/x/Proj", "message": {"content": "please fix the WIDGET parser"}},
            {"type": "assistant", "sessionId": "s1", "uuid": "u2", "timestamp": "2026-06-06T10:00:01Z",
             "message": {"content": [
                 {"type": "thinking", "thinking": "the tokenizer mishandles unicode"},
                 {"type": "text", "text": "fixed it"},
                 {"type": "tool_use", "name": "Bash", "input": {"command": "grep NEEDLE_TOK src/foo.py"}}]}},
            {"type": "user", "sessionId": "s1", "uuid": "u3", "timestamp": "2026-06-06T10:00:02Z",
             "message": {"content": [{"type": "tool_result", "content": [
                 {"type": "text", "text": "foo.py:42: RESULTONLY_MARKER here"}]}]}},
        ])

    def tearDown(self):
        self.idx.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write_lines(self, lines, mode="w"):
        with open(self.p, mode) as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")

    def test_disabled_writes_nothing(self):
        # The opt-in firewall: until enable(), no content reaches disk.
        self.assertEqual(self.idx.index_pass([self.p]), {"files": 0, "docs": 0})
        self.assertEqual(self.idx.stats()["docs"], 0)
        self.assertEqual(self.idx.search("WIDGET"), [])

    def test_enabled_indexes_everything_incl_tool_io(self):
        self.idx.enable()
        self.assertEqual(self.idx.index_pass([self.p])["docs"], 5)  # user+thinking+text+tool_use+tool_result
        self.assertTrue(self.idx.search("NEEDLE_TOK"))         # tool_use INPUT is searchable
        self.assertTrue(self.idx.search("RESULTONLY_MARKER"))  # tool_result OUTPUT is searchable
        self.assertTrue(self.idx.search("tokenizer"))          # assistant thinking
        self.assertTrue(self.idx.search("WIDGET"))             # user prompt
        self.assertEqual(self.idx.search("zzznotpresentzz"), [])

    def test_incremental_offset_and_dedup(self):
        self.idx.enable()
        self.idx.index_pass([self.p])
        self.assertEqual(self.idx.index_pass([self.p])["docs"], 0)  # nothing new on a re-pass
        self._write_lines([{"type": "user", "sessionId": "s1", "uuid": "u9",
                            "timestamp": "2026-06-06T10:00:09Z",
                            "message": {"content": "APPENDEDWORD later"}}], mode="a")
        self.assertEqual(self.idx.index_pass([self.p])["docs"], 1)  # only the appended line
        self.assertEqual(len(self.idx.search("APPENDEDWORD")), 1)

    def test_snippet_uses_sentinels_not_html(self):
        # Highlight is delimited by control chars so the client can esc() then swap for <mark> (XSS-safe).
        self.idx.enable()
        self.idx.index_pass([self.p])
        snip = self.idx.search("tokenizer")[0]["snippet"]
        self.assertIn("\x02", snip)
        self.assertIn("\x03", snip)
        self.assertNotIn("<mark>", snip)

    def test_fts_injection_is_safe(self):
        self.idx.enable()
        self.idx.index_pass([self.p])
        for q in ['" OR 1=1 --', "foo* AND (bar", "NEAR(", '")', "*"]:
            self.assertIsInstance(self.idx.search(q), list)  # arbitrary input never raises

    def test_wipe_clears_and_disables(self):
        self.idx.enable()
        self.idx.index_pass([self.p])
        self.assertGreater(self.idx.stats()["docs"], 0)
        self.idx.wipe()
        self.assertEqual(self.idx.stats()["docs"], 0)
        self.assertFalse(self.idx.enabled())

    def test_search_results_carry_uuid_anchor(self):
        self.idx.enable()
        self.idx.index_pass([self.p])
        hit = self.idx.search("NEEDLE_TOK")[0]
        self.assertTrue(hit.get("uuid"))   # the stable anchor used to open the conversation

    def test_search_filters_by_kind_and_since(self):
        import datetime
        now = time.time()

        def iso(t):
            return datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%SZ")

        fp = os.path.join(self.dir, "f.jsonl")
        with open(fp, "w") as f:
            f.write(json.dumps({"type": "user", "sessionId": "f", "uuid": "x1", "timestamp": iso(now - 40 * 86400),
                                "message": {"content": "ZEBRA in an old prompt"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "sessionId": "f", "uuid": "x2", "timestamp": iso(now - 3600),
                                "message": {"content": [{"type": "text", "text": "ZEBRA in a fresh reply"}]}}) + "\n")
            f.write(json.dumps({"type": "assistant", "sessionId": "f", "uuid": "x3", "timestamp": iso(now - 3600),
                                "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "grep ZEBRA"}}]}}) + "\n")
        self.idx.enable()
        self.idx.index_pass([fp])
        self.assertEqual(len(self.idx.search("ZEBRA")), 3)                         # unfiltered
        self.assertEqual([h["kind"] for h in self.idx.search("ZEBRA", kinds=["user"])], ["user"])
        self.assertEqual(sorted(h["kind"] for h in self.idx.search("ZEBRA", kinds=["assistant", "tool_use"])),
                         ["assistant", "tool_use"])
        recent = self.idx.search("ZEBRA", since_ts=now - 7 * 86400)               # excludes the 40-day-old prompt
        self.assertNotIn("user", [h["kind"] for h in recent])
        self.assertEqual(len(recent), 2)
        # an invalid kind string must NOT filter (and must never reach SQL) → behaves as "all"
        self.assertEqual(len(self.idx.search("ZEBRA", kinds=["x'; DROP TABLE docs;--"])), 3)

    def test_semantic_search_with_mock_embedder(self):
        if content_index.ContentIndex._np() is None:
            self.skipTest("numpy not available")
        # deterministic 3-dim vectors per text — no network. The query is closest to the billing/subscription docs.
        VEC = {
            "how do I cancel my subscription": [1.0, 0.0, 0.0],
            "billing and refunds for your plan": [0.95, 0.10, 0.0],
            "the weather is nice today": [0.0, 1.0, 0.0],
            "stop my recurring payment": [0.9, 0.05, 0.0],   # the query
        }
        fp = os.path.join(self.dir, "sem.jsonl")
        with open(fp, "w") as f:
            f.write(json.dumps({"type": "user", "sessionId": "z", "uuid": "z1", "timestamp": "2026-06-06T10:00:00Z",
                                "message": {"content": "how do I cancel my subscription"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "sessionId": "z", "uuid": "z2", "timestamp": "2026-06-06T10:00:00Z",
                                "message": {"content": [{"type": "text", "text": "billing and refunds for your plan"}]}}) + "\n")
            f.write(json.dumps({"type": "assistant", "sessionId": "z", "uuid": "z3", "timestamp": "2026-06-06T10:00:00Z",
                                "message": {"content": [{"type": "text", "text": "the weather is nice today"}]}}) + "\n")
        orig_model = content_index.EMBED_MODEL
        content_index.EMBED_MODEL = "mock-embed"        # enable the semantic path
        self.idx._embed_texts = lambda texts: [VEC[t] for t in texts]   # stub Ollama (instance override)
        try:
            self.idx.enable()
            self.idx.index_pass([fp])
            self.assertTrue(self.idx.semantic_available())
            self.assertEqual(self.idx.embed_pass(batch=10)["embedded"], 3)
            self.assertEqual(self.idx.embed_stats()["embedded"], 3)
            res = self.idx.semantic_search("stop my recurring payment", limit=3)
            self.assertTrue(res)
            top2 = {r["uuid"] for r in res[:2]}        # subscription + billing rank above weather
            self.assertEqual(top2, {"z1:0", "z2:0"})
            self.assertEqual(res[-1]["uuid"], "z3:0")
            self.assertGreater(res[0]["score"], res[-1]["score"])
            # the kind/date filters still apply in semantic mode
            self.assertTrue(all(r["kind"] == "user"
                                for r in self.idx.semantic_search("stop my recurring payment", kinds=["user"])))
        finally:
            content_index.EMBED_MODEL = orig_model

    def test_context_window_and_paging(self):
        # a 30-block session; open around a mid hit, then page up/down (load-more).
        big = os.path.join(self.dir, "big.jsonl")
        with open(big, "w") as f:
            for i in range(30):
                marker = "FINDME" if i == 15 else "filler"
                f.write(json.dumps({"type": "user", "sessionId": "big", "uuid": f"b{i}",
                                    "timestamp": "2026-06-06T10:00:00Z",
                                    "message": {"content": f"line {i} {marker}"}}) + "\n")
        self.idx.enable()
        self.idx.index_pass([big])
        hit = self.idx.search("FINDME")[0]
        ctx = self.idx.context("big", anchor=hit["uuid"], n=5)
        hits = [b for b in ctx["blocks"] if b["hit"]]
        self.assertEqual(len(hits), 1)               # exactly the anchor is flagged
        self.assertIn("FINDME", hits[0]["text"])
        self.assertEqual(len(ctx["blocks"]), 11)     # 5 before + anchor + 5 after
        self.assertTrue(ctx["has_more_before"] and ctx["has_more_after"])
        # page up from the top, then down to the end (has_more_after must clear)
        up = self.idx.context("big", before_pos=ctx["blocks"][0]["pos"], n=5)
        self.assertEqual(len(up["blocks"]), 5)
        self.assertTrue(all(not b["hit"] for b in up["blocks"]))
        down = self.idx.context("big", after_pos=ctx["blocks"][-1]["pos"], n=100)
        self.assertFalse(down["has_more_after"])     # reached the end of the session


class ContentSearchGateTests(unittest.TestCase):
    """V9 content firewall: raw content is served ONLY on the loopback content port; the LAN listener walls
    it. Enforcement is the BOUND PORT (Docker-correct — source IP is unreliable under bridge masquerade)."""

    def setUp(self):
        from http.server import ThreadingHTTPServer
        import threading

        class _QuietHandler(server.Handler):
            def log_message(self, *a):
                pass

        self.dir = tempfile.mkdtemp()
        self._orig_content = server._content
        self._orig_cport = server.CONTENT_PORT
        self._orig_pin = server.ACCESS_PIN
        server.ACCESS_PIN = ""   # open auth so this isolates the LOCAL gate, not PIN auth
        server._content = content_index.ContentIndex(os.path.join(self.dir, "c.db"), host="h")
        self.content_httpd = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
        self.cport = self.content_httpd.server_address[1]
        server.CONTENT_PORT = self.cport            # the server bound here IS the content port
        self.lan_httpd = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
        self.lport = self.lan_httpd.server_address[1]
        for h in (self.content_httpd, self.lan_httpd):
            threading.Thread(target=h.serve_forever, daemon=True).start()

    def tearDown(self):
        for h in (self.content_httpd, self.lan_httpd):
            h.shutdown()
            h.server_close()
        server._content.close()
        server._content = self._orig_content
        server.CONTENT_PORT = self._orig_cport
        server.ACCESS_PIN = self._orig_pin
        shutil.rmtree(self.dir, ignore_errors=True)

    def _get(self, port, path):
        import urllib.request
        import urllib.error
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _post(self, port, path):
        import urllib.request
        import urllib.error
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b"", method="POST")
        try:
            r = urllib.request.urlopen(req, timeout=3)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_search_walled_on_lan_port(self):
        code, body = self._get(self.lport, "/search?q=anything")
        self.assertEqual(code, 403)
        self.assertFalse(body["local"])
        self.assertEqual(body["results"], [])

    def test_search_served_on_content_port(self):
        code, body = self._get(self.cport, "/search?q=anything")
        self.assertEqual(code, 200)
        self.assertTrue(body["local"])

    def test_meta_local_flag_differs_by_port(self):
        self.assertFalse(self._get(self.lport, "/search-meta")[1]["local"])
        self.assertTrue(self._get(self.cport, "/search-meta")[1]["local"])

    def test_context_walled_on_lan_port(self):
        # the conversation reader is raw content too — must 403 off the content port
        code, body = self._get(self.lport, "/search-context?sid=whatever")
        self.assertEqual(code, 403)
        self.assertFalse(body["local"])
        self.assertEqual(body["blocks"], [])

    def test_enable_refused_without_pin(self):
        code, body = self._post(self.cport, "/search-enable")
        self.assertEqual(code, 403)
        self.assertEqual(body["reason"], "disabled")

    def test_content_config_refused_without_pin(self):
        code, body = self._post(self.cport, "/content-config")
        self.assertEqual(code, 403)
        self.assertEqual(body["reason"], "disabled")


class ContentPrivacyTests(unittest.TestCase):
    """V13 2B: retention auto-purge, best-effort redaction, conversation-only scope."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.idx = content_index.ContentIndex(os.path.join(self.dir, "c.db"), host="h")
        self.idx.enable()
        self.p = os.path.join(self.dir, "s1.jsonl")

    def tearDown(self):
        self.idx.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, lines, mode="w"):
        with open(self.p, mode) as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")

    def test_redact_function_masks_and_passes(self):
        red = redact.redact("here is sk-ABCDEFGHIJKLMNOPQRSTUVWX and AKIA1234567890ABCDEF done")
        self.assertNotIn("sk-ABCDEFGHIJKLMNOPQRSTUVWX", red)
        self.assertNotIn("AKIA1234567890ABCDEF", red)
        self.assertIn("[REDACTED", red)
        self.assertEqual(redact.redact("just normal prose"), "just normal prose")

    def test_redaction_at_index_time_when_on(self):
        self._write([{"type": "user", "sessionId": "s1", "uuid": "r1", "timestamp": "2026-06-06T10:00:00Z",
                      "message": {"content": "token=SUPERSECRETVALUE123 in here"}}])
        self.idx.set_config(redact=True)
        self.idx.index_pass([self.p])
        self.assertEqual(self.idx.search("SUPERSECRETVALUE123"), [])   # secret value masked before storage

    def test_redaction_off_by_default(self):
        self._write([{"type": "user", "sessionId": "s1", "uuid": "r2", "timestamp": "2026-06-06T10:00:00Z",
                      "message": {"content": "plainword12345 here"}}])
        self.idx.index_pass([self.p])
        self.assertTrue(self.idx.search("plainword12345"))

    def test_conversation_only_scope_excludes_tool_output(self):
        self._write([
            {"type": "user", "sessionId": "s1", "uuid": "c1", "timestamp": "2026-06-06T10:00:00Z",
             "message": {"content": "USERPROMPT_X"}},
            {"type": "user", "sessionId": "s1", "uuid": "c2", "timestamp": "2026-06-06T10:00:01Z",
             "message": {"content": [{"type": "tool_result", "content": [{"type": "text", "text": "TOOLOUT_Y"}]}]}},
        ])
        self.idx.set_config(scope="conversation")
        self.idx.index_pass([self.p])
        self.assertTrue(self.idx.search("USERPROMPT_X"))
        self.assertEqual(self.idx.search("TOOLOUT_Y"), [])

    def test_apply_retention_purges_old_keeps_new(self):
        now = time.time()
        self.idx._insert_docs([
            {"uuid": "old:0", "session_id": "S", "project": "p", "host": "h",
             "role": "user", "kind": "user", "ts": now - 20 * 86400, "text": "OLDDOC"},
            {"uuid": "new:0", "session_id": "S", "project": "p", "host": "h",
             "role": "user", "kind": "user", "ts": now, "text": "NEWDOC"}])
        self.idx.set_config(retention_days=14)
        self.assertEqual(self.idx.apply_retention(), 1)
        self.assertEqual(self.idx.search("OLDDOC"), [])
        self.assertTrue(self.idx.search("NEWDOC"))

    def test_retention_zero_disables_autopurge(self):
        now = time.time()
        self.idx._insert_docs([{"uuid": "o:0", "session_id": "S", "project": "p", "host": "h",
                                "role": "user", "kind": "user", "ts": now - 99 * 86400, "text": "ANCIENT"}])
        self.idx.set_config(retention_days=0)
        self.assertEqual(self.idx.apply_retention(), 0)
        self.assertTrue(self.idx.search("ANCIENT"))

    def test_config_roundtrip(self):
        self.idx.set_config(retention_days=30, redact=True, scope="conversation")
        c = self.idx.config()
        self.assertEqual(c["retention_days"], 30)
        self.assertTrue(c["redact"])
        self.assertEqual(c["scope"], "conversation")
        self.assertEqual(content_index.ContentIndex(self.idx.path).config()["retention_days"], 30)  # persisted


class MigrationTests(unittest.TestCase):
    """V13 Slice 1: forward-only migration runner keyed on meta.schema_version (store + content_index)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_store_fresh_db_at_current_version(self):
        p = os.path.join(self.dir, "u.db")
        s = store.Store(p)
        self.assertEqual(s.get_meta("schema_version"), str(store._CODE_SCHEMA_VERSION))
        # the baseline migration ensured the account/is_sidechain columns exist
        cols = {r["name"] for r in s._all("PRAGMA table_info(usage)")}
        self.assertIn("account", cols)
        self.assertIn("is_sidechain", cols)
        s.close()

    def test_store_baselines_preversion_db_and_is_idempotent(self):
        p = os.path.join(self.dir, "u.db")
        store.Store(p).close()
        # simulate a DB created before versioning existed: drop the schema_version marker
        raw = sqlite3.connect(p)
        raw.execute("DELETE FROM meta WHERE key='schema_version'")
        raw.commit()
        raw.close()
        s2 = store.Store(p)   # reopen → runner must re-baseline without error
        self.assertEqual(s2.get_meta("schema_version"), str(store._CODE_SCHEMA_VERSION))
        s2.close()
        store.Store(p).close()   # reopen again → still stable (idempotent)

    def test_store_does_not_downgrade_a_future_db(self):
        p = os.path.join(self.dir, "u.db")
        s = store.Store(p)
        s.set_meta("schema_version", "999")   # pretend a newer app wrote this
        s.close()
        s2 = store.Store(p)
        self.assertEqual(s2.get_meta("schema_version"), "999")   # never rolled back
        s2.close()

    def test_content_index_versioned(self):
        p = os.path.join(self.dir, "c.db")
        idx = content_index.ContentIndex(p, host="h")
        self.assertEqual(idx.get_meta("schema_version"), str(content_index._CODE_SCHEMA_VERSION))
        idx.close()


class ConcurrencyTests(unittest.TestCase):
    """V13 Slice 1: reads use short-lived connections (no write-lock contention) for file-backed DBs."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.s = store.Store(os.path.join(self.dir, "u.db"))

    def tearDown(self):
        self.s.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_file_db_uses_read_connections(self):
        self.assertFalse(self.s._shared_reads)

    def test_memory_db_falls_back_to_shared(self):
        s = store.Store(":memory:")
        self.assertTrue(s._shared_reads)
        s.close()

    def test_reads_see_committed_writes(self):
        now = time.time()
        self.s.record_many([{"message_id": "m1", "request_id": "r1", "session_id": "sX",
                             "ts": now, "context_tokens": 1000, "cost_usd": 0.5,
                             "input_tokens": 1, "output_tokens": 2, "is_sidechain": 0}])
        self.assertEqual(self.s.session_total("sX")["messages"], 1)

    def test_concurrent_reads_do_not_deadlock(self):
        import threading as _t
        now = time.time()
        self.s.record_many([{"message_id": "m%d" % i, "request_id": "r%d" % i, "session_id": "sX",
                             "ts": now + i, "context_tokens": 100 * i, "is_sidechain": 0}
                            for i in range(20)])
        errors = []

        def worker():
            try:
                for _ in range(20):
                    self.s.session_context_series("sX")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [_t.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertFalse(any(t.is_alive() for t in threads))


class HealthTests(unittest.TestCase):
    """V13 Slice 1: /health is content-free, unauthenticated, and reports version + runtime."""

    def setUp(self):
        from http.server import ThreadingHTTPServer
        import threading

        class _QuietHandler(server.Handler):
            def log_message(self, *a):
                pass

        self._orig_pin = server.ACCESS_PIN
        server.ACCESS_PIN = "1234"   # even with a PIN set, /health must NOT require auth
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.ACCESS_PIN = self._orig_pin

    def test_health_reports_version_and_runtime(self):
        import urllib.request
        r = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=3)
        self.assertEqual(r.status, 200)
        body = json.loads(r.read())
        self.assertTrue(body["ok"])
        self.assertEqual(body["version"], version.VERSION)
        self.assertIn(body["runtime"], ("docker", "native"))

    def test_update_endpoint_defaults_to_no_update(self):
        import urllib.request
        r = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/update", timeout=3)
        body = json.loads(r.read())
        self.assertEqual(body["current"], version.VERSION)
        self.assertFalse(body["update_available"])          # no manifest configured → never flags an update
        self.assertIn("verb", body)


class VersionTests(unittest.TestCase):
    """V13 Slice 6: version compare + per-runtime upgrade verb."""

    def test_is_newer(self):
        self.assertTrue(version.is_newer("0.14.0", "0.13.0"))
        self.assertTrue(version.is_newer("1.0.0", "0.13.9"))
        self.assertFalse(version.is_newer("0.13.0", "0.13.0"))
        self.assertFalse(version.is_newer("0.12.9", "0.13.0"))
        self.assertFalse(version.is_newer("garbage", "0.13.0"))

    def test_is_newer_extended(self):
        """Additional is_newer cases required by A3 spec."""
        # patch bump
        self.assertTrue(version.is_newer("0.13.1", "0.13.0"))
        # minor bump
        self.assertTrue(version.is_newer("0.14.0", "0.13.0"))
        # same version
        self.assertFalse(version.is_newer("0.13.0", "0.13.0"))
        # older minor (9 < 13)
        self.assertFalse(version.is_newer("0.9.0", "0.13.0"))
        # v-prefix tolerated on latest
        self.assertTrue(version.is_newer("v0.13.1", "0.13.0"))
        # v-prefix tolerated on current
        self.assertTrue(version.is_newer("0.13.1", "v0.13.0"))
        # malformed/empty LATEST (e.g. failed manifest fetch) → False, no exception, no spurious update.
        # (current is always version.VERSION in production, never None — so only `latest` is exercised here.)
        self.assertFalse(version.is_newer("not-a-version", "0.13.0"))
        self.assertFalse(version.is_newer(None, "0.13.0"))    # type: ignore[arg-type]

    def test_upgrade_verb(self):
        self.assertIn("docker compose", version.upgrade_verb("docker"))
        self.assertIn("pipx", version.upgrade_verb("native"))

    def test_version_py_and_pyproject_toml_in_sync(self):
        """Guard: version.VERSION must equal the version field in pyproject.toml.
        Prevents silent drift between the two sources of truth."""
        import re
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        toml_path = os.path.join(repo_root, "pyproject.toml")
        self.assertTrue(os.path.exists(toml_path), f"pyproject.toml not found at {toml_path}")
        with open(toml_path, encoding="utf-8") as fh:
            toml_text = fh.read()
        # Try stdlib tomllib (Python 3.11+) first; fall back to regex for 3.10.
        toml_version = None
        try:
            import tomllib  # available Python 3.11+
            data = tomllib.loads(toml_text)
            toml_version = data.get("project", {}).get("version")
        except ImportError:
            m = re.search(r'^\s*version\s*=\s*"([^"]+)"', toml_text, re.MULTILINE)
            if m:
                toml_version = m.group(1)
        self.assertIsNotNone(toml_version, "Could not parse version from pyproject.toml")
        self.assertEqual(
            version.VERSION,
            toml_version,
            f"version.py VERSION ({version.VERSION!r}) != pyproject.toml version ({toml_version!r}). "
            "Run scripts/bump-version.sh to update both at once.",
        )


class MaintenanceTests(unittest.TestCase):
    """V13 Slice 2: checkpoint / prune / vacuum + db_stats (all content-free)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.s = store.Store(os.path.join(self.dir, "u.db"))
        self.idx = content_index.ContentIndex(os.path.join(self.dir, "c.db"), host="h")

    def tearDown(self):
        self.s.close()
        self.idx.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _seed_rate(self):
        now = time.time()
        for i in range(3):   # 3 old (>30d)
            self.s.record_rate_point("a@x", "five_hour", 10.0 * i + 1, now, ts=now - 40 * 86400 - i * 100, min_gap=0)
        for i in range(2):   # 2 recent
            self.s.record_rate_point("a@x", "five_hour", 50.0 + i, now, ts=now - i * 100, min_gap=0)

    def test_prune_rate_history_dry_equals_real_and_idempotent(self):
        self._seed_rate()
        dry = maintenance.prune_preview(self.s, None, {"rate_history_days": 30})["rate_history"]["rows"]
        self.assertEqual(dry, 3)
        real = maintenance.prune_apply(self.s, None, {"rate_history_days": 30})["rate_history"]["rows"]
        self.assertEqual(real, 3)
        again = maintenance.prune_apply(self.s, None, {"rate_history_days": 30})["rate_history"]["rows"]
        self.assertEqual(again, 0)   # nothing left older than the cutoff

    def test_checkpoint_leaves_small_wal(self):
        self._seed_rate()
        self.s.checkpoint()   # TRUNCATE → -wal shrinks to ~0 (no open readers in this test)
        wal = self.s.path + "-wal"
        size = os.path.getsize(wal) if os.path.exists(wal) else 0
        self.assertLessEqual(size, 32768)

    def test_content_prune_before_cascades(self):
        self.idx.enable()
        now = time.time()
        self.idx._insert_docs([
            {"uuid": "o1:0", "session_id": "S", "project": "p", "host": "h",
             "role": "user", "kind": "user", "ts": now - 40 * 86400, "text": "old hello"},
            {"uuid": "n1:0", "session_id": "S", "project": "p", "host": "h",
             "role": "user", "kind": "user", "ts": now, "text": "new hello"}])
        with self.idx._lock:
            self.idx._db.execute("INSERT INTO embeddings(uuid,vec) VALUES('o1:0', x'00')")
            self.idx._db.commit()
        removed = self.idx.prune_before(now - 30 * 86400)
        self.assertEqual(removed, 1)
        with self.idx._lock:
            docs = [r["uuid"] for r in self.idx._db.execute("SELECT uuid FROM docs")]
            embs = self.idx._db.execute("SELECT COUNT(*) c FROM embeddings").fetchone()["c"]
            seen_old = self.idx._db.execute("SELECT COUNT(*) c FROM seen WHERE uuid='o1:0'").fetchone()["c"]
        self.assertEqual(docs, ["n1:0"])     # only the new doc remains
        self.assertEqual(embs, 0)            # its embedding cascaded
        self.assertEqual(seen_old, 0)        # its seen-marker cascaded

    def test_content_prune_orphans(self):
        self.idx.enable()
        now = time.time()
        self.idx._insert_docs([
            {"uuid": "live:0", "session_id": "sessLIVE", "project": "p", "host": "h",
             "role": "user", "kind": "user", "ts": now, "text": "a"},
            {"uuid": "dead:0", "session_id": "sessDEAD", "project": "p", "host": "h",
             "role": "user", "kind": "user", "ts": now, "text": "b"}])
        with self.idx._lock:
            for sid in ("sessLIVE", "sessDEAD"):
                self.idx._db.execute("INSERT INTO files(path,mtime,size,offset) VALUES(?,0,0,0)",
                                     ("/x/" + sid + ".jsonl",))
            self.idx._db.commit()
        live = ["/x/sessLIVE.jsonl"]
        prev = maintenance.prune_preview(None, self.idx, {"content_orphans": True}, live_paths=live)["content_orphans"]
        self.assertEqual((prev["files"], prev["docs"]), (1, 1))
        res = maintenance.prune_apply(None, self.idx, {"content_orphans": True}, live_paths=live)["content_orphans"]
        self.assertEqual(res["docs"], 1)
        with self.idx._lock:
            sids = [r["session_id"] for r in self.idx._db.execute("SELECT session_id FROM docs")]
            files = [r["path"] for r in self.idx._db.execute("SELECT path FROM files")]
        self.assertEqual(sids, ["sessLIVE"])
        self.assertEqual(files, ["/x/sessLIVE.jsonl"])

    def test_db_stats_counts_and_sizes(self):
        self._seed_rate()
        st = maintenance.db_stats(self.s, self.idx)
        self.assertEqual(st["usage"]["rows"]["rate_history"], 5)
        self.assertGreater(st["usage"]["sizes"]["main"], 0)
        self.assertIn("content", st)

    def test_vacuum_reports_size(self):
        self._seed_rate()
        out = maintenance.vacuum(self.s, None, which="usage")
        self.assertIn("usage", out)
        self.assertGreater(out["usage"]["after"], 0)


class AggregateCacheTests(unittest.TestCase):
    """V13 Slice 4: TTL + write-generation memoize on the heavy aggregates (multi-client polling win)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.s = store.Store(os.path.join(self.dir, "u.db"))

    def tearDown(self):
        self.s.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _seed(self, n, base):
        now = time.time()
        self.s.record_many([{"message_id": f"m{base}_{i}", "request_id": f"r{base}_{i}", "session_id": "s",
                             "ts": now - i * 3600, "context_tokens": 1000, "cost_usd": 0.1, "input_tokens": 1,
                             "output_tokens": 1, "is_sidechain": 0, "model": "claude-opus-4-8", "project": "p"}
                            for i in range(n)])

    def test_cache_hit_returns_same_object(self):
        self._seed(5, "a")
        a = self.s.daily_totals(days=14)
        b = self.s.daily_totals(days=14)
        self.assertIs(a, b)             # served from cache within TTL/generation

    def test_write_invalidates_cache(self):
        self._seed(5, "a")
        a = self.s.daily_totals(days=14)
        self._seed(3, "b")              # a write bumps the generation
        c = self.s.daily_totals(days=14)
        self.assertIsNot(a, c)          # recomputed after the write

    def test_cached_value_matches_recompute(self):
        self._seed(6, "a")
        cached = self.s.daily_totals(days=14)
        self.s._bump_agg()              # force a miss
        fresh = self.s.daily_totals(days=14)
        self.assertEqual(cached, fresh)  # parity: cache never changes the answer

    def test_distinct_args_cached_separately(self):
        self._seed(4, "a")
        self.assertIsNot(self.s.daily_totals(days=7), self.s.daily_totals(days=14))


class CryptoTests(unittest.TestCase):
    """V13 Slice 3: AES-256-GCM encrypt/decrypt with a passphrase."""

    @unittest.skipUnless(crypto.available(), "cryptography not installed")
    def test_roundtrip(self):
        ct = crypto.encrypt(b"secret bytes here", "hunter2")
        self.assertTrue(crypto.is_encrypted(ct))
        self.assertNotIn(b"secret bytes here", ct)
        self.assertEqual(crypto.decrypt(ct, "hunter2"), b"secret bytes here")

    @unittest.skipUnless(crypto.available(), "cryptography not installed")
    def test_wrong_passphrase_fails(self):
        ct = crypto.encrypt(b"x" * 64, "right")
        with self.assertRaises(Exception):
            crypto.decrypt(ct, "wrong")

    @unittest.skipUnless(crypto.available(), "cryptography not installed")
    def test_empty_passphrase_rejected(self):
        with self.assertRaises(ValueError):
            crypto.encrypt(b"x", "")


class PortableExportTests(unittest.TestCase):
    """V13 Slice 3: content-free portable bundle export/import (idempotent merge) + encryption-before-write."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.s = store.Store(os.path.join(self.dir, "u.db"))
        now = time.time()
        self.s.record_many([{"message_id": "m1", "request_id": "r1", "session_id": "sA", "host": "h1",
                             "ts": now, "context_tokens": 1000, "cost_usd": 0.5, "input_tokens": 1,
                             "output_tokens": 2, "is_sidechain": 0}])
        self.s.record_rate_point("a@x", "five_hour", 10.0, now, ts=now, min_gap=0)
        self.s.set_session_account("sA", "a@x", host="h1", source="hook")
        self.s.set_session_pref("sA", hidden=True)
        self.s.set_meta("plan_prices", json.dumps({"a@x": 20}))

    def tearDown(self):
        self.s.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_bundle_content_free_and_covers_tables(self):
        b = portable.export_bundle(self.s)
        self.assertEqual(b["format"], portable.FORMAT)
        for t in portable.EXPORT_TABLES:
            self.assertIn(t, b["tables"])
        for rows in b["tables"].values():     # content-free: scalar values only
            for r in rows:
                for v in r.values():
                    self.assertTrue(v is None or isinstance(v, (int, float, str)))

    def test_roundtrip_and_idempotent(self):
        b = portable.export_bundle(self.s)
        s2 = store.Store(os.path.join(self.dir, "u2.db"))
        res = portable.import_bundle(s2, b)
        self.assertEqual(res["usage"]["added"], 1)
        self.assertEqual(res["session_account"]["added"], 1)
        res2 = portable.import_bundle(s2, b)
        self.assertTrue(all(v["added"] == 0 for v in res2.values()))   # re-import adds nothing
        self.assertEqual(s2.session_total("sA")["messages"], 1)
        self.assertEqual(s2.hidden_sessions(), {"sA"})
        s2.close()

    def test_dry_import_does_not_write(self):
        b = portable.export_bundle(self.s)
        s2 = store.Store(os.path.join(self.dir, "u3.db"))
        res = portable.import_bundle(s2, b, dry=True)
        self.assertEqual(res["usage"]["added"], 1)
        self.assertEqual(s2.session_total("sA")["messages"], 0)        # nothing persisted on dry-run
        s2.close()

    def test_bad_bundle_rejected(self):
        with self.assertRaises(ValueError):
            portable.import_bundle(self.s, {"format": "nope"})

    @unittest.skipUnless(crypto.available(), "cryptography not installed")
    def test_write_encrypted_no_plaintext_then_load(self):
        res = portable.write_export(self.s, os.path.join(self.dir, "exp"), passphrase="pw", encrypt=True)
        self.assertTrue(res["encrypted"])
        with open(res["path"], "rb") as f:
            blob = f.read()
        self.assertTrue(crypto.is_encrypted(blob))
        self.assertNotIn(b"cc-observability/portable", blob)   # no plaintext bundle markers
        self.assertNotIn(b"plan_prices", blob)
        self.assertEqual(portable.load_export(blob, passphrase="pw")["format"], portable.FORMAT)
        with self.assertRaises(Exception):
            portable.load_export(blob, passphrase="wrong")

    @unittest.skipUnless(crypto.available(), "cryptography not installed")
    def test_encrypted_export_requires_passphrase(self):
        with self.assertRaises(ValueError):
            portable.write_export(self.s, os.path.join(self.dir, "exp2"), encrypt=True)

    def test_plaintext_export_opt_in(self):
        res = portable.write_export(self.s, os.path.join(self.dir, "exp3"), encrypt=False)
        self.assertFalse(res["encrypted"])
        with open(res["path"], "rb") as f:
            blob = f.read()
        self.assertFalse(crypto.is_encrypted(blob))
        self.assertEqual(portable.load_export(blob)["format"], portable.FORMAT)


class MaintenanceEndpointTests(unittest.TestCase):
    """V13 Slice 3: /maintenance (stats), /export (encrypted write), /import (PIN-gated)."""

    def setUp(self):
        from http.server import ThreadingHTTPServer
        import threading

        class _Q(server.Handler):
            def log_message(self, *a):
                pass

        self.dir = tempfile.mkdtemp()
        self._os, self._op, self._oe = server._store, server.ACCESS_PIN, server.EXPORT_DIR
        server.ACCESS_PIN = ""                       # open auth → isolates the PIN gate on mutations
        server.EXPORT_DIR = os.path.join(self.dir, "exp")
        server._store = store.Store(os.path.join(self.dir, "u.db"))
        server._store.record_rate_point("a@x", "five_hour", 5.0, time.time(), ts=time.time(), min_gap=0)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Q)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server._store.close()
        server._store, server.ACCESS_PIN, server.EXPORT_DIR = self._os, self._op, self._oe
        shutil.rmtree(self.dir, ignore_errors=True)

    def _get(self, path):
        import urllib.request
        import urllib.error
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=3)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _post(self, path, body):
        import urllib.request
        import urllib.error
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            r = urllib.request.urlopen(req, timeout=5)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_maintenance_returns_stats(self):
        code, body = self._get("/maintenance")
        self.assertEqual(code, 200)
        self.assertIn("stats", body)
        self.assertIn("usage", body["stats"])

    def test_import_refused_without_pin(self):
        code, body = self._post("/import", {"data": {"format": portable.FORMAT, "tables": {}}})
        self.assertEqual(code, 403)
        self.assertEqual(body["reason"], "disabled")

    @unittest.skipUnless(crypto.available(), "cryptography not installed")
    def test_export_writes_encrypted_file(self):
        code, body = self._post("/export", {"passphrase": "pw"})
        self.assertEqual(code, 200)
        self.assertTrue(body["encrypted"])
        self.assertTrue(os.path.exists(body["path"]))

    def test_export_encrypted_needs_passphrase(self):
        code, body = self._post("/export", {})
        self.assertEqual(code, 400)
        self.assertEqual(body["reason"], "passphrase_required")


if __name__ == "__main__":
    unittest.main()

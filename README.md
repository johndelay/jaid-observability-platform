# JAID Observability Platform

> Part of the **JAID** family of tools. _(Package / CLI / container name: `cc-observability`.)_

A phone-glanceable **fuel gauge for Claude Code sessions**: how full the context
window is and how close you are to auto-compaction — plus best-effort subagent
activity. Stdlib Python only (no pip deps).

_Independent project — not affiliated with or endorsed by Anthropic, Google, or any AI vendor whose products it integrates. Product names and trademarks (e.g. "Claude", "Antigravity") belong to their respective owners. Licensed under the MIT License (see LICENSE)._

TOS / compliance posture (passive, local, no credentials, no third-party data egress): [`COMPLIANCE.md`](COMPLIANCE.md).
Terms of Use: [`TERMS.md`](TERMS.md) · License: [`LICENSE`](LICENSE).

## How it works

```
Claude Code → ~/.claude/projects/**/*.jsonl   (transcripts)
   watcher.py  (per machine)  ── polls transcripts, ships raw token facts ──►
   server.py   (one host)     ── applies window/threshold policy, serves SSE ──►
   web/index.html             ── mobile dashboard (open on a phone)
```

The watcher is **passive** (polls transcripts; not a Claude Code hook) so it adds
zero latency to your sessions. It POSTs fire-and-forget with a short timeout, so an
unreachable collector never stalls anything.

**Context occupancy** = the latest non-sidechain assistant message's
`input_tokens + cache_creation_input_tokens + cache_read_input_tokens`.

## Window detection (important)

Transcripts record the model **without** the `[1m]` marker, so a 1M-context session
looks like a 200k one until it crosses 200k. Declare your 1M models:

```
CC_WINDOW_1M_MODELS=claude-opus-4-8
```

The UI shows a `1M` / `200k` badge so the assumption is always visible. Backstop:
the collector auto-escalates to 1M once a session exceeds 200k.

## Install — two paths

Pick by what you're running:

- **New user, your own laptop → native, driven by Claude Code.** In Claude Code, run the
  **`cc-observability-setup`** skill (`skill/cc-observability-setup/`). It detects your OS, makes a venv
  (`pip install -e .` → numpy + cryptography), seeds `.env` with a generated PIN, installs a per-OS
  background service (systemd --user / launchd / Task Scheduler), starts it, and verifies `/health`.
  Native runs as you, so it needs no sidecars and binds the raw-content search port straight to `127.0.0.1`.

- **Always-on homelab server / many hosts → Docker.** Isolation, restart policy, a read-only transcript
  mount, and one reproducible image across the fleet. See **Run it (Docker)** below.

Either way the app is self-hosted and content-free, makes no third-party calls by default (two opt-in features fetch public files — no data sent), and survives upgrades
(your data lives in a volume / `~/.cache`; see `docs/UPGRADE.md`). Back up / move data with the encrypted
export in the 🧹 Maintenance scene (`docs/DATA_EXPORT.md`).

## Run it (Docker — recommended for servers)

A single small container (python:3.12-alpine) that reads this host's transcripts read-only and serves the
dashboard. One process: `server.py` with `CC_LOCAL_SCAN=1` scans `~/.claude/projects` in-process.

```bash
mkdir -p ~/.cache/cc-dashboard   # state-snapshot mount target — must exist before the first `up`
cp .env.sample .env              # set CC_HOST to this machine's name
docker compose up -d --build
# open http://<this-host>:8099   (my-desktop = http://localhost:8099)
```

> First load looks empty? That's expected until Claude Code has actually run on this host — the
> dashboard reads `~/.claude/projects`, so it fills in as you use Claude Code (nothing to configure).

> Only `~/.claude/projects` is mounted, **read-only** — not all of `~/.claude` (that holds
> credentials). The container can't see the real hostname, so set `CC_HOST` in `.env`.

## Run it (native, no Docker)

The `cc-observability-setup` skill automates this, but by hand:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e .   # numpy + cryptography
cp .env.sample .env                                     # set CC_HOST, CC_RUNTIME=native, CC_ACCESS_PIN
CC_RUNTIME=native ./.venv/bin/cc-observability          # or: python3 server.py
# or as systemd --user services:
./install-user-services.sh
```

`pip install -e .` (editable) keeps the `web/` assets in the checkout; for a non-editable install outside a
checkout, set `CC_WEB_DIR` to the `web/` location. Native binds the content-search port to `127.0.0.1`
directly (no Docker masquerade → no dual-port firewall).

## Remote hosts (multi-host, later)

Run the dashboard once (anywhere), then on each *other* Claude machine run `watcher.py`
pointed at it — it POSTs to `/ingest`:

```bash
CC_COLLECTOR_URL=http://<dashboard-host>:8099/ingest CC_HOST=$(hostname) python3 watcher.py
```

## Config (env)

| Var | Default | Where | Meaning |
|-----|---------|-------|---------|
| `CC_PORT` | 8099 | server | listen port |
| `CC_WINDOW_DEFAULT` | 200000 | server | assumed window when model isn't a known 1M model |
| `CC_WINDOW_1M_MODELS` | (empty) | server | comma-sep model-id substrings to treat as 1M |
| `CC_ACCESS_PIN` | (empty) | server | set → require a PIN to view the UI (PIN→cookie); empty = open on LAN |
| `CC_INGEST_TOKEN` | (empty) | server | set → require `X-CC-Token` header on `/ingest` from remote watchers |
| `CC_STATE_DIR` | /data/state | server/watcher | hook+statusline state dir (`~/.cache/cc-dashboard` outside Docker) |
| `CC_COMPACT_THRESHOLD` | 0.95 | server | fraction of window where auto-compact fires |
| `CC_STALE_SECS` | 120 | server | no update → card dims |
| `CC_EXPIRE_SECS` | 21600 | server | drop session from view after this idle |
| `CC_SSE_INTERVAL` | 5 | server | how often the live stream checks for changes (sends only on change) |
| `CC_SSE_HEARTBEAT` | 30 | server | keep-alive ping when nothing changed |
| `CC_COLLECTOR_URL` | http://localhost:8099/ingest | watcher | where to POST (remote mode) |
| `CC_HOST` | hostname | both | label for this machine |
| `CC_POLL_INTERVAL` | 10 | both | seconds between transcript scans (kept light — context creeps slowly) |
| `CC_ACTIVE_HOURS` | 6 | both | only report sessions touched within this window |
| `CC_SUBAGENT_RUNNING_SECS` | 30 | both | a subagent file modified within this = "running" |
| `CC_SUBAGENT_WINDOW` | 900 | both | show subagents active within this many seconds |

> These are all env-tunable today; a future in-app **settings menu** will just write them.

## Testing

One harness, run it before claiming anything works (the `cc-verify` skill enforces this):

```bash
CC_URL=http://localhost:8099 ./verify.sh    # must end ✅ ALL GREEN
```

It runs four layers, fastest-feedback first:
1. **Python unit tests** (`tests/test_core.py`) — watcher parsing + server policy.
2. **DOM smoke** (`tests/ui_smoke.js`) — runs the page's `render()`/`renderStream()`/`openDetail()` in a
   *stubbed* DOM (node stdlib, no deps). Catches render-time JS errors curl can't see.
3. **Browser E2E** (`tests/e2e_playwright.js`) — loads the **real page in real Chromium** against a running
   server: title, fleet render, drill-in resolves past "loading…", no uncaught exceptions, and writes a
   screenshot to `tests/artifacts/dashboard.png`. Skips cleanly if Playwright isn't installed.
4. **Live endpoint checks** — `/health`, `/`, `/state`, `/activity`.

Layers 1, 2, 4 are dependency-free. The browser layer needs a one-time setup:

```bash
npm install && npx playwright install chromium
```

> **Interactive visual polish** (vs. the automated gate above): this repo also enables the **Playwright MCP**
> (via the bundled `.mcp.json`). Start a Claude Code session with this repo as the project root and
> Claude can drive a real browser on demand — navigate, screenshot, read console — the same way a local
> article-publishing workflow does. (Takes effect on a fresh session; MCP servers load at startup.)

## Answer from phone (E5)

Tap a waiting session in the drill-in, type a reply, and it's injected into that session's
**tmux pane** — answer Claude from your phone without walking to the machine.

```
phone → POST /reply (PIN-gated) → collector queues it under the session's host
     → responder.py (on that host) long-polls /outbox → tmux send-keys into the pane
     → reports the outcome back → the phone shows "delivered → %3"
```

Because injecting a reply is **RCE into your shell**, it's off by default and gated hard:

- `/reply` is **refused unless `CC_ACCESS_PIN` is set** (which also gates the whole UI behind a login).
- The responder authenticates to the collector with `CC_INGEST_TOKEN`.
- Only sessions running **inside tmux** are answerable — the state hook records `session_id → $TMUX_PANE`
  in `~/.cache/cc-dashboard/<sid>.target.json`; the UI shows a reply box only when a target exists *and* a
  PIN is set. Run it only on a trusted network (LAN / Tailscale), never the public edge.

Enable it on a host:

```bash
# 1) set a PIN (+ token) in .env, then recreate the collector so it picks them up
#    CC_ACCESS_PIN=...   CC_INGEST_TOKEN=...
docker compose up -d --force-recreate
# 2) start the native responder (must run on the host — the container can't reach host tmux)
./deploy-responder.sh
```

The responder is a separate, opt-in daemon by design: the watcher is read-only, this is the *write* path.

## Cost history, burn rate & time-to-compact ETA (Sprint 3)

The collector persists every message's token usage into a small SQLite store (`costing.py` prices it,
`store.py` keeps it) so the dashboard can show **where your tokens go** and **how long you've got**:

- **Burn rate** — the current ccusage-style **5-hour block**: `$/hour` + `tokens/min` (cache excluded, since
  it dominates and inflates the rate), shown as a summary chip with today's spend.
- **Time-to-compact ETA** — `⏱ ~22m` on each card + in the drill-in, from the recent slope of the session's
  context occupancy. Disappears when a session isn't growing (e.g. just compacted).
- **`GET /cost`** — JSON: 5h burn block, 14-day daily totals, per-project totals, grand total.

Idempotent + compaction-safe by construction: each usage row is keyed `(message_id, request_id)` with
`INSERT OR IGNORE`, so re-scanning a transcript (and compaction replaying the same `message.id`) never
double-counts — no special-case logic. The DB lives in a writable `/data/db` volume (`CC_DB_PATH`); outside
Docker it defaults under `~/.cache/cc-observability/`.

> Cost is an **estimate** for history/rollups; when the statusline feed is present the dashboard shows
> Claude's **authoritative** `cost.total_cost_usd` instead (estimates are marked `~`). The estimate runs a
> few % low on 1M-context sessions because the above-200k price tier isn't modelled yet (the exact Opus 1M
> numbers aren't in the price table — added when known, rather than guessed).

## Roadmap

Phase 1 = this (token gauge MVP). Next: PWA polish, containerize the collector on a
dedicated server, multi-host watcher rollout, subagent/workflow timeline.

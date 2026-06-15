# Components & Options

A map of **everything JAID can run**, what's **on by default**, and how to turn on the rest. The guided
install that uses this reference is **[`TEMPLATE_INSTALL_CHECKLIST.md`](../TEMPLATE_INSTALL_CHECKLIST.md)** —
your AI agent copies it and walks you through. For the agent's rules of engagement (it **asks before
changing your system**), see [`AGENTS.md`](../AGENTS.md).

> **Self-hosted & content-free.** Everything runs on your own machines. By default the app makes **no
> third-party network calls** and stores **no conversation content** (only token counts/metadata). The two
> features that *can* reach out — the opt-in self-update check and the opt-in "latest suggestions" fetch —
> send **no user data**, and semantic search uses **your own local Ollama**. See [`COMPLIANCE.md`](../COMPLIANCE.md).

---

## The mental model — 7 layers

| Layer | What | Default |
|------|------|---------|
| 1. **Dashboard** | the web UI + API, on **one** host | you pick a runtime (Docker or native) |
| 2. **This host's data** | reads this machine's `~/.claude/projects` transcripts | ✅ automatic with layer 1 |
| 3. **Per-terminal enrichment** | statusline + state hooks (Claude Code `settings.json`) | ❌ **opt-in, per host** |
| 4. **Native sidecars** | answer-from-phone responder, in-app coach worker | ❌ opt-in |
| 5. **Fleet** | a watcher on each *other* machine, reporting in | ❌ opt-in |
| 6. **Optional integrations** | semantic search (Ollama), MCP-tax probe, self-update check | ❌ opt-in |
| 7. **Security & data lifecycle** | PIN/token, encrypted export, maintenance | recommended / built-in |

**Layers 1–2 are the whole product for one machine.** Everything from layer 3 on is additive and optional.

---

## Layer 1 — The dashboard (pick ONE runtime)

The dashboard is a single Python process (`server.py`) that serves the mobile web UI on
`http://<host>:8099` and reads this host's transcripts.

### Docker — recommended for an always-on server
`docker compose up -d --build`. Isolation, restart policy, one reproducible image. Mounts
`~/.claude/projects` **read-only**. Set `CC_HOST` in `.env` (the container can't see the real hostname).

### Native — recommended for a laptop / personal machine
Driven by Claude Code via the **`jaid-setup`** skill (it makes a venv, seeds `.env`, installs a per-OS
background service, and — if you accept — wires the statusline + state hooks below). By hand:
`python3 -m venv .venv && ./.venv/bin/pip install -e .` then `./install-user-services.sh`.

> ⚠️ **Don't mix them.** `install-user-services.sh` installs `cc-collector` **and** `cc-watcher` services —
> you do **not** want those on a Docker host (the container already is the collector). On a Docker host, add
> only the **sidecars** you want (`deploy-responder.sh`, `deploy-coach-worker.sh`).

---

## Layer 2 — This host's data (automatic)

With either runtime, the dashboard reads this machine's transcripts and fills in as you use Claude Code.
**Nothing to configure** — if it looks empty, you just haven't run Claude Code on this host yet.

---

## Layer 3 — Per-terminal enrichment (opt-in, per host) — **the in-terminal gauge**

These are **Claude Code `settings.json`** entries, **not** installed by any dashboard installer by default.
They run *inside your Claude Code terminal* on each host you want them on. Editing `settings.json` is a
change to your environment, so an agent should **ask before wiring them** (per `AGENTS.md`).

### Statusline (`hooks/cc-statusline.sh`) — ⭐ the `● brave-otter · Opus 4.8 · 🟢 34% · 🧠 long context` line
**Two jobs:**
1. **In your terminal**, it prints a status line with: a **friendly session name + color** (the same one the
   dashboard shows, so you can tell *which* terminal matches *which* card); the model; and **two decoupled
   context signals** —
   - **compaction proximity** = `% of the context window`, colored 🟢 (<60%) / 🟡 (≥60%, "consider /compact")
     / 🔴 (≥85%, "⚠ /compact now"). This is the hard wall.
   - **dumb-zone note** = a dim, quiet `🧠 long context` once absolute context passes ~120k tokens
     (Context Rot — quality can soften long before the wall). Never red, never nags.
2. **Feeds the dashboard** the **authoritative** window size, used-%, cost, rate-limits, and Claude account
   (captured host-side so OAuth tokens never enter the container). This is what upgrades cost from an
   estimate to Claude's exact number and powers the per-account rate-limit gauges.

**Install:** add to `~/.claude/settings.json`:
```json
"statusLine": { "type": "command", "command": "<repo>/hooks/cc-statusline.sh" }
```
(`<repo>` = your checkout, e.g. `~/git/jaid-observability-platform`.) Takes effect next session.

### State hooks (`hooks/cc-state-hook.sh`) — precise activity + answer-from-phone capture
Writes a tiny per-session state file on each Claude Code event. **Gives you:**
- **Precise activity state** — "needs you" fires only when Claude is genuinely **blocked** (a permission
  prompt, or an AskUserQuestion), not on every finished turn. (Without the hook, the dashboard still derives
  a best-effort state from the transcript, but can't see permission waits.)
- **tmux pane capture** — records `session_id → $TMUX_PANE`, which is what makes **answer-from-phone**
  (layer 4) able to type back into the right terminal.

**Install:** add the hook to these 7 events in `~/.claude/settings.json` `hooks` (each as
`"command": "<repo>/hooks/cc-state-hook.sh <EventName>"`): `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
`PostToolUse`, `Stop`, `Notification`, `SessionEnd`. Shape per event:
```json
"SessionStart": [ { "hooks": [ { "type": "command",
  "command": "<repo>/hooks/cc-state-hook.sh SessionStart" } ] } ]
```
> The `jaid-setup` skill can wire **both** of these for you (it asks first and backs up `settings.json`).
> These are **per-host**: wire them on every machine where you want the gauge + precise state.

---

## Advisory — the "dumb zone" is a heuristic, not an Anthropic-defined threshold

> ⚠️ **Not official.** The context-health "dumb zone" / zones (🟢 <50k · 🟡 50–120k · 🟠 120–200k · 🔴 ≥200k
> absolute tokens) used by the statusline and the gauge are **not documented or acknowledged by Anthropic at
> this time** — nor by any other vendor. They are a **practical heuristic** grounded in public long-context
> research, surfaced as a soft signal, **not a guarantee or a capability cliff.**

What the published research consensus supports:
1. **Quality degradation as context grows is real and universal**, and it begins well below the advertised
   window (200k / 1M). [Liu 2023; RULER 2024; NoLiMa 2025; Chroma 2025]
2. **"Effective context" is a fraction of the advertised window** — RULER found GPT-4's effective context
   ≈ 64k against a 128k claim, and roughly half of tested models couldn't hold quality even at 32k.
3. **Degradation is gradual / continuous, not a sharp cliff** — Chroma: "the decline is continuous, not a
   cliff." (This matches JAID's own analysis of real usage, which found **no measurable error-rate knee** —
   so the zones are presented as a gradient, never a hard line.)
4. **It is heavily task-dependent.** Simple keyword retrieval holds near the window limit on frontier models,
   but reasoning, multi-hop, instruction-following, and code tasks degrade much earlier. Claude Code work
   (tool-call chains, plans, accumulated constraints) sits on the **faster-degrading** end.
5. **No single agreed threshold exists.** The common "a 200k-window model can degrade by ~50k" rule of thumb
   is a *qualitative* characterization, not a measured constant.
6. **No vendor (Anthropic / OpenAI / Google) publishes an official sub-window "effective context" floor** —
   every threshold in the wild comes from third-party benchmarks.

**Bottom line:** treat the zones as a *"consider a clean checkpoint (`/compact` or `/clear`)"* nudge, not a
hard limit. Exact numbers vary by model, task, and version; figures here are from published research
summaries and are informational only.

### Sources
- Liu et al., **"Lost in the Middle"** (TACL 2023) — https://arxiv.org/abs/2307.03172
- **RULER**: "What's the Real Context Size of Your Long-Context LMs?" (NVIDIA, COLM 2024) — https://arxiv.org/abs/2404.06654
- **NoLiMa**: "Long-Context Evaluation Beyond Literal Matching" (ICML 2025) — https://arxiv.org/abs/2502.05167
- **Chroma Research**, "Context Rot" (2025) — https://research.trychroma.com/context-rot
- **InfiniteBench / ∞Bench** (ACL 2024) — https://arxiv.org/abs/2402.13718
- **HELMET** (Princeton, 2024) — https://arxiv.org/abs/2410.02694

---

## Layer 4 — Native sidecars (opt-in)

Small native daemons that run **as you** (not in the container) because they need access the container
doesn't have. Install on the host that runs the relevant terminals.

### Answer-from-phone (`responder.py` → `cc-responder` service)
Tap a waiting session on your phone, type a reply → injected into its **tmux pane**. Install with
`./deploy-responder.sh`. **Requirements:** the **state hooks** (layer 3, for the tmux pane), a **`CC_ACCESS_PIN`**
(the server refuses `/reply` without one — injecting a reply is RCE into your shell), `CC_INGEST_TOKEN`, and
sessions running **inside tmux**. Run only on a trusted network (LAN / Tailscale). Full detail in the README's
"Answer from phone" section.

### In-app coach (`coach_worker.py` → `cc-coach-worker` service)
Generates the 🧠 **Coach** scene's narrative using **your own Claude** (`claude -p`) or a **local Ollama** —
the container can't run either, so this native worker does it and POSTs the result back. Install with
`./deploy-coach-worker.sh`. Engine resolves via `COACH_ENGINE=auto` → `claude` CLI if present → Ollama if
`CC_OLLAMA_URL` set → off. Content-free and zero-egress (your own model). Without it, the Coach scene still
hands off to the standalone **`jaid-coach`** skill.

---

## Layer 5 — Fleet (opt-in): watch your other machines

Run the dashboard once; on each **other** Claude Code machine run a **watcher** that POSTs token facts to
the dashboard's `/ingest`. Install per host with `./deploy-watcher.sh` (run it **on that machine**):
```bash
CC_COLLECTOR_URL=http://<dashboard-host>:8099/ingest ./deploy-watcher.sh
```
Set `CC_INGEST_TOKEN` (matching the dashboard) if the dashboard requires one. For a traveling laptop, point
the URL at the dashboard's **Tailscale** IP. The watcher is **read-only and passive** (no hooks, no latency).
Want the statusline/precise-state on a fleet host too? Wire layer 3 there as well.

---

## Layer 6 — Optional integrations

### Semantic search (Ollama) — 🔍 Search scene
The Search scene (full-text over your transcripts) is **opt-in and default-OFF**; enable it in-app (it indexes
into a **separate** DB served on a **loopback-only** port — the "content firewall"). Keyword search needs
nothing extra. **Meaning-based** search additionally needs a **local Ollama** with an embedding model:
```
CC_EMBED_MODEL=nomic-embed-text
CC_OLLAMA_URL=http://host.docker.internal:11434   # or http://localhost:11434 native
```
(then `ollama pull nomic-embed-text`). Still zero third-party egress — it's your own model.

### MCP-tax probe (`mcp_probe.py`) — 🧩 MCP scene
Shows which configured MCP servers are **dead weight** (enabled but unused) and the real context-token floor
they add. Run on a host that has the `claude` CLI; it POSTs **server names + connection status only** to
`/mcp-ingest` — **never** commands, URLs, headers, or env (no secrets leave the host). The 🧩 MCP scene's
"Set up MCP tracking" card gives a copy-paste command to run it (one-off, or wire it as a `SessionStart` hook).

### Self-update check (`CC_UPDATE_URL`) — opt-in
Unset by default = no outbound calls. When set to the public manifest URL, the server polls it every ~6h and
shows a banner if a newer release exists. Outbound GET of a tiny JSON only — **zero user data sent**, never
auto-applies.

---

## Layer 7 — Security & data lifecycle

- **`CC_ACCESS_PIN`** — set it to require a PIN to view the UI (PIN → long-lived cookie) **and** to gate every
  mutating action (reply, import, prune). Empty = open on the LAN. **Required** for answer-from-phone.
- **`CC_INGEST_TOKEN`** — require remote watchers/probes to send `X-CC-Token`. Any long random string.
- **Encrypted export / import** (🧹 Maintenance scene) — AES-256-GCM, encrypt-before-write; move or back up
  your data as a single encrypted file you put anywhere. See [`DATA_EXPORT.md`](DATA_EXPORT.md).
- **Maintenance** (🧹 scene) — WAL-checkpoint, prune, vacuum; 14-day content-index auto-purge. See
  [`CONTENT_PRIVACY.md`](CONTENT_PRIVACY.md).
- **Upgrades** — `git pull` + rebuild/restart; your data survives (it lives in a volume / `~/.cache`). See
  [`UPGRADE.md`](UPGRADE.md).

---

## The 9 UI scenes (swipe / nav between them)

Consolidated from an earlier 14 — several scenes that were really one job got merged: **Reports** folds the
old History + Trophy Room; the **Coach** hub tabs Score (Craft) · Coach · Savings (Efficiency); **Archive**
is now a filter on Triage; **About** folded into Help. The hamburger menu groups them: Live · Spend ·
Improve · Hygiene · System.

| Scene | Group | What it shows |
|------|------|----------------|
| 🎯 **Triage** | Live | who **needs you** (genuinely blocked) on top; working/idle below; context gauge per session. The **📋 Active / 🗂️ Archived** tabs appear here when you've hidden sessions (server-side, cross-device) — Archive is no longer its own scene |
| 💰 **Cost** | Spend | spend by account, burn rate ($/h), today vs baseline, plan rate-limit gauges (5h/7d) |
| 📊 **Reports** | Improve | **History** (all-time totals, daily spend bars, top projects, context-trajectory + Craft-signal trends) stacked above the **Trophy Room** (totals + streaks, calendar heatmap, time-of-day rhythm, model-mix, cache savings) |
| 🏅 **Coach** | Improve | tabbed hub — **Score** (Craft Score: Efficiency · Hygiene · Craft + grade, medals, recent-form & personal-best) · **Coach** (content-free session "report card" + AI narrative via the coach worker / `jaid-coach` skill) · **Savings** (opportunities never judgments: model right-sizing what-ifs, subscription ROI, reducible spend) |
| 🧩 **MCP** | Hygiene | configured vs used MCP servers (dead weight), real context-floor token cost; setup card |
| 🖥️ **Fleet** | Live | per-host health (🟢/🟡/🔴) — tells "idle but healthy" apart from "watcher down" |
| 🔍 **Search** | Hygiene | **opt-in, default-OFF, local-only** full-text + semantic search over transcripts (content firewall) |
| 🧹 **Maintenance** | System | storage stats, encrypted export/import, checkpoint/prune/vacuum |
| ℹ️ **About / Help** | System | legend + troubleshooting; how to run the `jaid-coach` skill; version, non-affiliation disclaimer, Terms & License |

---

## Environment variables — quick reference

Full annotated list with defaults: **[`.env.sample`](../.env.sample)** (Docker reads it automatically). The
ones you'll actually touch:

### Essentials
| Var | Default | Meaning |
|-----|---------|---------|
| `CC_HOST` | hostname | label for this machine (set it in Docker — container can't see the real name) |
| `CC_PORT` | 8099 | dashboard port |
| `CC_WINDOW_1M_MODELS` | (empty) | comma-sep model-id substrings to treat as a 1M window (transcripts omit the `[1m]` marker) |
| `TZ` | UTC | timezone for cost "today"/daily rollups (e.g. `America/Chicago`) |
| `CC_RUNTIME` | (docker) | set `native` for the no-Docker path |
| `CC_ACCOUNT` | auto | override the detected Claude account (rarely needed; see `.env.sample`) |

### Security
| Var | Default | Meaning |
|-----|---------|---------|
| `CC_ACCESS_PIN` | (empty) | PIN to view the UI + gate mutating actions; **required for answer-from-phone** |
| `CC_INGEST_TOKEN` | (empty) | shared token remote watchers/probes must send (`X-CC-Token`) |

### Feature toggles / integrations
| Var | Default | Meaning |
|-----|---------|---------|
| `CC_CONTENT_PORT` | 8100 | loopback-only port for the Search index (the content firewall) |
| `CC_EMBED_MODEL` / `CC_OLLAMA_URL` | (unset) | enable semantic search via your local Ollama |
| `COACH_ENGINE` | auto | in-app coach engine: `auto` / `claude` / `ollama` / `off` |
| `CC_UPDATE_URL` | (unset) | opt-in self-update manifest URL (no user data sent) |
| `CC_STRICT_NEEDS_ME` | on | "needs you" only for genuine blocks (permission / ask); `0` = any finished turn |

### Data locations (host)
| Var | Default | Meaning |
|-----|---------|---------|
| `CC_DB_DIR` | `~/.cache/cc-observability` | usage/cost history DB (read-write) |
| `CC_STATE_DIR_HOST` | `~/.cache/cc-dashboard` | statusline/state snapshots (read-only mount) |
| `CC_EXPORT_DIR_HOST` | `~/cc-observability-exports` | encrypted export bundles land here |

> **Advanced tuning** (poll intervals, staleness windows, timeouts, tail/cap sizes — `CC_POLL_INTERVAL`,
> `CC_STALE_SECS`, `CC_SSE_INTERVAL`, `CC_WAITING_RECENT_SECS`, `CC_SUBAGENT_*`, `CC_HOST_*_SECS`, the coach/
> responder/content timeouts, etc.) all have sensible defaults. Change them only if you have a reason; read
> the inline comment in `.env.sample` or the `os.environ` call in the source first.

### Naming note
The **product** is "JAID Observability Platform." The **package / CLI / container / cache-path** names and the
`CC_*` wire-protocol env vars are intentionally `cc-observability` / `CC_*` — that's deliberate, not stale
branding; don't expect those to be renamed.

<!-- TEMPLATE — do not edit in place. Your AI agent copies this to a working file (e.g.
     install-checklist.local.md, which is gitignored) and walks through it with you, checking boxes as it
     goes. Reference for every component: docs/COMPONENTS.md. Agent rules of engagement: AGENTS.md. -->

# JAID — Install Checklist (TEMPLATE)

**For the AI agent:** Copy this file to `install-checklist.local.md` and work through *that* copy with the
user, ticking each box as you complete it. **Follow [`AGENTS.md`](AGENTS.md): before every step that changes
the system (installs a package, creates a venv, writes/edits `.env` or `settings.json`, installs/enables a
service, needs `sudo`, or writes outside this repo), tell the user what you're about to do and get a yes
first.** The user can say "just do the whole thing, stop asking" to waive per-step confirmation for the
session. Skip the optional sections the user doesn't want — they're optional by design. What each component
is and whether it's default vs opt-in: **[`docs/COMPONENTS.md`](docs/COMPONENTS.md)**.

**For the user:** This walks you through installing the dashboard and choosing which optional pieces you
want. Only the first two sections are required; everything else is opt-in. Nothing leaves your machine by
default.

---

## 0. Decisions (fill these in first)

- [ ] **Runtime:** ☐ Docker (always-on server) ☐ Native (laptop / personal machine) — see COMPONENTS §Layer 1
- [ ] **This host's name** (`CC_HOST`): `________`
- [ ] **Do you use a 1M-context model?** (e.g. Opus) → note its id for `CC_WINDOW_1M_MODELS`: `________`
- [ ] **Timezone** for cost rollups (`TZ`, e.g. `America/Chicago`): `________`
- [ ] **Set a PIN?** (required if you'll answer-from-phone; recommended on any shared network): ☐ yes ☐ no
- [ ] **Which optional pieces?** (tick what you want; details in their sections below)
  - [ ] In-terminal gauge — statusline + state hooks (§4) ⭐ *recommended — this is the `🟢 34% · 🧠 long context` line*
  - [ ] Answer-from-phone (§5)
  - [ ] In-app coach (§6)
  - [ ] Semantic search via Ollama (§7)
  - [ ] MCP-tax tracking (§8)
  - [ ] Self-update check (§9)
  - [ ] Watch other machines / fleet (§10)

## 1. Prerequisites

- [ ] Claude Code has run on this host at least once (so `~/.claude/projects` exists). The dashboard fills in
      from there; an empty dashboard just means no transcripts yet.
- [ ] **Docker runtime:** Docker + compose installed. **Native runtime:** `python3` (3.12+) available.
- [ ] (Answer-from-phone, later) `tmux` installed and you run Claude Code inside it.
- [ ] Repo present at `~/git/jaid-observability-platform` (clone it if not — ask the user for the URL).

## 2. Install the dashboard — DO ONE

### 2a. Docker (recommended for servers)
- [ ] `mkdir -p ~/.cache/cc-dashboard` (state-snapshot mount target)
- [ ] `cp .env.sample .env` *(ask before writing if one exists — never clobber)*
- [ ] Set `CC_HOST` (+ `CC_WINDOW_1M_MODELS`, `TZ`) in `.env`
- [ ] `docker compose up -d --build`  *(installs/builds an image — confirm first)*

### 2b. Native (recommended for a laptop) — or run the `jaid-setup` skill instead
- [ ] `python3 -m venv .venv && ./.venv/bin/pip install -e .`  *(pulls numpy + cryptography — confirm first)*
- [ ] `cp .env.sample .env`; set `CC_HOST`, `CC_RUNTIME=native`, `CC_WINDOW_1M_MODELS`, `TZ`
- [ ] `./install-user-services.sh`  *(installs `cc-collector` + `cc-watcher` systemd --user services + enables linger — confirm first)*
- [ ] ⚠️ **Do NOT run `install-user-services.sh` on a Docker host** — it would double up the collector/watcher.

## 3. Verify the dashboard is up

- [ ] `curl -fsS http://localhost:8099/health` → `{"ok":true,"version":"…","runtime":"…"}`
- [ ] Open `http://<this-host>:8099` on your desktop or phone; the 🎯 Triage scene loads.
- [ ] (If you set a PIN in §11) log in once with the PIN.

## 4. ⭐ In-terminal gauge — statusline + state hooks (opt-in, recommended, PER HOST)

> This is the `● brave-otter · Opus 4.8 · 🟢 34% · 🧠 long context` line in your terminal, and it also feeds
> the dashboard authoritative window/cost/rate-limits. **It is NOT installed by default** — it edits Claude
> Code's `~/.claude/settings.json`. See COMPONENTS §Layer 3. **The `jaid-setup` skill can do this for you
> (it asks first and backs up `settings.json`).** Wire it on every host you want the gauge on.

- [ ] **Back up** `~/.claude/settings.json` first *(ask before editing it)*.
- [ ] Add the **statusline**:
      `"statusLine": { "type": "command", "command": "<repo>/hooks/cc-statusline.sh" }`
- [ ] Add the **state hooks** to all 7 events (`SessionStart`, `UserPromptSubmit`, `PreToolUse`,
      `PostToolUse`, `Stop`, `Notification`, `SessionEnd`), each as
      `"command": "<repo>/hooks/cc-state-hook.sh <EventName>"` (see COMPONENTS §Layer 3 for the exact shape).
- [ ] Use the **absolute path to THIS checkout** (`~/git/jaid-observability-platform/hooks/...`).
- [ ] Validate the JSON (`python3 -c "import json;json.load(open('~/.claude/settings.json'))"`).
- [ ] Start a **new** Claude Code session → the status line shows the gauge; the dashboard now shows exact
      cost + a `1M`/`200k` badge + rate-limit gauges for this host.

## 5. Answer-from-phone (opt-in) — needs §4 + a PIN

- [ ] State hooks (§4) wired (they capture the tmux pane). You run Claude Code inside `tmux`.
- [ ] `CC_ACCESS_PIN` set (§11) — **required**; `/reply` is refused without it (it's RCE into your shell).
- [ ] `CC_INGEST_TOKEN` set (§11).
- [ ] `./deploy-responder.sh`  *(installs the `cc-responder` systemd --user service — confirm first)*
- [ ] (Docker host) `docker compose up -d --force-recreate` so the server picks up the PIN/token.
- [ ] Test: tap a waiting session on the phone → reply → it lands in the right tmux pane.

## 6. In-app coach (opt-in)

- [ ] Decide the engine: your `claude` CLI (uses your tokens) or a local Ollama (`COACH_ENGINE` / `CC_OLLAMA_URL`).
- [ ] `./deploy-coach-worker.sh`  *(installs the `cc-coach-worker` systemd --user service — confirm first)*
- [ ] Open the 🧠 Coach scene → "Generate in-app" produces a narrative. (Without this, Coach hands off to the
      standalone `jaid-coach` skill instead.)

## 7. Semantic search via Ollama (opt-in)

- [ ] Keyword search needs nothing extra — just enable the 🔍 Search scene in-app (default-OFF, local-only).
- [ ] For meaning-based search: a local Ollama is running and `ollama pull nomic-embed-text` done.
- [ ] Set `CC_EMBED_MODEL=nomic-embed-text` + `CC_OLLAMA_URL=...` in `.env`; recreate/restart the dashboard.
- [ ] ⚠️ Search shows real conversation content — it's walled to a loopback-only port and carries a
      "do not screenshot/share" banner. Keep it on a trusted machine.

## 8. MCP-tax tracking (opt-in)

- [ ] On a host with the `claude` CLI, run `python3 mcp_probe.py` (the 🧩 MCP scene's "Set up MCP tracking"
      card gives the exact command, incl. wiring it as a `SessionStart` hook).
- [ ] Confirm the 🧩 MCP scene lists your servers (names + status only — no secrets leave the host).

## 9. Self-update check (opt-in)

- [ ] Set `CC_UPDATE_URL` to the public manifest in `.env` (see `.env.sample` for the URL); restart.
- [ ] Leave unset for **zero** outbound calls. When set, it only GETs a tiny version JSON — no user data sent.

## 10. Watch other machines / fleet (opt-in) — run ON each other host

- [ ] On the dashboard host, note its address (LAN IP or Tailscale IP).
- [ ] On each other machine: `CC_COLLECTOR_URL=http://<dashboard>:8099/ingest ./deploy-watcher.sh`
      *(installs a `cc-watcher` systemd --user service — confirm first)*.
- [ ] If the dashboard set `CC_INGEST_TOKEN`, set the same on the watcher host.
- [ ] (Optional) wire §4 statusline/hooks on the fleet host too for the gauge + precise state there.
- [ ] Confirm the 🖥️ Fleet scene shows the host green.

## 11. Security (recommended)

- [ ] `CC_ACCESS_PIN` in `.env` — gates the UI + every mutating action (reply/import/prune).
- [ ] `CC_INGEST_TOKEN` in `.env` — if any remote watcher/probe reports in (any long random string).
- [ ] Recreate/restart the dashboard after changing these.
- [ ] Keep the dashboard on a trusted network (LAN / Tailscale), never the public edge — especially with
      answer-from-phone enabled.

## 12. Data & upgrades (good to know)

- [ ] Back up via the 🧹 Maintenance scene's **encrypted export** (see `docs/DATA_EXPORT.md`).
- [ ] Upgrades: `git pull` + (`docker compose up -d --build` | restart the native service). Data survives
      (it's in a volume / `~/.cache`). See `docs/UPGRADE.md`.

## 13. Done — report to the user

- [ ] Dashboard URL + the PIN (if set).
- [ ] Which optional pieces were installed, and the service names (so they can `systemctl --user restart …`).
- [ ] Where data + exports live; point them at `docs/COMPONENTS.md` for anything they skipped.

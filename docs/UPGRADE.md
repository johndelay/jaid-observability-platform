# Upgrading JAID Observability Platform (and what protects your data)

JAID Observability Platform is built so the **app code and your data are separate** — you can replace the app
(even across major versions) without losing history. This doc explains the boundary and the safety net.

## The data/config boundary

Two tiers live in the SQLite DBs under the writable data dir (`/data/db` in Docker;
`~/.cache/cc-observability/` native):

### User data — sacred, never auto-discarded
Migrations may *transform* it; they never drop it.

| Where | What |
|---|---|
| `cc-usage.db` → `usage` | per-message token/cost rows (content-free) |
| `cc-usage.db` → `events` | behavioral events (compaction, tool_error, mcp_call, …) |
| `cc-usage.db` → `rate_history` | live rate-limit snapshots (NOT rebuildable from transcripts) |
| `cc-usage.db` → `session_account` | which Claude account ran a session |
| `cc-usage.db` → `session_prefs` | archive flags / nicknames |
| `cc-usage.db` → `mcp_reports` | latest MCP probe per host (names + status only) |
| `cc-content.db` | opt-in full-text/semantic index of raw transcripts |

Most of `usage`/`events`/`cc-content.db` is **rebuildable** from the on-disk transcripts
(`~/.claude/projects/**/*.jsonl`); `rate_history` and the per-session settings are **not** — they're
captured live. That's why the export bundle (Slice 3) prioritizes the non-rebuildable slice.

### App state — rebuildable / resettable
Safe to `DROP` and rebuild on upgrade; the app reconstructs it.

| Pattern | What |
|---|---|
| `meta` keys prefixed `cfg.*` | user-set configuration (retention window, toggles, …) |
| tables prefixed `rollup_*` | derived aggregates (rebuilt from `usage`/`events`) |
| tables prefixed `cache_*` | transient caches |
| per-device `localStorage` | UI prefs (scene order, masking) — lives in the browser, not the server |

## Schema migrations

On startup the app runs **ordered, forward-only migrations** keyed on `meta['schema_version']`
(in both `store.py` and `content_index.py`). Each migration is idempotent; a pre-versioning DB is
baselined automatically. You never run migrations by hand — starting the new version is enough.

## Pre-upgrade auto-backup (Slice 3)

Before a migration advances `schema_version`, the app auto-writes a content-free export bundle to the
export dir (`pre-upgrade-<oldversion>-<ts>.json.gz`). If an upgrade goes wrong, you can restore from it.

## How to upgrade

- **Docker:** `docker compose pull && docker compose up -d`
- **Native (pipx):** `pipx upgrade cc-observability` (or `git pull` + restart your service)

Your data dir is a mounted volume / a fixed path, so it survives the swap. The in-app update banner
(Slice 6) tells you when a new version is available and shows the right command for your runtime.

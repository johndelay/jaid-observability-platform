# Changelog

All notable user-facing changes to the **JAID Observability Platform** (`cc-observability`) are recorded
here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/), and the project aims
to follow [Semantic Versioning](https://semver.org/).

This is a curated, user-facing log — for the full commit history see the Git log.

## [Unreleased]

### Changed
- **The session % gauge now shows distance-to-auto-compaction, not raw context-window fill.** It used to
  read `tokens / full window` (e.g. 93%), which trailed Claude Code's own "100% context used" indicator —
  because Claude Code compacts at the window *minus* a reserved output/compaction buffer, not at the hard
  ceiling. The number, bar, color, sort, "fullest"→"to compact" summary chip, and drill-in now report
  `pct_compact`: Claude Code's authoritative `used_percentage` when the statusline feed supplies it (so the
  dashboard hits 100% exactly when Claude Code compacts), falling back to a computed `pct_to_compact` on
  watcher-only hosts. Raw window fill is retained on the % tooltip and in the drill-in. The absolute-token
  "dumb zone" bands are unchanged; their on-bar threshold needles were rescaled to the new denominator so
  they stay aligned.
- **Suggestions & Reading card** now auto-advances to the next article every 20 seconds (pauses while you
  hover or focus it; the ‹ › arrows still work and reset the timer).
- The article **title in the Suggestions card is now clickable** — it opens the article in a new tab, the
  same as the "Read →" link.
- **Nav bar:** the scene-name label is now a fixed width, so the ► / ◄ buttons no longer shift sideways
  when you move between scenes (the button stays put under your cursor).

### Documentation
- **`.env.sample`** now documents the optional settings the app already supported but the sample omitted:
  the local-search content port (`CC_CONTENT_PORT`), semantic-search via a local Ollama
  (`CC_EMBED_MODEL` / `CC_OLLAMA_URL`), and the advanced data-directory overrides. The `CC_INGEST_TOKEN`
  note now explains it can be any long random string (you never type it — it's used programmatically).
- **README:** the Docker quick-start now reminds you to `mkdir -p ~/.cache/cc-dashboard` before the first
  run, and notes that an empty dashboard on first load is expected until Claude Code has run on the host.

## [0.13.0] — 2026-06-07

First public release of the JAID Observability Platform — a self-hosted, local-first dashboard for your
own Claude Code / AI-assistant usage. Runs entirely on your machine; by default it egresses nothing.

### Features
- **Live session view** — token/context fuel gauge with how close you are to auto-compaction, plus
  best-effort subagent activity, viewable from your phone.
- **Cost & burn tracking** — per-session and daily cost estimates, burn rate, and a time-to-compact ETA
  (estimates derived from local data and published rates — always reconcile against your vendor's billing).
- **Multi-scene UI** — Triage, Cost, History, Trophy Room, Craft score, Coach, Efficiency, MCP, Fleet,
  Archive, Search, Maintenance, and About — swipeable, with a screenshot-safe masking mode by default.
- **Craft score, medals, and trophy room** — honor-system, local-only self-improvement metrics and reporting.
- **Efficiency coach** — opportunity-framed, clearly-labeled estimates (model-downgrade what-ifs,
  subscription ROI, rate-limit headroom, reducible-spend bands). Never judgments.
- **MCP usage detector** — see which configured MCP servers are actually used vs. dead weight, plus the
  real billed context floor, without ever reading your MCP config or secrets.
- **Local transcript search** (opt-in) — full-text keyword search and optional semantic search (via your
  own local Ollama model) over your transcripts, served behind a loopback-only content firewall.
- **Encrypted export / import** — move or back up your data as an AES-256-GCM-encrypted file.
- **Data lifecycle** — schema migrations, a maintenance engine, content auto-purge, opt-in redaction, and
  a pre-upgrade auto-backup.
- **Opt-in self-update check** — fetches a public version manifest to tell you when a newer release exists;
  sends no data and never auto-applies.
- **Two runtimes** — a single Docker container, or a native install driven by the
  `cc-observability-setup` Claude Code skill. Multi-host watchers and answer-from-phone are optional.

[Unreleased]: https://github.com/johndelay/jaid-observability-platform/compare/v0.13.0...HEAD
[0.13.0]: https://github.com/johndelay/jaid-observability-platform/releases/tag/v0.13.0

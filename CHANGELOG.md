# Changelog

All notable user-facing changes to the **JAID Observability Platform** (`cc-observability`) are recorded
here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/), and the project aims
to follow [Semantic Versioning](https://semver.org/).

This is a curated, user-facing log — for the full commit history see the Git log.

## [Unreleased]

_Nothing yet._

## [0.15.0] — 2026-07-20

A drill-in pass: the context-trajectory chart now shows values you can actually read and compare, the
drill-in stops going stale while you watch it, and the dashboard can no longer claim to be a release it
isn't.

### Added
- **Y-axis on the context-trajectory chart.** Ticks for the peak, the 200k danger and 120k drift
  boundaries, and 0, with dashed gridlines at the boundaries. The chart previously showed the *shape* of a
  session's context growth with no indication of the values, so you could see that it climbed but not where
  it had climbed to. Boundaries above the plotted range are omitted rather than stacked on the top label.
- **Manual refresh button in the drill-in** — pulls the trajectory, the event counts and the activity
  stream at once, so you never need a page reload to get current numbers.
- **Build stamp on `/health`** (`build`, `release`, `version_display`). `VERSION` is bumped per release, so
  between releases a running instance reported a clean release number while actually serving post-tag code.
  The stamp is `v<VERSION>` when built from exactly the tag on a clean tree, else a short commit sha
  (+`-dirty`); the About scene shows `0.15.0+<sha>` and a *dev build* marker. An image built without a stamp
  reports `release: null` ("build unknown") rather than claiming to be the release. `version` itself stays
  bare semver, so the update check is unaffected. Use `scripts/rebuild.sh` to bake the stamp in.
- **MCP scene: filter by reporting host** — chips to show one host's MCP usage or all, persisted per device.
- **Cost scene: the ten priciest sessions**, with host, project, cost and token count.
- **More in-app help** — "?" buttons on Maintenance and on Export/Import explaining each function, and a
  standing section under Search explaining that transcript search is off by default, why, and how to enable it.

### Changed
- **The trajectory chart's y-axis is now absolute by default.** It used to fit the ceiling to the session's
  own peak — and since context only grows during a session, that pinned the newest point to the top of the
  box every time. A 45k session and a 240k session drew the same picture, and the zone bands landed
  somewhere different in each. The ceiling now snaps to a coarse ladder (150k / 250k / 400k / 700k / 1M),
  so the bands hold still across sessions and heights are comparable. A **fit** checkbox on the chart
  restores the old per-session scaling for shape detail; the lowest rung sits above 120k so the drift band
  is always on the plot.
- The Coach scene's call-to-action and headline are visually promoted, using the brand accent rather than
  the semantic state colours.
- `docs/RELEASING.md` corrected — it referenced a sync script that no longer exists and omitted the
  changelog-promotion and tagging steps. Now also documents that release tags land on the merge commit on
  `main`, which is why the build stamp compares against the tag directly instead of using `git describe`.

### Fixed
- **The drill-in went stale while open.** Stats and activity refreshed every 2.5s, but the context
  trajectory and behavioral-event counts were fetched once when the drill-in opened and never again — so
  the graph froze for as long as you kept looking at it. Both now refresh every 30s.
- **A page reload threw you back to the first scene.** The carousel now remembers the scene you were on
  (by scene ID, so enabling or disabling a scene doesn't restore the wrong one).

### Security
- Removed a real hostname from two code comments. This repository is public; the value was an internal
  host name with no business being in it.

## [0.14.0] — 2026-07-19

Six weeks of work since 0.13.0: the JAID rebrand, two security-review batches, a scene consolidation,
macOS watcher support, and the session color picker.

### Added
- **Set a session's identity color from the dashboard.** The drill-in now has a swatch row with Claude
  Code's eight `/color` names plus a ⟲ reset. Picking one does two things: it persists the color (so the
  session's badge shows it on every device, replacing the hash-derived hue), and — when the session has a
  live tmux target — it injects `/color <name>` through the existing answer-from-phone path so Claude
  Code's own prompt bar matches. That makes a session identifiable at a glance in both places, and
  deliberately *chosen* rather than assigned by a hash. Sessions without a tmux target still get the badge
  color; the picker says so rather than failing silently. The color is stored in `session_prefs.color`
  (schema migration 003) and `/pref` allowlists it to the eight known names.
- **Launch-a-session instructions**, in the README and as a 🚀 modal in the hamburger menu — how to start a
  session the dashboard can write back to. Leads with the tmux requirement, because it's the only piece
  that can't be added to an already-running session.
- **macOS watcher support** — an agent-followable install guide (`docs/WATCHER_MACOS.md`) and a
  `jaid-watcher-macos` skill for adding a Mac (including Claude Desktop agent-mode sessions) to the fleet.
- **Privacy modal** in the menu — the no-egress posture and what's stored in plaintext, in the app.
- **❓ Help & troubleshooting modal** — a copy-able prompt that points your own Claude at the container
  logs and repo, plus links to the docs and issues.
- **🧠 JAID Coach modal** explaining the `/jaid-coach` skill.
- **Per-scene "?" help** on Search, Cost, History and Maintenance.
- **Model right-sizing** — all current Claude models priced (Fable 5, Mythos 5, …) with family-fallback
  matching, plus a deterministic recommendation card and a per-project / per-session breakdown of the
  downgrade opportunity.
- **MCP scene: turn-off candidates** — servers never called or stale, with last-used times and a
  `/context` helper button.
- **Trophy scene**: clickable day squares and hour-slot squares open a stats popup.
- **Dumb-zone threshold tick marks** on session fill bars, with hover tooltips, plus a cited Advisory
  explaining the heuristic (and noting it isn't Anthropic-acknowledged).
- **Opt-in self-update check** — `CC_UPDATE_URL` wired through Compose and documented. Outbound GET only;
  it never auto-applies anything.

### Security
- **Security review batch 1** — fixed an attribute-context XSS, added login throttling, confined import
  paths, added security headers, and a port guard preventing the LAN port from colliding with the
  loopback content port.
- **Security review batch 2** — the control plane (`/outbox`, `/outbox/result`) now fails closed behind a
  token gate, the auth cookie rotates, and the content database is written `0600`.
- **Cross-platform file-permission hardening** for sensitive data at rest.

### Changed
- **Rebranded to "JAID Observability Platform"** across the UI header, PWA manifest and skills — the PWA
  no longer names itself "Claude Code", and the skills are now `/jaid-coach` and `/jaid-setup`. Package,
  container and wire-protocol names remain `cc-observability` / `CC_*` by design.
- **Scenes consolidated 14 → 9** (Reports, a Coach hub, Archive as a filter, merged About/Help, grouped
  menu), and the carousel reordered so Fleet is last and Search second-from-last.
- **"Needs you" now means genuinely blocked** — a permission prompt or a question, enforced server-side,
  rather than every finished turn. Only waiting sessions that actually have a last message get promoted
  to the hero slot.
- **The dumb zone renders as a yellow→orange→red range** rather than a single note, and the 🧠 pip hover
  names the current band.
- **Suggestions card** floats in a bottom dock so the list scrolls behind it.
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
- **Hamburger menu** section titles restyled (neon-green pills, sized down after an initial pass).

### Fixed
- **Statusline-only hosts no longer wipe transcript-derived state.** A host running the statusline but not
  the watcher's full parse could blank a session's state on ingest.
- **The Suggestions opt-in checkbox couldn't be checked**; the article list also pointed at the wrong repo.
- **Two tests pinned fixture dates against a rolling 30-day window** and went red six weeks later with the
  code still correct. Fixtures feeding a windowed query are now relative to now, and the window filter is
  asserted directly instead of exercised by accident.

### Documentation
- **`.env.sample`** now documents the optional settings the app already supported but the sample omitted:
  the local-search content port (`CC_CONTENT_PORT`), semantic-search via a local Ollama
  (`CC_EMBED_MODEL` / `CC_OLLAMA_URL`), and the advanced data-directory overrides. The `CC_INGEST_TOKEN`
  note now explains it can be any long random string (you never type it — it's used programmatically).
- **README:** the Docker quick-start now reminds you to `mkdir -p ~/.cache/cc-dashboard` before the first
  run, and notes that an empty dashboard on first load is expected until Claude Code has run on the host.
- **Install checklist + components reference** (`TEMPLATE_INSTALL_CHECKLIST.md`, `docs/COMPONENTS.md`),
  documenting the statusline and filling gaps in the setup docs.
- **`AGENTS.md` guardrails** — an AI agent working in this repo must ask before changing anything outside
  the checkout (packages, services, dotfiles, `sudo`), and be explicit about what it did.
- **Egress and network claims precised** across the docs to match reality (the only outbound calls are
  opt-in GitHub fetches).

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

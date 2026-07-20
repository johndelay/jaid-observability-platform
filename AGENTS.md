# AGENTS.md — instructions for AI agents working in this repo

This file applies to any AI coding agent (Claude Code, Cursor, etc.) operating in this repository or
running its install/setup skills. Read it first.

## Operating principles (read first) — be careful, be polite, ask before you reach outside this repo

You are a **guest on the user's machine.** This project's skills (notably `jaid-setup`) can install software
and background services, and the dashboard reads the user's Claude Code transcripts — so a careless agent can
do real damage or surprise the user. Default to caution.

**Before any action that changes the system beyond this repository's own working tree, STOP and ask the user
for explicit confirmation first** — say plainly what you're about to do and why, then wait for a yes.

This is the **default precedence**, set from the start. **The user can override it at any time** ("just do
it", "you don't need to ask before installing", "go ahead and edit my profile") — when they do, honor that
for the rest of the session. Their say-so always wins; this rule only governs what happens *absent* it.

**Ask first before you:**
- **install, upgrade, or remove packages** — `pip`/`pipx`/`apt`/`brew`/`npm`/`dnf`/any system package
  manager, or creating a venv that pulls dependencies;
- **install, enable, start, stop, or modify background services** — `systemd` (system or `--user`),
  `launchd`, Windows Task Scheduler, cron, or anything that runs at login/boot (incl. `loginctl enable-linger`);
- **modify the user's profile / dotfiles / environment** — `~/.bashrc`, `~/.profile`, `~/.zshrc`,
  `~/.claude/settings.json`, shell `PATH`, login items, etc.;
- **change OS, security, or filesystem settings** — firewall rules, file permissions/ownership, or anything
  that needs `sudo`/root;
- **write outside this repo's working tree** — the user's home dir, `/etc`, `/data`, system paths — or
  **delete or overwrite files you did not create**;
- **make outbound network calls** beyond what an explicitly-requested step requires (this project is
  self-hosted and content-free by design — don't break that).

**You can do these without asking:** read this repo, run its tests (`./verify.sh`), edit files inside the
checkout, build/run the local container or app to test it, and answer questions. When in doubt, ask — keep it
brief, then proceed once cleared.

**Be polite and honest:** tell the user what you're doing and why before you do it; report what actually
happened, including failures and skipped steps; and don't nag — name a risk **once**, then defer to the
user's decision.

## What this project is

A phone-glanceable, **self-hosted, content-free** dashboard for Claude Code sessions (context/compaction
gauge, cost, fleet triage). Stdlib-Python server + vanilla-JS UI. No third-party data egress by default.
See `README.md` for architecture and the two install paths (native via the `jaid-setup` skill; Docker for
servers).

## Working in the code

- **Verify by running, never by eyeballing.** After any change, run `./verify.sh` (Python unit tests + a
  Node DOM smoke test + a real-Chromium Playwright E2E + live endpoint checks). Don't claim "done" until it's
  ALL GREEN. UI change → the DOM smoke + Playwright layers must pass; new endpoint/parser → add a test first.
- **Branch flow:** commit on `development`; merge to `main` only on the maintainer's explicit sign-off.
- **The Dockerfile COPYs source files explicitly** — a new `.py` module must be added to it or the container
  won't see it. Rebuild with `docker compose up -d --build --force-recreate` and confirm the change is baked in.
- **Naming:** the product is "JAID Observability Platform"; the package / CLI / container / cache-path names
  are intentionally `cc-observability` and the wire-protocol env vars are `CC_*` — don't "fix" those.
- **Never use the semantic state colours decoratively.** `--red` / `--amber` / `--orange` / `--yellow` /
  `--green` encode *state* everywhere in the UI (compaction proximity, dumb-zone bands, alerts). Reusing one
  to make something look nice makes it read as an alarm. For emphasis use the brand tokens — `--primary`,
  `--secondary`, `--accent` — and the existing idioms: the accent ring
  (`box-shadow:0 0 0 1px var(--accent), …`) for a highlighted card, and the `linear-gradient(90deg,
  var(--primary), var(--accent))` text treatment for a hero heading.
- **Secrets:** never write secrets into the repo or tracked files. The only credential is `CC_ACCESS_PIN`,
  which lives in `.env` (gitignored).

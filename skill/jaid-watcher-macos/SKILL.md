---
name: jaid-watcher-macos
description: Add a Mac (especially one running Claude Desktop) to an existing JAID / cc-observability fleet by installing the passive watcher as a launchd LaunchAgent. Detects which transcript dirs exist (Claude Code CLI ~/.claude/projects and/or Claude Desktop agent-mode ~/Library/Application Support/Claude/local-agent-mode-sessions), writes the plist, handles the macOS-specific gotchas (CC_PROJECTS_DIRS override, Full Disk Access, token-aware build), loads it, and verifies the host reports in. Use when the user asks to "add my Mac to the dashboard", "watch Claude Desktop on this Mac", "install the JAID watcher on macOS", or to add a macOS reporting host. This sets up a REPORTING host only — to install the dashboard itself, use jaid-setup.
---

# JAID watcher on macOS (add a Mac to the fleet)

You are installing the **passive watcher** on THIS Mac so its Claude sessions report into an
**already-running** JAID / cc-observability dashboard. This is a *reporting host only* — it does not run
the dashboard. The watcher is read-only and makes only LAN POSTs to the user's own dashboard (no
third-party egress).

The full, sanitized runbook is **`docs/WATCHER_MACOS.md`** — read it; this skill is the driver that
executes it carefully on a real machine.

> **Ask before you touch the system — be careful, be polite (see `AGENTS.md`).** Before each step that
> changes anything outside a repo checkout — copying `watcher.py` into place, writing the LaunchAgent
> plist, granting Full Disk Access, loading the agent — tell the user what you're about to do and get a
> yes first. The user can say "just do the whole install, don't ask each step" to waive this for the
> session. Absent that, ask.

## What you're setting up
A `launchd` LaunchAgent (`~/Library/LaunchAgents/com.ccobs.watcher.plist`) that runs `watcher.py`,
polls this Mac's Claude transcripts, and POSTs token facts to the dashboard's `/ingest`.

## Know this before you start (the macOS realities)
- **Claude Desktop only** = a **watcher-only** host: you get session state, "needs you", context
  tokens, and the dumb-zone bar — **not** cost/burn/rate-limits (those need the Claude Code statusline
  hook, which Desktop agent mode doesn't run). Tell the user this up front so the gaps aren't a surprise.
- Transcripts come from **Claude Desktop agent mode** (`~/Library/Application Support/Claude/local-agent-mode-sessions`)
  and/or **Claude Code CLI** (`~/.claude/projects`). Plain Desktop chat writes nothing watchable.
- `CC_PROJECTS_DIRS` **replaces** the default root — to capture both sources, list **both paths**.
- A launchd agent may need **Full Disk Access** (granted to `python3`) to read
  `~/Library/Application Support/`. Symptom if missing: green but **0 sessions** despite files existing.

## Procedure

1. **Get the inputs.** Ask the user for: the dashboard ingest URL (`http://<host>:8099/ingest`) and the
   `CC_INGEST_TOKEN` **if** the dashboard requires one. Pick a `CC_HOST` label for this Mac. Confirm
   `python3 --version` works (else `xcode-select --install`). **Treat the token as a secret — never echo
   it, never commit it, never write it to `/tmp`.**

2. **Place `watcher.py`** under `~/.local/share/cc-observability/`. Clone the repo or copy the one file.
   Verify it's the token-aware build: `grep -c X-CC-Token watcher.py` must print **2** (0 = stale → 401s).

3. **Discover transcript dirs.** Check both `~/.claude/projects` and
   `~/Library/Application Support/Claude/local-agent-mode-sessions`. Build `CC_PROJECTS_DIRS` from
   whichever exist (comma-separated full paths) — usually both.

4. **Write the plist** (`~/Library/LaunchAgents/com.ccobs.watcher.plist`) per `docs/WATCHER_MACOS.md`,
   with `CC_COLLECTOR_URL`, `CC_HOST`, `CC_INGEST_TOKEN` (if any), `CC_PROJECTS_DIRS`, and
   `StandardErrorPath`/`StandardOutPath` for debuggability. **Set the token without exposing it** — edit
   with `plistlib` and feed the value via stdin (see the doc's "Agent tip"), not as a command argument.
   Then `plutil -lint` the file.

5. **Grant Full Disk Access** to the interpreter (`/usr/bin/python3` or the Homebrew one) if watching
   `~/Library/Application Support/`. This is a manual System Settings step — walk the user through it.

6. **Load and verify locally:** `launchctl load` the plist, then `launchctl list | grep ccobs`
   (PID, exit 0) and `tail /tmp/ccobs-watcher.err` — the startup line must list **both** configured
   `roots=[...]`.

7. **Verify on the dashboard:** the host should appear **green** in the Fleet scene. If the Mac has
   been idle longer than `CC_ACTIVE_HOURS` (default 6h), **0 sessions is correct** — have the user run
   Claude on the Mac and re-check within the window.

8. **Report:** the host label, that it's watcher-only (and what that omits), where the logs are
   (`/tmp/ccobs-watcher.*`), and the manage/uninstall commands from the doc.

## Guardrails
- Don't write the plist, grant FDA, or load the agent without confirming. Never print the ingest token,
  never commit it, never put it in `/tmp` or any tracked file — it belongs only in the plist under the
  user's home.
- This installs a **reporting host**. If the user actually wants the dashboard on this Mac, use the
  `jaid-setup` skill instead.
- Full details, troubleshooting table, and rotation/uninstall: `docs/WATCHER_MACOS.md`.

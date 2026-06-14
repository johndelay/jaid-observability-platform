# CLAUDE.md

Instructions for AI agents working in this repo live in **[`AGENTS.md`](AGENTS.md)** — read it first.

**Most important rule (precedence, user-overridable):** you are a guest on the user's machine. **Before any
action that changes the system beyond this repository's own working tree — installing/removing packages,
installing or modifying background services (systemd/launchd/Task Scheduler/cron), editing the user's profile
or dotfiles or `~/.claude/settings.json`, changing OS/security/filesystem settings, anything needing `sudo`,
or writing outside the checkout — STOP and ask the user for explicit confirmation first.** Be careful and be
polite. The user can override this at any time ("just do it"); honor that once they say so. Full list and
rationale: `AGENTS.md`.

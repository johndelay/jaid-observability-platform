---
name: jaid-setup
description: Install and run the cc-observability dashboard NATIVELY (no Docker) on this machine, driven by Claude Code. Detects the OS, sets up a Python venv, seeds config with a generated PIN, installs a per-OS background service (systemd --user / launchd / Task Scheduler), starts it, and verifies /health. Use when the user asks to "install cc-observability", "set up the Claude Code dashboard", "run the dashboard without Docker", or to add a new machine to their fleet. For an always-on homelab server, prefer the Docker path in the README instead.
---

# JAID native setup (the cc-observability dashboard)

You are installing the **cc-observability dashboard** on THIS machine, natively (no Docker). The audience runs
Claude Code, so you do the per-OS wiring by reasoning about the host. The app is self-hosted, content-free, and
makes no third-party network calls.

> **Ask before you touch the system — be careful, be polite (see `AGENTS.md`).** This skill installs a Python
> venv (pulling packages), writes config, and installs a background service that runs at login. **Before each
> step that changes anything outside this repo's checkout — installing packages, creating the venv, writing
> `.env`, installing/enabling the background service, or anything needing `sudo` — tell the user exactly what
> you're about to do and get a yes first.** The user can say "just do the whole install, don't ask each step"
> to waive this for the session — then proceed. Absent that, ask. Walk the user through it; don't surprise them.

## What you're setting up
A single Python process (`server.py`) that reads this host's `~/.claude/projects` transcripts read-only and
serves the dashboard on `http://localhost:8099`. Native mode binds the raw-content search port to `127.0.0.1`
directly (no Docker, so no dual-port firewall needed). Data lives under `~/.cache/cc-observability/`.

## Procedure

1. **Locate or clone the repo.** If `~/git/jaid-observability-platform` exists, use it. Otherwise clone it
   (`git clone <the repo url> ~/git/jaid-observability-platform`) — ask the user for the URL if you don't know it.
   `cd` there for the rest.

2. **Python env + deps.** Create a venv and install:
   ```bash
   python3 -m venv .venv
   ./.venv/bin/pip install -e .          # editable: keeps web/ assets in the checkout
   ```
   (`pip install -e .` pulls numpy + cryptography. If wheels are unavailable, fall back to
   `./.venv/bin/pip install -r requirements.txt`.)

3. **Seed `.env`** (only if missing — never clobber an existing one). Copy `.env.sample` to `.env`, then set:
   - `CC_HOST` = this machine's short hostname.
   - `CC_RUNTIME=native`
   - `CC_ACCESS_PIN` = a freshly generated PIN (e.g. `python3 -c "import secrets;print(secrets.randbelow(10**6))"`).
     **Tell the user this PIN and that it gates the UI + all mutating actions (reply/import/prune).**
   - `CC_DB_PATH=~/.cache/cc-observability/cc-usage.db`, `CC_CONTENT_DB_PATH=~/.cache/cc-observability/cc-content.db`,
     `CC_EXPORT_DIR=~/cc-observability-exports` (create the dir).
   - Optional: `CC_OLLAMA_URL=http://localhost:11434` + `CC_EMBED_MODEL=nomic-embed-text` to enable semantic
     search (only if the user runs Ollama with that model pulled).
   - Leave `CC_UPDATE_URL` unset unless the user wants the opt-in update check (it makes an outbound version GET).

4. **Install a background service** for the user's OS (confirm first):
   - **Linux (systemd --user):** write `~/.config/systemd/user/cc-observability.service` with
     `ExecStart=%h/git/jaid-observability-platform/.venv/bin/cc-observability`, `WorkingDirectory=%h/git/jaid-observability-platform`,
     `EnvironmentFile=-%h/git/jaid-observability-platform/.env`, `Restart=on-failure`, `WantedBy=default.target`. Then
     `systemctl --user daemon-reload && systemctl --user enable --now cc-observability`. (`loginctl enable-linger
     $USER` so it runs without an active session.)
   - **macOS (launchd):** write `~/Library/LaunchAgents/com.ccobs.dashboard.plist` running the venv's
     `cc-observability` with the checkout as the working dir and the `.env` values as `EnvironmentVariables`;
     `launchctl load` it.
   - **Windows:** create a Task Scheduler task (at-logon) running `.venv\Scripts\cc-observability.exe`, or
     instruct the user to run it in a terminal. Set env vars from `.env`.

5. **Verify.** Hit `http://localhost:8099/health` → expect JSON `{"ok":true,"version":"...","runtime":"native"}`.
   Then open the UI, log in with the PIN, and confirm the Triage scene loads. If search is wanted, enable it in
   the 🔍 Search scene (it's local-only and opt-in).

6. **Report** the URL, the PIN, the service name (so they can `systemctl --user restart` etc.), and where data +
   exports live. Point them at `docs/DATA_EXPORT.md` (backup) and `docs/UPGRADE.md` (upgrades: `git pull` +
   restart the service; data survives because it's under `~/.cache`).

## Guardrails
- Don't install services without confirming. Don't overwrite an existing `.env`. Never put secrets in the repo
  or in tracked files. The PIN is the only credential and it lives in `.env` (gitignored).
- This is the native path. For an always-on multi-host server, the Docker path (`docker compose up -d`) in the
  README is usually the better fit — mention it if that's what they're doing.

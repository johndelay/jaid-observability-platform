# Compliance & TOS posture

> **Not legal advice.** This is the project's good-faith reasoning about how it relates to Anthropic's
> terms, written so the design rationale is captured for contributors and an eventual open-source release.
> Terms change — re-check the live [Usage Policy](https://www.anthropic.com/legal/aup),
> [Consumer Terms](https://www.anthropic.com/legal/consumer-terms), and
> [Commercial Terms](https://www.anthropic.com/legal/commercial-terms) before relying on this.

## What this tool does (and deliberately does not do)

cc-observability is a **passive, local observability layer** for Claude Code:

- **Reads your own local transcript files** (`~/.claude/projects/**/*.jsonl`) **read-only**. These are files
  Claude Code writes to disk; the schema is documented by Anthropic.
- **Uses documented, supported Claude Code features** — `hooks` and the `statusLine` command — to capture
  session state and the authoritative status JSON Anthropic pipes to the statusline.
- **Computes a cost estimate locally** from a static price table (the ccusage model). When Claude's own
  authoritative cost is present (via the statusline feed) the UI shows that instead.
- **Answer-from-phone** types a reply into your **own** running Claude Code session via `tmux send-keys` —
  i.e. it drives the **real, official `claude` CLI**, exactly as if you typed at the keyboard.

It does **not**:

- ❌ extract, store, forward, or reuse OAuth tokens or any Claude credentials;
- ❌ make any calls to the Anthropic API or `claude.ai`;
- ❌ implement or impersonate ("spoof") the Claude Code harness, or run its own model client;
- ❌ send your transcripts, prompts, or costs to any third party (self-hosted, LAN/Tailscale only).

## Why that matters for Anthropic's terms

Anthropic's 2026 enforcement against third-party tools targets a **specific** pattern: tools that
**extract OAuth tokens from a Pro/Max subscription and use them in their own API client**, "spoofing the
Claude Code harness." The publicly stated distinction is:

> Calling the actual `claude` CLI (Anthropic's official product) = **allowed**.
> Extracting OAuth tokens for a third-party API client = **banned**.

cc-observability is squarely in the first category: it reads local files and drives the genuine CLI. It
makes no API calls and never handles credentials, so the token-reuse / harness-spoofing prohibition does
not apply. Anthropic's clarifications do not address passive monitoring / observability tooling, and a
broad ecosystem of similar local tools (e.g. ccusage and various Claude Code monitors) operates the same way.

### One thing to keep clean: answer-from-phone stays human-in-the-loop

The Consumer Terms restrict accessing the service through "automated or non-human means." Answer-from-phone
is **remote human input**, not automation: a person initiates every reply and it flows through the official
CLI. **Do not** build auto-reply / unattended-bot behavior on top of it — that would move it toward the
automated-access line. Keep a human deciding each message.

## Transport choice (Tailscale, not Cloudflare) — a privacy decision, not a TOS one

For off-LAN access the project uses **Tailscale** (WireGuard, end-to-end encrypted device-to-device) and
**avoids a Cloudflare Tunnel in the data path**. This is **not** an Anthropic-TOS requirement — transport
is orthogonal to Anthropic's terms. It's a **data-confidentiality** choice: a Cloudflare Tunnel terminates
TLS at Cloudflare's edge, so Cloudflare could see your dashboard traffic — which is your Claude activity
(prompts, code, file paths, costs) — in plaintext. Keeping it on your own encrypted overlay avoids putting
a third party in the middle of potentially sensitive (incl. work-confidential) conversation data.

## Before publishing / open-sourcing

- Re-read the current Usage Policy + Consumer/Commercial Terms (above) — they change.
- Run the repo's sanitization review on anything that leaves your machine.
- Keep this file honest: if a feature ever makes an API call, handles a token, or automates input, update
  this posture first.

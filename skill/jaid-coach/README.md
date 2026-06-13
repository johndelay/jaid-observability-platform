# JAID Coach — a zero-infra Claude Code coaching Skill

Your own Claude, coaching you on how well you use Claude Code — **efficiency** (cache use), **context
hygiene** (compaction discipline, dumb-zone time), and **craft** (skills, subagents) — read straight from
your local `~/.claude/projects` transcripts.

- **No server, no Docker, no third party.** It runs off your transcripts. (If you also run the
  [cc-observability](../../README.md) dashboard, Coach uses its richer, authoritative metrics automatically.)
- **Content-free & local-only.** Token counts, event types, and timestamps — never conversation text — and
  nothing ever leaves your machine.
- Scoring is shared with the dashboard (`craft.py` / `medals.py`), so the numbers match.

## Install

Symlink (or copy) this directory into your Claude skills folder:

```bash
ln -sfn "$(pwd)/skill/jaid-coach" ~/.claude/skills/jaid-coach   # from the repo root
```

Then in any Claude Code session: **`/jaid-coach`** — or just ask *"coach me on my Claude usage"*,
*"how's my craft score?"*, *"am I improving?"*.

## Optional config

| Env var | Default | Meaning |
|---|---|---|
| `CC_URL` | `http://localhost:8099` | A cc-observability dashboard to read from (falls back to standalone if unreachable). |
| `CC_ACCESS_PIN` | — | The dashboard's PIN, if its UI is gated. |
| `CC_PROJECTS_DIR` | `~/.claude/projects` | Where your transcripts live. |

`python3 coach_metrics.py --standalone` forces the no-dashboard scan and prints the raw metrics JSON.

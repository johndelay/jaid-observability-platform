# Export, import & maintenance

The 🧹 **Maintenance** scene is your data's control panel: see how much it's using, move it between machines,
and keep it from growing forever. It's screenshot-safe (sizes and counts only — no conversation content).

## Export (back up / move to a new machine)

What's in the bundle: a **content-free** copy of your history — cost rows, behavioral events, rate-limit
history, account/session settings, and config. It does **not** include your raw transcripts or the search
index (those are separate; see below).

1. Open the Maintenance scene → **Export (encrypted)**.
2. Enter a **passphrase**. The bundle is gzipped and AES-256-GCM encrypted *before it's written* — only
   ciphertext ever touches the disk.
3. The file lands in the export dir (shown in the Storage card; `~/cc-observability-exports` by default).
   Download it from the **download ↓** link, or let SyncThing/rclone carry it off-box.

**⚠ Always export encrypted before sharing or uploading. Store the passphrase in 1Password (or your vault) —
if you lose it, the file is unrecoverable.** A plaintext export exists (`plaintext (local only)`) for local
use only; it's loudly gated.

## Import (restore / merge a machine)

Maintenance scene → **Import…** → give the path to an export file on this host (and its passphrase if it's
`.enc`). You get a **preview** ("N new rows would be merged") before anything is written. Merge is
**idempotent** (`INSERT OR IGNORE`): re-importing the same file adds nothing, and importing several machines'
exports combines them safely.

## Maintenance (keep it lean for years)

- **Checkpoint WAL** — lossless; truncates the write-ahead logs. This also runs automatically in the
  background, so you rarely need the button.
- **Prune…** — preview-then-confirm removal of old `rate_history` (the densest grower) and content orphaned by
  deleted transcripts. Nothing is deleted without your confirmation.
- **Vacuum / reclaim** — compacts the database files after a prune. Can take a few seconds; best run on the host.

The content search index has its own **14-day auto-purge** (see `CONTENT_PRIVACY.md`).

## The three data layers (what's where)

| Layer | Contains | Backed up by |
|---|---|---|
| `cc-usage.db` | content-free history (cost/events/rate/settings) | the **export bundle** above |
| `cc-content.db` | opt-in raw transcript search index | rebuildable by re-indexing (not in the bundle) |
| `~/.claude/projects` | your raw transcripts (the source of truth) | Claude Code / your own backups |

Upgrades never touch your data — see `UPGRADE.md`.

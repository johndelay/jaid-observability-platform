# Releasing cc-observability

This document describes the end-to-end steps to cut a new release and activate the
opt-in self-update checker for users who have set `CC_UPDATE_URL`.

---

## 1. Bump the version

Run the bump script with the new version (must be `MAJOR.MINOR.PATCH`):

```sh
scripts/bump-version.sh 0.14.0
```

This updates **both** files that must stay in sync:
- `version.py` — `VERSION = "..."`
- `pyproject.toml` — `version = "..."`

The script prints a reminder about the remaining manual steps.

---

## 2. Update the public manifest

In the **public** repo (`johndelay/jaid-observability-platform`), update `manifest.json`:

```json
{
  "version": "0.14.0",
  "name": "JAID Observability Platform",
  "notes_url": "https://github.com/johndelay/jaid-observability-platform/releases/tag/v0.14.0"
}
```

The `CC_UPDATE_URL` for users points at:
```
https://raw.githubusercontent.com/johndelay/jaid-observability-platform/main/manifest.json
```

The server's `update_check_loop()` GETs this URL, reads `manifest["version"]`, and
compares it against the local `version.VERSION` using `version.is_newer()`. No user
data is ever sent; the check is outbound GET only; it never auto-applies anything.

---

## 3. Sync a sanitized snapshot to the public repo

A gated sync script genericizes and gates the snapshot: `scripts/sync-to-public.sh`. It copies the
tracked source tree into the published checkout, applies the genericization rules, runs a fingerprint
gate that **aborts before any commit/push** if a sensitive fingerprint survives, and never overwrites
the public-managed files (`README.md`, `TERMS.md`, `manifest.json`, `LICENSE`, `COMPLIANCE.md`,
`CHANGELOG.md` — hand-edit those directly in the published checkout).

```sh
bash scripts/sync-to-public.sh            # dry-run: review the diff + confirm "Fingerprint gate: CLEAN ✓"
bash scripts/sync-to-public.sh --push     # commit + push the published repo (only after a clean dry-run)
```

Because it copies the whole current tree (not a diff) it is idempotent — one run brings the published
checkout fully current and sweeps up any earlier unsynced drift. As a second guard for combination
risks, also run `/sanitize-review` (Claude Code skill) on the checkout before sharing a release widely.

---

## 4. Cut the GitHub release

```sh
gh release create v0.14.0 \
  --repo johndelay/jaid-observability-platform \
  --title "v0.14.0" \
  --notes "See CHANGELOG for details."
```

---

## 5. Self-update checker summary

Users who want update notifications add one line to their `.env`:

```sh
CC_UPDATE_URL=https://raw.githubusercontent.com/johndelay/jaid-observability-platform/main/manifest.json
```

- Default: **empty** — no outbound check at all.
- When set: the server polls the manifest every `CC_UPDATE_INTERVAL` seconds (default 21600 = 6 h).
- The `#updatebar` banner appears in the UI when `manifest["version"]` is newer than the
  running `version.VERSION`.
- The per-runtime upgrade verb (`docker compose pull && docker compose up -d` for Docker;
  `pipx upgrade cc-observability` for native installs) is shown in the banner.
- **Never auto-applies.** The user runs the upgrade command manually.

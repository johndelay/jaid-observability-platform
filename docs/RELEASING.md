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

> **NOTE: The gated sanitizing sync script is Part A2 — not yet built.**
>
> Until A2 is complete, perform a **manual sanitized copy** of the source tree:
> 1. Copy changed files from `~/git/jaid-observability-platform/` to `~/git/jaid-observability-platform/`.
> 2. Run `/sanitize-review` (Claude Code skill) on the public repo checkout **before pushing**
>    to catch any homelab fingerprints (real hostnames, internal IPs, credentials, table
>    prefixes, combination-of-innocuous-details risks).
> 3. Confirm the review is clean, then `git push` the public repo.

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

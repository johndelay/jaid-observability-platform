# Releasing JAID Observability Platform

End-to-end steps to cut a release and activate the opt-in self-update checker for users who have set
`CC_UPDATE_URL`.

> **This repo IS the public repo** (`johndelay/jaid-observability-platform`). There is no separate private
> tree and no sanitizing sync step — that split was retired. Everything below happens in this checkout.
> (If you find a reference to `scripts/sync-to-public.sh` anywhere, it is stale: the script no longer
> exists. The `sync: release from private HEAD` commits in the log are fossils of the old flow.)

Work on `development` throughout; `main` is only ever reached by a merge (step 5).

---

## 1. Promote the changelog

`CHANGELOG.md` accumulates under `## [Unreleased]` as work lands. Turn that into a dated release heading
and open a fresh empty one:

```md
## [Unreleased]

_Nothing yet._

## [0.14.0] — 2026-07-19
```

**Check the section is actually complete before promoting it.** Entries get missed — 0.14.0 was cut with
only ~8 bullets recorded against 57 non-merge commits, and the gap included two security-review batches.
Diff the log against the last tag and fill in what's missing:

```sh
git log --oneline --no-merges v0.13.0..HEAD
```

Give **security fixes their own `### Security` section** rather than folding them into `### Changed`.
They're the entries most likely to decide whether someone upgrades, and they should be findable.

---

## 2. Bump the version

```sh
scripts/bump-version.sh 0.14.0        # must be MAJOR.MINOR.PATCH
```

Updates **both** files that must stay in sync:
- `version.py` — `VERSION = "..."`
- `pyproject.toml` — `version = "..."`

---

## 3. Update `manifest.json`

This is the file the self-update checker fetches. Bump `version` to match:

```json
{
  "version": "0.14.0",
  "name": "JAID Observability Platform",
  "notes_url": "https://github.com/johndelay/jaid-observability-platform/releases/latest"
}
```

**Leave `notes_url` pointing at `/releases/latest`, not at a version-specific tag URL.** A tag URL 404s
during the window between pushing the tag (step 5) and publishing the release (step 6) — and that is
exactly the window in which a user's dashboard may fetch it.

`CC_UPDATE_URL` points users at:
```
https://raw.githubusercontent.com/johndelay/jaid-observability-platform/main/manifest.json
```
Note it reads from **`main`**, so a release isn't visible to users until step 5 lands.

The server's `update_check_loop()` GETs that URL, reads `manifest["version"]`, and compares it against the
local `version.VERSION` via `version.is_newer()`. No user data is sent; outbound GET only; never
auto-applies.

---

## 4. Verify

```sh
./verify.sh            # or: CC_URL=http://<host>:8099 ./verify.sh
```

Must end **✅ ALL GREEN** — python unit tests, node DOM smoke, real-Chromium Playwright, syntax, and live
endpoint checks. If the Playwright layer is testing a running container, **rebuild it first** or you are
testing the previous build:

```sh
scripts/rebuild.sh
```

Use the script rather than `docker compose up -d --build` directly: it bakes the build stamp (`CC_BUILD`)
into the image, which is what lets `/health` distinguish the tagged release from post-tag code. A plain
compose build still works and is honest about it — `/health` reports `release: null` and the About scene
says *"build unknown"* — but it can't confirm a release.

> **Tag the release AFTER this step but note where the tag lands.** `v0.14.0` points at the *merge commit
> on `main`* (`706db9b`), which is **not an ancestor of `development`** — so `git describe` run from
> `development` walks straight past it and reports `v0.13.0-<n>-g<sha>`. That's why the build stamp compares
> `HEAD` against the tag's commit directly instead of using `git describe`. If you ever switch to tagging on
> `development` before the merge, revisit `scripts/rebuild.sh`.

---

## 5. Merge to `main`, tag, push

Merging to `main` requires the maintainer's explicit sign-off (see `AGENTS.md`).

```sh
git commit -am "Release 0.14.0"
git checkout main
git merge --no-ff development -m "Merge development: release 0.14.0"
git tag -a v0.14.0 -m "JAID Observability Platform 0.14.0"
git push origin main
git push origin v0.14.0
git checkout development && git push origin development     # keep the branches level
```

Before pushing, sanity-check the outgoing diff — this repo is public:

```sh
git diff origin/main development | grep -E '^\+' | grep -niE '192\.168\.|10\.[0-9]+\.|\.internal|/home/[a-z]+|BEGIN [A-Z ]*PRIVATE KEY'
```

⚠️ **An empty grep result is not by itself proof of a clean scan** — it looks identical to a grep that
received no input. Confirm the pipeline works with a positive control that *should* match:

```sh
git diff origin/main development | grep -c '^+'      # non-zero = there is a diff to scan
```

For combination risks (individually-harmless details that together fingerprint a private environment),
also run the `/sanitize-review` skill over the outgoing diff before a widely-shared release.

---

## 6. Cut the GitHub release

```sh
gh release create v0.14.0 \
  --repo johndelay/jaid-observability-platform \
  --title "v0.14.0" \
  --notes "See CHANGELOG.md for details."
```

Do this promptly after step 5 — `notes_url` points at `/releases/latest`, so until a release exists the
banner's "what's new" link lands on the previous one.

---

## 7. Self-update checker summary

Users who want update notifications add one line to their `.env`:

```sh
CC_UPDATE_URL=https://raw.githubusercontent.com/johndelay/jaid-observability-platform/main/manifest.json
```

- Default: **empty** — no outbound check at all.
- When set: the server polls the manifest every `CC_UPDATE_INTERVAL` seconds (default 21600 = 6 h).
- The `#updatebar` banner appears when `manifest["version"]` is newer than the running `version.VERSION`.
- The per-runtime upgrade verb (`docker compose pull && docker compose up -d` for Docker;
  `pipx upgrade cc-observability` for native) is shown in the banner.
- **Never auto-applies.** The user runs the upgrade command manually.

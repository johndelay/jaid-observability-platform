#!/usr/bin/env bash
# Rebuild + recreate the container WITH a build stamp, so /health can tell a tagged release from post-tag
# code. Plain `docker compose up -d --build` still works — it just leaves CC_BUILD empty, and the dashboard
# then says "build unknown" instead of claiming to be exactly the tagged release.
#
#   scripts/rebuild.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

# NB: deliberately NOT `git describe`. Release tags land on the MERGE COMMIT on main (v0.14.0 -> 706db9b),
# which is not an ancestor of development — so describe run from development walks back past it and reports
# the PREVIOUS tag (v0.13.0-67-g...), which is worse than no stamp at all. Comparing HEAD to the tag's commit
# directly is independent of branch topology.
VERSION="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' version.py)"
[ -n "$VERSION" ] || { echo "ERROR: could not read VERSION from version.py" >&2; exit 1; }

if ! head_sha="$(git rev-parse --short HEAD 2>/dev/null)"; then
  CC_BUILD=""
  echo "build stamp: (unavailable — not a git checkout; /health will report build unknown)" >&2
else
  dirty=""
  git diff --quiet HEAD -- 2>/dev/null || dirty="-dirty"
  tag_sha="$(git rev-parse -q --verify "v${VERSION}^{commit}" 2>/dev/null || true)"
  if [ -z "$dirty" ] && [ -n "$tag_sha" ] && [ "$tag_sha" = "$(git rev-parse HEAD)" ]; then
    CC_BUILD="v${VERSION}"            # exactly the tagged release, clean tree
  else
    CC_BUILD="${head_sha}${dirty}"    # anything else is a dev build and says so
  fi
  echo "build stamp: ${CC_BUILD}"
fi
export CC_BUILD

docker compose up -d --build --force-recreate

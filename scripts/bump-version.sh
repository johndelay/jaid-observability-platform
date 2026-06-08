#!/usr/bin/env bash
# bump-version.sh — update version.py and pyproject.toml to the given version.
# Usage: scripts/bump-version.sh <NEW_VERSION>
# Example: scripts/bump-version.sh 0.14.0
set -euo pipefail

NEW_VERSION="${1:-}"

# Validate: must look like N.N.N (digits, dots only in the semver part; allow leading 'v').
STRIPPED="${NEW_VERSION#v}"
if [[ -z "$STRIPPED" ]] || ! [[ "$STRIPPED" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: version argument must be N.N.N (e.g. 0.14.0 or v0.14.0), got: '${NEW_VERSION}'" >&2
    exit 1
fi
NEW_VERSION="$STRIPPED"

# Resolve paths relative to the repo root (the directory containing this script's parent).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VERSION_PY="$REPO_ROOT/version.py"
PYPROJECT_TOML="$REPO_ROOT/pyproject.toml"

if [[ ! -f "$VERSION_PY" ]]; then
    echo "ERROR: $VERSION_PY not found" >&2
    exit 1
fi
if [[ ! -f "$PYPROJECT_TOML" ]]; then
    echo "ERROR: $PYPROJECT_TOML not found" >&2
    exit 1
fi

# Update version.py: replace  VERSION = "..."
sed -i "s/^VERSION = \"[^\"]*\"/VERSION = \"${NEW_VERSION}\"/" "$VERSION_PY"

# Update pyproject.toml: replace  version = "..."  (only the first occurrence, in [project])
sed -i "0,/^version = \"[^\"]*\"/{s/^version = \"[^\"]*\"/version = \"${NEW_VERSION}\"/}" "$PYPROJECT_TOML"

echo "Bumped to ${NEW_VERSION}:"
echo "  $VERSION_PY    -> VERSION = \"${NEW_VERSION}\""
echo "  $PYPROJECT_TOML -> version = \"${NEW_VERSION}\""
echo ""
echo "Remaining manual steps:"
echo "  1. Update manifest.json in the public repo (johndelay/jaid-observability-platform):"
echo "       { \"version\": \"${NEW_VERSION}\", ... }"
echo "  2. Sanitize + sync the public snapshot (see docs/RELEASING.md — A2 not yet built; manual for now)."
echo "  3. Cut the GitHub release:"
echo "       gh release create v${NEW_VERSION} --repo johndelay/jaid-observability-platform"

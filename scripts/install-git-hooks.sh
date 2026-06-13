#!/usr/bin/env bash
# install-git-hooks.sh — install this repo's versioned git hooks into the local
# .git/hooks dir. Run once per fresh clone / per machine (hooks are not shared by git).
#
# Usage:  scripts/install-git-hooks.sh
#
# Idempotent: re-running just refreshes the copies. POSIX/bash-3.2 friendly (works on
# macOS's stock bash too). Uses `git rev-parse --git-path hooks` so it's correct under
# worktrees and non-default git dirs.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
src="$repo_root/scripts/git-hooks"
# Resolve the hooks dir (may be relative to the git dir); make it absolute.
hooks_rel="$(git -C "$repo_root" rev-parse --git-path hooks)"
case "$hooks_rel" in
  /*) dst="$hooks_rel" ;;
  *)  dst="$repo_root/$hooks_rel" ;;
esac

if [ ! -d "$src" ]; then
  echo "ERROR: no hooks source dir at $src" >&2
  exit 1
fi
mkdir -p "$dst"

n=0
for h in "$src"/*; do
  [ -f "$h" ] || continue
  name="$(basename "$h")"
  cp "$h" "$dst/$name"
  chmod +x "$dst/$name"
  echo "  installed: $name -> $dst/$name"
  n=$((n + 1))
done

if [ "$n" -eq 0 ]; then
  echo "ERROR: no hook files found in $src" >&2
  exit 1
fi
echo "✅ $n git hook(s) installed. Direct commits on 'main' are now blocked (escape: git commit --no-verify)."

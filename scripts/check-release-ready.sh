#!/usr/bin/env bash
# Pre-release gate. Run BEFORE promoting the changelog (docs/RELEASING.md step 1).
#
#   scripts/check-release-ready.sh              # check the working tree
#   scripts/check-release-ready.sh --self-test  # run the regression suite
#
# Exit 0 = ready, 1 = blocked.
#
# WHY THIS EXISTS. Twice in a row a release was cut with an under-recorded changelog:
#   - 0.14.0 shipped with ~8 bullets against 57 non-merge commits, missing two security-review batches.
#   - 0.15.0 was reached with `[Unreleased]` still reading "_Nothing yet._" against 8 commits, including a
#     real hostname stripped from two code comments in a PUBLIC repo.
# The second happened AFTER the first was written up in RELEASING.md — so prose demonstrably does not
# prevent it. The failure is structural: entries are written at release time from memory rather than as
# work lands, and the person cutting the release is the same person who would have to notice.
# Security entries are the ones most likely to decide whether someone upgrades, and they fail SILENTLY —
# nobody reads a changelog and notices what isn't there.
set -euo pipefail
cd "$(dirname "$0")/.."

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
[ -t 1 ] || { RED=""; GRN=""; YEL=""; BLD=""; RST=""; }

fail_count=0
say_fail() { printf '  %sFAIL%s %s\n' "$RED" "$RST" "$1"; fail_count=$((fail_count + 1)); }
say_pass() { printf '  %sPASS%s %s\n' "$GRN" "$RST" "$1"; }
say_note() { printf '  %s·%s    %s\n' "$YEL" "$RST" "$1"; }

# ---- pure helpers (no git, no filesystem beyond the file passed in) -------------------------------
# Text of the [Unreleased] section: everything between it and the next top-level "## " heading.
unreleased_section() { awk '/^## \[Unreleased\]/{f=1;next} /^## /{f=0} f' "$1"; }
count_bullets()      { grep -cE '^[[:space:]]*[-*] ' <<<"$1" || true; }
has_security()       { grep -qiE '^###[[:space:]]+Security' <<<"$1"; }

# Commit subjects that look security-relevant. Deliberately broad: a false positive costs one sentence in
# the changelog, a false negative ships an unannounced security fix.
SEC_RE='secur|CVE|vulnerab|hostname|credential|secret|password|token|leak|redact|sanitiz|escape|injection|traversal|auth|PIN'
security_subjects() { grep -iE "$SEC_RE" <<<"$1" || true; }

# ---- checks --------------------------------------------------------------------------------------
check_changelog() {
  local file="$1" subjects="$2" n_commits n_bullets section sec_hits
  section="$(unreleased_section "$file")"
  n_bullets="$(count_bullets "$section")"
  n_commits="$(grep -c . <<<"$subjects" || true)"
  [ -n "$subjects" ] || n_commits=0

  if [ "$n_commits" -eq 0 ]; then
    say_pass "no commits since the last release tag — nothing to record"
    return
  fi
  say_note "$n_commits non-merge commit(s) since the last tag; [Unreleased] has $n_bullets bullet(s)"

  if [ "$n_bullets" -eq 0 ]; then
    say_fail "[Unreleased] is empty but $n_commits commit(s) have landed since the last tag."
    printf '         Unrecorded commits:\n'
    sed 's/^/           /' <<<"$subjects"
    return
  fi
  say_pass "[Unreleased] has entries"

  sec_hits="$(security_subjects "$subjects")"
  if [ -n "$sec_hits" ] && ! has_security "$section"; then
    say_fail "security-relevant commit(s) since the last tag, but [Unreleased] has no '### Security' section."
    printf '         These may need their own Security entry (or confirm they are not user-facing):\n'
    sed 's/^/           /' <<<"$sec_hits"
  elif [ -n "$sec_hits" ]; then
    say_pass "security-relevant commits are covered by a '### Security' section"
  fi
}

# All three version strings must agree. version.py<->pyproject.toml is covered by a unit test; manifest.json
# was NOT covered by anything, and it is the file users actually fetch — a stale value there means every
# update banner in the fleet is wrong, silently, until someone notices by hand.
check_versions() {
  local v_py v_toml v_manifest
  v_py="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' version.py)"
  v_toml="$(sed -n 's/^version = "\([^"]*\)".*/\1/p' pyproject.toml | head -1)"
  v_manifest="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' manifest.json | head -1)"
  if [ "$v_py" = "$v_toml" ] && [ "$v_py" = "$v_manifest" ]; then
    say_pass "version consistent across version.py / pyproject.toml / manifest.json ($v_py)"
  else
    say_fail "version mismatch — version.py=$v_py pyproject.toml=$v_toml manifest.json=$v_manifest"
  fi
}

# ---- regression suite ----------------------------------------------------------------------------
# Every case below is a failure this gate is supposed to catch, including the exact 0.15.0 miss.
self_test() {
  local tmp rc=0 out
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' RETURN
  _case() { # name, expect(pass|fail), changelog-body, subjects
    local name="$1" expect="$2" body="$3" subjects="$4" got
    printf '%s\n' "$body" > "$tmp/CL.md"
    fail_count=0
    # NB: redirect, NEVER `out=$(check_changelog ...)`. Command substitution forks a subshell, so
    # say_fail's increments would be discarded and every case would report "pass" — the harness would
    # green-light a gate that was actually firing correctly. That is exactly what it did on first run.
    check_changelog "$tmp/CL.md" "$subjects" > "$tmp/out" 2>&1
    got=$([ "$fail_count" -eq 0 ] && echo pass || echo fail)
    if [ "$got" = "$expect" ]; then printf '  %sPASS%s self-test: %s\n' "$GRN" "$RST" "$name"
    else printf '  %sFAIL%s self-test: %s (expected %s, got %s)\n' "$RED" "$RST" "$name" "$expect" "$got"; sed 's/^/      /' "$tmp/out"; rc=1; fi
  }

  local EMPTY='## [Unreleased]

_Nothing yet._

## [0.15.0] — 2026-07-20
- old thing'
  local FILLED='## [Unreleased]

### Added
- a new thing

## [0.15.0] — 2026-07-20
- old thing'
  local WITHSEC='## [Unreleased]

### Security
- stripped a hostname

### Added
- a new thing

## [0.15.0] — 2026-07-20'

  # the exact 0.15.0 miss: "_Nothing yet._" with real commits behind it
  _case "empty Unreleased + commits => FAIL" fail "$EMPTY" 'feat: a thing
fix: another'
  _case "empty Unreleased + no commits => pass" pass "$EMPTY" ''
  _case "filled Unreleased + commits => pass" pass "$FILLED" 'feat: a thing'
  # the security half of the 0.14.0 miss
  _case "security commit + no Security section => FAIL" fail "$FILLED" 'docs: drop a real hostname from two code comments'
  _case "security commit + Security section => pass" pass "$WITHSEC" 'docs: drop a real hostname from two code comments'
  # a bullet count lower than the commit count is legitimate — one entry can cover several commits
  _case "fewer bullets than commits => pass (not an error)" pass "$FILLED" 'a
b
c
d
e'
  # guard the section parser: entries from the PREVIOUS release must not count as Unreleased content
  _case "only prior-release bullets => FAIL" fail '## [Unreleased]

## [0.15.0] — 2026-07-20
- old thing
- another old thing' 'feat: a thing'

  return $rc
}

# ---- main ----------------------------------------------------------------------------------------
if [ "${1:-}" = "--self-test" ]; then
  printf '%sRelease-gate regression suite%s\n' "$BLD" "$RST"
  self_test && { printf '  %sall self-tests passed%s\n' "$GRN" "$RST"; exit 0; } || exit 1
fi

# --changelog/--since let you replay a PAST release through the gate ("would this have caught it?"), which
# is the only way to show the check works on the failure it was written for rather than on fixtures alone.
CL_FILE="CHANGELOG.md"; TAG_OVERRIDE=""; REF="HEAD"
while [ $# -gt 0 ]; do
  case "$1" in
    --changelog) CL_FILE="$2"; shift 2 ;;
    --since)     TAG_OVERRIDE="$2"; shift 2 ;;
    --ref)       REF="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

printf '%sPre-release check%s\n' "$BLD" "$RST"

# Latest release tag by version order. NOT `git describe`: release tags land on the merge commit on main,
# which is not an ancestor of development, so describe walks past the newest tag and names the previous one.
TAG="${TAG_OVERRIDE:-$(git tag --list 'v[0-9]*' --sort=-v:refname | head -1)}"
if [ -z "$TAG" ]; then
  say_note "no release tag found — treating every commit as unreleased"
  SUBJECTS="$(git log --format='%h %s' --no-merges "$REF" | head -50)"
else
  say_note "comparing $TAG..$REF"
  SUBJECTS="$(git log --format='%h %s' --no-merges "$TAG..$REF")"
fi

check_changelog "$CL_FILE" "$SUBJECTS"
check_versions

if [ "$fail_count" -eq 0 ]; then
  printf '%s  ready to release%s\n' "$GRN" "$RST"; exit 0
fi
printf '%s  %d blocking issue(s) — fix before cutting the release%s\n' "$RED" "$fail_count" "$RST"; exit 1

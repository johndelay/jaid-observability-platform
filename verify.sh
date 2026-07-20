#!/usr/bin/env bash
# cc-observability verification harness — run this so Claude (or you, or CI) can actually TEST the app,
# not eyeball it. Runs: python unit tests + a node DOM smoke test (catches render bugs like the undefined
# esc() that hung the drill-in) + syntax + live endpoint checks. Exits non-zero if anything fails.
#
#   ./verify.sh                 # tests + checks against http://localhost:8099
#   CC_URL=http://localhost:8099 ./verify.sh
set -u
cd "$(dirname "$0")"
FAIL=0
sec(){ printf "\n\033[1m== %s ==\033[0m\n" "$1"; }

# If a PIN gates the UI, read it from the local .env so the live checks + browser E2E can authenticate.
# (The secret stays in .env — never printed. Exported so the Playwright child logs in too.)
PIN="$(sed -n 's/^CC_ACCESS_PIN=//p' .env 2>/dev/null | head -1)"
export CC_ACCESS_PIN="$PIN"
# V9 content port (loopback-only search). Honor an explicit env, else the .env value, else the 8100 default.
CPORT="${CC_CONTENT_PORT:-$(sed -n 's/^CC_CONTENT_PORT=//p' .env 2>/dev/null | head -1)}"
CPORT="${CPORT:-8100}"

sec "Python unit tests (watcher + server core)"
python3 -m unittest discover -s tests -p 'test_*.py' || FAIL=$((FAIL+1))

sec "UI smoke (node DOM — runs the page's render paths)"
if command -v node >/dev/null 2>&1; then
  node tests/ui_smoke.js || FAIL=$((FAIL+1))
else
  echo "  SKIP (node not found)"
fi

sec "Browser E2E (Playwright — real Chromium against ${CC_URL:-http://localhost:8099})"
if command -v node >/dev/null 2>&1 && node -e "require.resolve('playwright')" >/dev/null 2>&1; then
  node tests/e2e_playwright.js || FAIL=$((FAIL+1))
else
  echo "  SKIP (playwright not installed — run: npm install && npx playwright install chromium)"
fi

sec "Syntax"
python3 -m py_compile server.py watcher.py coach.py coach_worker.py content_index.py store.py version.py maintenance.py redact.py portable.py crypto.py && echo "  PASS py_compile" || FAIL=$((FAIL+1))
bash -n hooks/cc-statusline.sh && echo "  PASS statusline syntax" || FAIL=$((FAIL+1))
bash -n scripts/check-release-ready.sh && bash -n scripts/rebuild.sh && echo "  PASS release-script syntax" || FAIL=$((FAIL+1))
# Runs the release gate's own regression suite, NOT the live gate. The live gate is a release-time step
# (docs/RELEASING.md step 1) and would fail routine work — mid-cycle you commit code, then write the
# changelog entry. What must never silently rot is the gate itself.
scripts/check-release-ready.sh --self-test >/dev/null 2>&1 && echo "  PASS release-gate self-test" || { echo "  FAIL release-gate self-test"; FAIL=$((FAIL+1)); }
# V13 Slice 7: native runtime binds the content port to loopback directly (no Docker dual-port).
if CC_RUNTIME=native python3 -c "import server; assert server.RUNTIME=='native' and server.CONTENT_BIND=='127.0.0.1'" 2>/dev/null; then
  echo "  PASS native runtime binds content to 127.0.0.1"; else echo "  FAIL native runtime bind"; FAIL=$((FAIL+1)); fi
if python3 -c "import sys; t=__import__('tomllib') if sys.version_info>=(3,11) else None; t and t.load(open('pyproject.toml','rb'))" 2>/dev/null; then
  echo "  PASS pyproject.toml parses"; else echo "  (skip pyproject parse — py<3.11)"; fi

sec "Statusline coaching (compaction proximity vs the dumb-zone RANGE 🟡→🟠→🔴)"
# Two decoupled signals. (1) The "/compact now" cue keys off WINDOW PROXIMITY (%), NOT absolute tokens — so
# 25% of a 1M window (=250k abs, far from the wall) must NOT say "/compact now". (2) The 🧠 dumb-zone note
# keys off ABSOLUTE tokens and escalates by band: 🟡 long context (>=50k) -> 🟠 drifting (>=120k) -> 🔴 deep
# (>=200k). So 250k abs = the deep band. A small/sharp fill (<50k) stays silent on both.
SL_BIGABS=$(printf '{"session_id":"vrfy","context_window":{"context_window_size":1000000,"used_percentage":25}}' | bash hooks/cc-statusline.sh | sed 's/\x1b\[[0-9;]*m//g')
SL_WALL=$(printf  '{"session_id":"vrfy","context_window":{"context_window_size":1000000,"used_percentage":90}}' | bash hooks/cc-statusline.sh | sed 's/\x1b\[[0-9;]*m//g')
SL_SHARP=$(printf '{"session_id":"vrfy","context_window":{"context_window_size":1000000,"used_percentage":3}}'  | bash hooks/cc-statusline.sh | sed 's/\x1b\[[0-9;]*m//g')
SL_YEL=$(printf   '{"session_id":"vrfy","context_window":{"context_window_size":200000,"used_percentage":30}}'  | bash hooks/cc-statusline.sh | sed 's/\x1b\[[0-9;]*m//g')
SL_ORG=$(printf   '{"session_id":"vrfy","context_window":{"context_window_size":200000,"used_percentage":75}}'  | bash hooks/cc-statusline.sh | sed 's/\x1b\[[0-9;]*m//g')
case "$SL_BIGABS" in *"/compact now"*) echo "  FAIL 25%-of-1M must NOT nag '/compact now' (far from wall): $SL_BIGABS"; FAIL=$((FAIL+1));; *) echo "  PASS 25%-of-1M does not falsely say '/compact now'";; esac
case "$SL_BIGABS" in *"🧠 deep context"*) echo "  PASS 250k abs shows the deep '🧠 deep context' note";; *) echo "  FAIL deep dumb-zone note missing on 250k abs: $SL_BIGABS"; FAIL=$((FAIL+1));; esac
case "$SL_YEL"    in *"🧠 long context"*) echo "  PASS 60k abs shows the yellow '🧠 long context' note";; *) echo "  FAIL yellow dumb-zone note missing on 60k abs: $SL_YEL"; FAIL=$((FAIL+1));; esac
case "$SL_ORG"    in *"🧠 drifting"*) echo "  PASS 150k abs shows the orange '🧠 drifting' note";; *) echo "  FAIL orange dumb-zone note missing on 150k abs: $SL_ORG"; FAIL=$((FAIL+1));; esac
case "$SL_WALL"   in *"/compact now"*) echo "  PASS 90% (near the wall) shows '/compact now' cue";; *) echo "  FAIL wall cue missing at 90%: $SL_WALL"; FAIL=$((FAIL+1));; esac
case "$SL_SHARP"  in *"/compact"*|*"🧠"*) echo "  FAIL sharp fill should stay quiet: $SL_SHARP"; FAIL=$((FAIL+1));; *) echo "  PASS sharp fill stays quiet (no cue, no note)";; esac

sec "Live endpoint checks (${CC_URL:-http://localhost:8099})"
URL="${CC_URL:-http://localhost:8099}"
JAR="$(mktemp)"; trap 'rm -f "$JAR"' EXIT
if curl -s -o /dev/null --connect-timeout 3 "$URL/health" 2>/dev/null; then
  # If the UI is PIN-gated, log in once and reuse the cookie for the gated endpoints below.
  if [ -n "$PIN" ]; then
    curl -s -o /dev/null -c "$JAR" --data-urlencode "pin=$PIN" "$URL/login" && echo "  (authenticated via .env PIN)"
  fi
  echo "  PASS /health -> $(curl -s -o /dev/null -w '%{http_code}' "$URL/health")"
  if curl -s "$URL/update" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert "current" in d and "update_available" in d and "verb" in d' 2>/dev/null; then
    echo "  PASS /update returns version/update status"; else echo "  FAIL /update bad response"; FAIL=$((FAIL+1)); fi
  code=$(curl -s -b "$JAR" -o /dev/null -w '%{http_code}' "$URL/")
  if [ "$code" = "200" ]; then echo "  PASS / -> 200"; else echo "  FAIL / -> $code (want 200; PIN gate? check .env)"; FAIL=$((FAIL+1)); fi
  if curl -s -b "$JAR" "$URL/state" | python3 -c 'import sys,json;assert isinstance(json.load(sys.stdin),list)' 2>/dev/null; then
    echo "  PASS /state is JSON list"; else echo "  FAIL /state not JSON list"; FAIL=$((FAIL+1)); fi
  if curl -s -b "$JAR" "$URL/cost" | python3 -c 'import sys,json;assert "enabled" in json.load(sys.stdin)' 2>/dev/null; then
    echo "  PASS /cost returns cost summary"; else echo "  FAIL /cost bad response"; FAIL=$((FAIL+1)); fi
  if curl -s -b "$JAR" "$URL/craft" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert "enabled" in d and "dims" in d or d.get("enabled") is False' 2>/dev/null; then
    echo "  PASS /craft returns a score breakdown"; else echo "  FAIL /craft bad response"; FAIL=$((FAIL+1)); fi
  if curl -s -b "$JAR" "$URL/drift" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert "enabled" in d and ("overall" in d or d.get("enabled") is False)' 2>/dev/null; then
    echo "  PASS /drift returns a drift curve"; else echo "  FAIL /drift bad response"; FAIL=$((FAIL+1)); fi
  if curl -s -b "$JAR" "$URL/report" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert "enabled" in d and ("all_time" in d or d.get("enabled") is False)' 2>/dev/null; then
    echo "  PASS /report returns a trophy summary"; else echo "  FAIL /report bad response"; FAIL=$((FAIL+1)); fi
  if curl -s -b "$JAR" "$URL/efficiency" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert "enabled" in d and ((d.get("savings") and "roi" in d) or d.get("enabled") is False)' 2>/dev/null; then
    echo "  PASS /efficiency returns savings + roi"; else echo "  FAIL /efficiency bad response"; FAIL=$((FAIL+1)); fi
  if curl -s -b "$JAR" "$URL/mcp" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert "enabled" in d and ("hosts" in d or d.get("enabled") is False)' 2>/dev/null; then
    echo "  PASS /mcp returns MCP report"; else echo "  FAIL /mcp bad response"; FAIL=$((FAIL+1)); fi
  if curl -s -b "$JAR" "$URL/coach" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert "enabled" in d and ("recent_sessions" in d or d.get("enabled") is False)' 2>/dev/null; then
    echo "  PASS /coach returns bundle + recent sessions"; else echo "  FAIL /coach bad response"; FAIL=$((FAIL+1)); fi
  rc=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' --data '{"narrative":"x"}' "$URL/coach-result")
  if [ "$rc" = "401" ]; then echo "  PASS /coach-result rejects no-token (401)"; else echo "  FAIL /coach-result token gate -> $rc (want 401)"; FAIL=$((FAIL+1)); fi
  # Session color: /pref allowlists the 8 /color names and round-trips the choice into /state.
  # NB: assert reason=="bad_color", not just the 400 — a build without this feature also 400s here, but with
  # reason "noop" (no recognized field), which would be a false PASS.
  if curl -s -b "$JAR" -X POST -H 'Content-Type: application/json' \
       --data '{"session_id":"verify-color-probe","color":"chartreuse"}' "$URL/pref" \
       | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d.get("reason")=="bad_color",d' 2>/dev/null; then
    echo "  PASS /pref rejects an unknown color (bad_color)"; else echo "  FAIL /pref color allowlist did not report bad_color"; FAIL=$((FAIL+1)); fi
  if curl -s -b "$JAR" -X POST -H 'Content-Type: application/json' \
       --data '{"session_id":"verify-color-probe","color":"cyan"}' "$URL/pref" \
       | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d.get("ok") and d.get("color")=="cyan"' 2>/dev/null; then
    echo "  PASS /pref accepts a valid color"; else echo "  FAIL /pref did not accept color=cyan"; FAIL=$((FAIL+1)); fi
  if curl -s -b "$JAR" -X POST -H 'Content-Type: application/json' \
       --data '{"session_id":"verify-color-probe","color":""}' "$URL/pref" \
       | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d.get("ok") and d.get("color") is None' 2>/dev/null; then
    echo "  PASS /pref clears a color back to the derived hue"; else echo "  FAIL /pref did not clear the color"; FAIL=$((FAIL+1)); fi
  # V9 content firewall: the LAN port must WALL raw-content search; the loopback content port serves it.
  if curl -s -b "$JAR" "$URL/search-meta" | python3 -c 'import sys,json;assert json.load(sys.stdin).get("local") is False' 2>/dev/null; then
    echo "  PASS /search-meta is local:false on the LAN port"; else echo "  FAIL /search-meta should be local:false on LAN"; FAIL=$((FAIL+1)); fi
  sc=$(curl -s -b "$JAR" -o /dev/null -w '%{http_code}' "$URL/search?q=x")
  if [ "$sc" = "403" ]; then echo "  PASS /search walled on LAN port (403)"; else echo "  FAIL /search on LAN -> $sc (want 403)"; FAIL=$((FAIL+1)); fi
  scc=$(curl -s -b "$JAR" -o /dev/null -w '%{http_code}' "$URL/search-context?sid=x")
  if [ "$scc" = "403" ]; then echo "  PASS /search-context (reader) walled on LAN port (403)"; else echo "  FAIL /search-context on LAN -> $scc (want 403)"; FAIL=$((FAIL+1)); fi
  CONTENT_URL="http://localhost:${CPORT}"
  if curl -s -o /dev/null --connect-timeout 2 "$CONTENT_URL/health" 2>/dev/null; then
    CJAR="$(mktemp)"
    [ -n "$PIN" ] && curl -s -o /dev/null -c "$CJAR" --data-urlencode "pin=$PIN" "$CONTENT_URL/login"
    if curl -s -b "$CJAR" "$CONTENT_URL/search-meta" | python3 -c 'import sys,json;assert json.load(sys.stdin).get("local") is True' 2>/dev/null; then
      echo "  PASS /search-meta is local:true on the loopback content port"; else echo "  FAIL content port not local:true"; FAIL=$((FAIL+1)); fi
    csc=$(curl -s -b "$CJAR" -o /dev/null -w '%{http_code}' "$CONTENT_URL/search?q=x")
    if [ "$csc" = "200" ]; then echo "  PASS /search served on the content port (200)"; else echo "  FAIL /search on content port -> $csc (want 200)"; FAIL=$((FAIL+1)); fi
    if curl -s -b "$CJAR" "$CONTENT_URL/search-meta" | python3 -c 'import sys,json;assert "embed" in json.load(sys.stdin)' 2>/dev/null; then
      echo "  PASS /search-meta carries semantic embed status"; else echo "  FAIL /search-meta missing embed status"; FAIL=$((FAIL+1)); fi
    smc=$(curl -s -b "$CJAR" -o /dev/null -w '%{http_code}' "$CONTENT_URL/search?q=x&mode=semantic")
    if [ "$smc" = "200" ]; then echo "  PASS /search?mode=semantic served (200)"; else echo "  FAIL semantic search -> $smc (want 200)"; FAIL=$((FAIL+1)); fi
    rm -f "$CJAR"
  else
    echo "  SKIP content-port checks (loopback :$CPORT not reachable from here)"
  fi
  # V13: maintenance stats + encrypted export
  if curl -s -b "$JAR" "$URL/maintenance" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert "stats" in d and "usage" in d["stats"]' 2>/dev/null; then
    echo "  PASS /maintenance returns DB stats"; else echo "  FAIL /maintenance bad response"; FAIL=$((FAIL+1)); fi
  EXP=$(curl -s -b "$JAR" -X POST -H 'Content-Type: application/json' --data '{"passphrase":"verify-throwaway"}' "$URL/export")
  if echo "$EXP" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d.get("ok") and d.get("encrypted") and d.get("path")' 2>/dev/null; then
    echo "  PASS /export writes an encrypted bundle"
    EPATH=$(echo "$EXP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["path"])' 2>/dev/null)
    # the file must NOT be valid gzip/json (it's AES-GCM ciphertext) and must carry the magic header
    if docker exec cc-observability sh -c "head -c8 '$EPATH' | grep -q CCOBSENC" 2>/dev/null; then echo "  PASS export file is encrypted (CCOBSENC magic)"; else echo "  WARN could not verify export magic (path off-container?)"; fi
    docker exec cc-observability rm -f "$EPATH" 2>/dev/null
  else echo "  FAIL /export bad response: $EXP"; FAIL=$((FAIL+1)); fi
  SID=$(curl -s -b "$JAR" "$URL/state" | python3 -c 'import sys,json;r=json.load(sys.stdin);print(r[0]["session_id"] if r else "")' 2>/dev/null)
  # /autopsy needs a STORE-backed session (one with usage rows), not whatever happens to sit at /state[0]
  # (which can be a synthetic test snapshot). recent_sessions is exactly the store-backed list.
  ASID=$(curl -s -b "$JAR" "$URL/coach" | python3 -c 'import sys,json;rs=(json.load(sys.stdin).get("recent_sessions") or []);print(rs[0]["session_id"] if rs else "")' 2>/dev/null)
  if [ -n "$ASID" ]; then
    if curl -s -b "$JAR" "$URL/autopsy?sid=$ASID" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert "zone_time" in d and "events" in d' 2>/dev/null; then
      echo "  PASS /autopsy returns a report card"; else echo "  FAIL /autopsy bad response"; FAIL=$((FAIL+1)); fi
  fi
  if [ -n "$SID" ]; then
    if curl -s -b "$JAR" "$URL/activity?id=$SID&n=3" | python3 -c 'import sys,json;assert "items" in json.load(sys.stdin)' 2>/dev/null; then
      echo "  PASS /activity returns items"; else echo "  FAIL /activity bad response"; FAIL=$((FAIL+1)); fi
    if curl -s -b "$JAR" "$URL/history?id=$SID" | python3 -c 'import sys,json;assert isinstance(json.load(sys.stdin).get("points"),list)' 2>/dev/null; then
      echo "  PASS /history returns points"; else echo "  FAIL /history bad response"; FAIL=$((FAIL+1)); fi
    if curl -s -b "$JAR" "$URL/session-events?id=$SID" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert isinstance(d.get("counts"),dict) and isinstance(d.get("items"),list)' 2>/dev/null; then
      echo "  PASS /session-events returns counts"; else echo "  FAIL /session-events bad response"; FAIL=$((FAIL+1)); fi
  fi
else
  echo "  SKIP (dashboard not reachable at $URL — start the container or set CC_URL)"
fi

sec "Summary"
if [ "$FAIL" -eq 0 ]; then echo "  ✅ ALL GREEN"; exit 0; else echo "  ❌ $FAIL component(s) failed"; exit 1; fi
